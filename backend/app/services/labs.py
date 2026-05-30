import asyncio
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import RetryError

from app.core.config import get_settings
from app.core.security import generate_windows_password
from app.models.models import AuditLog, Batch, CleanupJob, CleanupReason, Lab, LabStatus, uuid_str
from app.schemas.schemas import BatchCreate
from app.services.aws_ec2 import AwsEc2Service
from app.services.guacamole import GuacamoleService
from app.services.pricing import fallback_windows_price, get_estimated_spot_windows_price, get_hourly_windows_price
from app.services.secrets import delete_lab_credential_artifacts, get_lab_password, store_lab_password

TERMINATED_LAB_VISIBLE_HOURS = 24
AWS_CLIENT_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 1})


def _schedule_provision_lab(lab_id: str, position: int = 0) -> None:
    delay_seconds = get_settings().lab_provision_stagger_seconds * position
    asyncio.create_task(_provision_lab_after_delay(lab_id, delay_seconds))


def _schedule_stop_lab(lab_id: str, reason: CleanupReason) -> None:
    asyncio.create_task(_stop_lab_by_id(lab_id, reason))


def _schedule_resume_lab(lab_id: str) -> None:
    asyncio.create_task(_resume_lab_by_id(lab_id))


def _schedule_terminate_lab(lab_id: str, reason: CleanupReason) -> None:
    asyncio.create_task(_terminate_lab_by_id(lab_id, reason))


async def _provision_lab_after_delay(lab_id: str, delay_seconds: int) -> None:
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    await provision_lab(lab_id)


async def _stop_lab_by_id(lab_id: str, reason: CleanupReason) -> None:
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        lab = await db.get(Lab, lab_id)
        if lab:
            await stop_lab(db, lab, reason)


async def _resume_lab_by_id(lab_id: str) -> None:
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        lab = await db.get(Lab, lab_id)
        if lab:
            await resume_lab(db, lab)


async def _terminate_lab_by_id(lab_id: str, reason: CleanupReason) -> None:
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        lab = await db.get(Lab, lab_id)
        if lab:
            await terminate_lab(db, lab, reason)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def terminated_lab_visible_cutoff(now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(hours=TERMINATED_LAB_VISIBLE_HOURS)


def visible_lab_filter(now: datetime | None = None):
    cutoff = terminated_lab_visible_cutoff(now)
    return (Lab.status != LabStatus.terminated) | (Lab.terminated_at.is_(None)) | (Lab.terminated_at > cutoff)


def lab_is_visible(lab: Lab, now: datetime | None = None) -> bool:
    if lab.status != LabStatus.terminated:
        return True
    if not lab.terminated_at:
        return True
    return _aware(lab.terminated_at) > terminated_lab_visible_cutoff(now)


def _runtime_seconds(lab: Lab, now: datetime) -> float:
    accumulated = lab.accumulated_runtime_seconds or 0
    if lab.status == LabStatus.running and lab.last_started_at:
        accumulated += max((now - _aware(lab.last_started_at)).total_seconds(), 0)
    return accumulated


def _budget_spend(lab: Lab, now: datetime) -> float:
    return (_runtime_seconds(lab, now) / 3600) * (lab.hourly_cost or 0)


def _budget_exhausted(lab: Lab, now: datetime) -> bool:
    return bool(lab.hourly_cost and _budget_spend(lab, now) >= lab.budget_limit)


def _requested_market(on_demand_hourly_cost: float, spot_hourly_cost: float | None) -> str:
    settings = get_settings()
    configured_market = settings.lab_instance_market.lower()
    if not settings.lab_spot_enabled:
        return "on-demand"
    if configured_market == "spot":
        return "spot"
    if configured_market == "auto" and spot_hourly_cost is not None and spot_hourly_cost < on_demand_hourly_cost:
        return "spot"
    return "on-demand"


def _configured_claude_profiles() -> list[str]:
    settings = get_settings()
    return [item.strip() for item in settings.claude_profile_ids.split(",") if item.strip()]


def _claude_profile_key(profile_id: str) -> str:
    settings = get_settings()
    prefix = settings.claude_profile_prefix.strip("/")
    filename = f"{profile_id}{settings.claude_profile_archive_suffix}"
    return f"{prefix}/{filename}" if prefix else filename


async def _claude_profile_archive_exists(region: str, profile_id: str) -> bool:
    settings = get_settings()
    if not settings.claude_profile_bucket:
        return False
    client = boto3.client("s3", region_name=region, config=AWS_CLIENT_CONFIG)
    try:
        await asyncio.to_thread(
            client.head_object,
            Bucket=settings.claude_profile_bucket,
            Key=_claude_profile_key(profile_id),
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"403", "404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return True


async def _fast_launch_state(region: str, ami_id: str) -> tuple[str, int]:
    client = boto3.client("ec2", region_name=region, config=AWS_CLIENT_CONFIG)
    response = await asyncio.to_thread(client.describe_fast_launch_images, ImageIds=[ami_id])
    images = response.get("FastLaunchImages") or []
    if not images:
        return "disabled", 0
    image = images[0]
    target_count = int((image.get("SnapshotConfiguration") or {}).get("TargetResourceCount") or 0)
    return str(image.get("State") or "unknown"), target_count


async def _require_fast_launch_ready(region: str, ami_id: str) -> None:
    settings = get_settings()
    if not settings.claude_require_fast_launch:
        return
    state, target_count = await _fast_launch_state(region, ami_id)
    min_target_count = settings.claude_fast_launch_min_target_count
    if state != "enabled" or target_count < min_target_count:
        raise RuntimeError(
            f"Claude AMI Fast Launch is {state} with target pool {target_count}. "
            f"Wait until AWS reports Fast Launch enabled with target pool at least {min_target_count} for {ami_id} before launching Claude labs."
        )


async def _available_claude_profiles(db: AsyncSession) -> list[str]:
    profiles = _configured_claude_profiles()
    if not profiles:
        return []
    active_statuses = [
        LabStatus.scheduled,
        LabStatus.provisioning,
        LabStatus.running,
        LabStatus.stopped,
        LabStatus.resuming,
        LabStatus.budget_exceeded,
    ]
    rows = await db.scalars(
        select(Lab.claude_profile_id).where(
            Lab.lab_type == "claude",
            Lab.claude_profile_id.is_not(None),
            Lab.status.in_(active_statuses),
        )
    )
    used = {profile_id for profile_id in rows if profile_id}
    return [profile_id for profile_id in profiles if profile_id not in used]


async def _require_claude_profile_archives(region: str, profile_ids: list[str]) -> None:
    settings = get_settings()
    if not settings.claude_require_profile_archive:
        return
    missing: list[str] = []
    for profile_id in profile_ids:
        if not await _claude_profile_archive_exists(region, profile_id):
            missing.append(_claude_profile_key(profile_id))
    if missing:
        raise RuntimeError(
            "Claude pre-login profile archive is missing. "
            f"Upload a logged-in {settings.claude_account_email} profile archive to "
            f"s3://{settings.claude_profile_bucket}/{missing[0]} before launching Claude labs."
        )


def _is_spot_lab(lab: Lab) -> bool:
    return (lab.instance_market or "").lower() == "spot"


def _accrue_running_time(lab: Lab, now: datetime) -> None:
    if lab.last_started_at:
        lab.accumulated_runtime_seconds = _runtime_seconds(lab, now)
    lab.last_started_at = None


def _lab_owner_label(batch_name: str, index: int, created_at: datetime) -> str:
    date_stamp = created_at.strftime("%Y%m%d")
    return f"UNext-user-{index:03d}-{date_stamp}-{batch_name}"


def _lab_username(index: int, lab_id: str, created_at: datetime) -> str:
    date_stamp = created_at.strftime("%m%d")
    return f"UNext{index:03d}-{date_stamp}-{lab_id[:6]}"


def _rdp_username(lab: Lab) -> str:
    return get_settings().windows_admin_user


def _parse_hhmm(value: str) -> time:
    hour, minute = [int(part) for part in value.split(":", 1)]
    return time(hour=hour, minute=minute)


def _schedule_final_expiry(
    start_date: date,
    days: int,
    end_time: str,
    timezone_name: str,
) -> datetime:
    local_tz = ZoneInfo(timezone_name)
    final_date = start_date + timedelta(days=days - 1)
    local_expiry = datetime.combine(final_date, _parse_hhmm(end_time), tzinfo=local_tz)
    return local_expiry.astimezone(timezone.utc)


def _schedule_state(lab: Lab, now: datetime) -> str:
    if not (
        lab.schedule_enabled
        and lab.schedule_start_date
        and lab.schedule_days
        and lab.schedule_start_time
        and lab.schedule_end_time
    ):
        return "unscheduled"

    local_tz = ZoneInfo(lab.schedule_timezone or "Asia/Kolkata")
    local_now = now.astimezone(local_tz)
    first_date = lab.schedule_start_date
    last_date = first_date + timedelta(days=lab.schedule_days - 1)
    if local_now.date() < first_date:
        return "before"
    if local_now.date() > last_date:
        return "after"

    start_at = datetime.combine(local_now.date(), _parse_hhmm(lab.schedule_start_time), tzinfo=local_tz)
    end_at = datetime.combine(local_now.date(), _parse_hhmm(lab.schedule_end_time), tzinfo=local_tz)
    if local_now < start_at:
        return "before"
    if local_now >= end_at:
        return "after_day" if local_now.date() < last_date else "after"
    return "active"


def _pause_provisioning_until_schedule(db: AsyncSession, lab: Lab, state: str) -> None:
    lab.status = LabStatus.scheduled
    db.add(
        AuditLog(
            actor="system",
            action="lab.schedule.wait",
            resource_id=lab.id,
            message=f"Provisioning paused because the scheduled window is {state}",
        )
    )


async def _provisioning_was_cancelled(db: AsyncSession, lab: Lab) -> bool:
    await db.refresh(lab)
    if lab.status == LabStatus.provisioning:
        return False
    db.add(
        AuditLog(
            actor="system",
            action="lab.provision.cancelled",
            resource_id=lab.id,
            message=f"Provisioning finished after lab moved to {lab.status.value}; leaving current status unchanged",
        )
    )
    await db.commit()
    return True


def _batch_is_active_now(payload: BatchCreate, now: datetime) -> bool:
    if not payload.schedule_enabled:
        return True
    sample = Lab(
        batch_id="",
        owner_label="",
        aws_region=payload.aws_region,
        instance_type=payload.instance_type,
        windows_ami=payload.windows_ami,
        username="",
        password_secret_ref="",
        password_ciphertext="",
        budget_limit=payload.budget_per_vm,
        hourly_cost=0,
        expiry_time=now,
        idle_timeout_minutes=payload.idle_timeout_minutes,
        schedule_enabled=True,
        schedule_start_date=payload.schedule_start_date,
        schedule_days=payload.schedule_days,
        schedule_start_time=payload.schedule_start_time,
        schedule_end_time=payload.schedule_end_time,
        schedule_timezone=payload.schedule_timezone,
    )
    return _schedule_state(sample, now) == "active"


def _lab_rdp_host_candidates(lab: Lab) -> list[str]:
    return [host for host in [lab.private_ip, lab.public_ip] if host]


def _rdp_wait_attempts(lab: Lab) -> int:
    return 90 if lab.lab_type == "claude" else 90


def _windows_ready_wait_attempts(lab: Lab) -> int:
    return 120 if lab.lab_type == "claude" else 90


async def _can_reach_rdp(hostname: str, timeout_seconds: float = 4) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(hostname, 3389), timeout=timeout_seconds)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def _reachable_rdp_host(lab: Lab, *, attempts: int = 30, delay_seconds: int = 10) -> str:
    candidates = _lab_rdp_host_candidates(lab)
    if not candidates:
        raise RuntimeError("Lab has no reachable IP address")

    for attempt in range(attempts):
        for hostname in candidates:
            if await _can_reach_rdp(hostname):
                return hostname
        if attempt < attempts - 1:
            await asyncio.sleep(delay_seconds)

    raise RuntimeError(f"RDP port 3389 is not reachable on {', '.join(candidates)}")


async def _sync_guacamole_rdp_target(lab: Lab, windows_hostname: str | None = None) -> None:
    if not lab.guacamole_connection_id:
        return
    hostname = await _reachable_rdp_host(lab, attempts=30, delay_seconds=10)
    password = await get_lab_password(lab.aws_region, lab.password_secret_ref, lab.password_ciphertext)
    await GuacamoleService().update_rdp_connection(
        lab.guacamole_connection_id,
        hostname=hostname,
        username=_rdp_username(lab),
        password=password,
        domain=windows_hostname or ".",
    )


async def _stable_rdp_host(lab: Lab) -> str:
    guac_host = await _reachable_rdp_host(lab, attempts=_rdp_wait_attempts(lab), delay_seconds=10)
    if lab.lab_type == "claude":
        await asyncio.sleep(30)
        if not await _can_reach_rdp(guac_host):
            guac_host = await _reachable_rdp_host(lab, attempts=12, delay_seconds=10)
    return guac_host


async def _ensure_guacamole_access(
    db: AsyncSession,
    lab: Lab,
    raw_password: str,
    windows_hostname: str | None = None,
    *,
    rdp_host: str | None = None,
) -> None:
    guacamole = GuacamoleService()
    guac_host = rdp_host or await _stable_rdp_host(lab)
    db.add(AuditLog(actor="system", action="lab.rdp.ready", resource_id=lab.id, message=f"RDP is reachable on {guac_host}"))
    await db.commit()

    if lab.guacamole_connection_id:
        await guacamole.update_rdp_connection(
            lab.guacamole_connection_id,
            hostname=guac_host,
            username=_rdp_username(lab),
            password=raw_password,
            domain=windows_hostname or ".",
        )
        lab.access_url = lab.access_url or guacamole.access_url_for_connection(lab.guacamole_connection_id)
    else:
        instance_suffix = f"-{lab.ec2_instance_id[-6:]}" if lab.ec2_instance_id else ""
        connection_id, access_url = await guacamole.create_rdp_connection(
            name=f"{lab.owner_label}-{lab.id[:8]}{instance_suffix}",
            hostname=guac_host,
            username=_rdp_username(lab),
            password=raw_password,
            domain=windows_hostname or ".",
        )
        lab.guacamole_connection_id = connection_id
        lab.access_url = access_url
        await db.commit()

    await guacamole.create_user_mapping(username=lab.username, password=raw_password, connection_id=lab.guacamole_connection_id)
    lab.access_url = lab.access_url or guacamole.access_url_for_connection(lab.guacamole_connection_id)


async def _delete_lab_password_secret(lab: Lab, errors: list[str] | None = None) -> None:
    try:
        deleted = await delete_lab_credential_artifacts(lab.aws_region, lab.id, lab.password_secret_ref)
        if deleted:
            lab.password_secret_ref = "deleted"
            lab.password_ciphertext = ""
    except Exception as exc:
        if errors is not None:
            errors.append(f"secret: {exc}")


async def create_batch(db: AsyncSession, payload: BatchCreate, admin_id: str) -> Batch:
    settings = get_settings()
    claude_profiles: list[str] = []
    if payload.lab_type == "claude":
        if not settings.claude_profile_bucket:
            raise RuntimeError("CLAUDE_PROFILE_BUCKET must be configured before launching Claude labs")
        claude_profiles = await _available_claude_profiles(db)
        if len(claude_profiles) < payload.user_count:
            raise RuntimeError(f"Only {len(claude_profiles)} Claude profile(s) are available for {payload.user_count} requested lab(s)")
    on_demand_hourly_cost = fallback_windows_price(payload.instance_type)
    spot_hourly_cost = None
    requested_market = "on-demand" if payload.lab_type == "claude" else _requested_market(on_demand_hourly_cost, spot_hourly_cost)
    hourly_cost = on_demand_hourly_cost
    created_at = utcnow()
    if payload.schedule_enabled:
        expiry = _schedule_final_expiry(
            payload.schedule_start_date,
            payload.schedule_days,
            payload.schedule_end_time,
            payload.schedule_timezone,
        )
    else:
        expiry = created_at + timedelta(hours=payload.duration_hours)
    initial_status = LabStatus.provisioning if _batch_is_active_now(payload, created_at) else LabStatus.scheduled

    batch = Batch(**payload.model_dump(), created_by=admin_id)
    db.add(batch)
    await db.flush()

    labs: list[Lab] = []
    for index in range(1, payload.user_count + 1):
        lab_id = uuid_str()
        password = generate_windows_password()
        lab = Lab(
            id=lab_id,
            batch_id=batch.id,
            owner_label=_lab_owner_label(payload.name, index, created_at),
            aws_region=payload.aws_region,
            instance_type=payload.instance_type,
            windows_ami=payload.windows_ami,
            lab_type=payload.lab_type,
            claude_profile_id=claude_profiles[index - 1] if payload.lab_type == "claude" else None,
            username=_lab_username(index, lab_id, created_at),
            password_secret_ref="pending",
            password_ciphertext=password,
            budget_limit=payload.budget_per_vm,
            hourly_cost=hourly_cost,
            on_demand_hourly_cost=on_demand_hourly_cost,
            spot_hourly_cost=spot_hourly_cost,
            requested_instance_market=requested_market,
            instance_market=requested_market,
            expiry_time=expiry,
            idle_timeout_minutes=payload.idle_timeout_minutes,
            schedule_enabled=payload.schedule_enabled,
            schedule_start_date=payload.schedule_start_date,
            schedule_days=payload.schedule_days,
            schedule_start_time=payload.schedule_start_time,
            schedule_end_time=payload.schedule_end_time,
            schedule_timezone=payload.schedule_timezone,
            accumulated_runtime_seconds=0,
            status=initial_status,
        )
        db.add(lab)
        labs.append(lab)
    await db.commit()

    db.add(AuditLog(actor=admin_id, action="batch.created", resource_id=batch.id, message=f"Created {len(labs)} labs"))
    for lab in labs:
        db.add(
            AuditLog(
                actor="system",
                action="lab.credentials.ready",
                resource_id=lab.id,
                message="Credentials are ready. Browser access will appear after Windows and Guacamole finish provisioning.",
            )
        )
    await db.commit()
    for position, lab in enumerate(labs):
        if lab.status == LabStatus.provisioning:
            _schedule_provision_lab(lab.id, position)
    return batch


async def provision_lab(lab_id: str) -> None:
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        lab = await db.get(Lab, lab_id)
        if not lab:
            return
        if lab.status != LabStatus.provisioning:
            return
        schedule_state = _schedule_state(lab, utcnow())
        if schedule_state in {"before", "after_day"}:
            _pause_provisioning_until_schedule(db, lab, schedule_state)
            await db.commit()
            return
        if schedule_state == "after":
            await terminate_lab(db, lab, CleanupReason.expiry)
            return
        raw_password = (
            lab.password_ciphertext
            if lab.password_secret_ref == "pending"
            else await get_lab_password(lab.aws_region, lab.password_secret_ref, lab.password_ciphertext)
        )
        try:
            secret_ref, stored_value = await store_lab_password(lab.aws_region, lab.id, raw_password)
            lab.password_secret_ref = secret_ref
            lab.password_ciphertext = stored_value
            db.add(AuditLog(actor="system", action="lab.provision.started", resource_id=lab.id, message="Launching Windows EC2 instance"))
            await db.commit()

            if lab.lab_type == "claude":
                await _require_fast_launch_ready(lab.aws_region, lab.windows_ami)
                if lab.claude_profile_id:
                    await _require_claude_profile_archives(lab.aws_region, [lab.claude_profile_id])

            lab.on_demand_hourly_cost = await get_hourly_windows_price(lab.aws_region, lab.instance_type, get_settings().pricing_cache_ttl_seconds)
            if lab.lab_type != "claude":
                lab.spot_hourly_cost = await get_estimated_spot_windows_price(lab.aws_region, lab.instance_type, get_settings().pricing_cache_ttl_seconds)
                lab.requested_instance_market = _requested_market(lab.on_demand_hourly_cost, lab.spot_hourly_cost)
                lab.instance_market = lab.requested_instance_market
                lab.hourly_cost = lab.spot_hourly_cost if lab.requested_instance_market == "spot" and lab.spot_hourly_cost is not None else lab.on_demand_hourly_cost
            else:
                lab.requested_instance_market = "on-demand"
                lab.instance_market = "on-demand"
                lab.hourly_cost = lab.on_demand_hourly_cost
            await db.commit()

            ec2 = AwsEc2Service(lab.aws_region)
            if lab.ec2_instance_id:
                db.add(
                    AuditLog(
                        actor="system",
                        action="lab.provision.resumed",
                        resource_id=lab.id,
                        message=f"Continuing provisioning for existing EC2 instance {lab.ec2_instance_id}",
                    )
                )
                await db.commit()
                instance = await ec2.start_instance(lab.ec2_instance_id, lab_id=lab.id)
            else:
                instance = await ec2.launch_windows_instance(
                    ami_id=lab.windows_ami,
                    instance_type=lab.instance_type,
                    username=lab.username,
                    password=raw_password,
                    display_name=lab.owner_label,
                    batch_id=lab.batch_id,
                    lab_id=lab.id,
                    budget_limit=lab.budget_limit,
                    idle_timeout_minutes=lab.idle_timeout_minutes,
                    expiry_iso=lab.expiry_time.isoformat(),
                    instance_market=lab.requested_instance_market,
                    lab_type=lab.lab_type,
                    claude_profile_id=lab.claude_profile_id,
                )
            lab.ec2_instance_id = instance.instance_id
            lab.instance_type = instance.instance_type or lab.instance_type
            lab.instance_market = instance.market
            if instance.market == "on-demand":
                lab.hourly_cost = lab.on_demand_hourly_cost or lab.hourly_cost
            else:
                lab.spot_hourly_cost = await get_estimated_spot_windows_price(lab.aws_region, lab.instance_type, get_settings().pricing_cache_ttl_seconds)
                if lab.spot_hourly_cost is not None:
                    lab.hourly_cost = lab.spot_hourly_cost
            lab.private_ip = instance.private_ip
            lab.public_ip = instance.public_ip
            db.add(
                AuditLog(
                    actor="system",
                    action="lab.instance.running",
                    resource_id=lab.id,
                    message=f"EC2 instance {instance.instance_id} is running; checking RDP directly",
                )
            )
            await db.commit()

            if lab.lab_type == "claude":
                rdp_host = await _stable_rdp_host(lab)
                db.add(AuditLog(actor="system", action="lab.rdp.stable", resource_id=lab.id, message=f"RDP is stable on {rdp_host}; waiting for Claude Desktop"))
                await db.commit()
                db.add(
                    AuditLog(
                        actor="system",
                        action="lab.claude.waiting",
                        resource_id=lab.id,
                        message="Waiting for Claude Desktop install/profile marker before exposing browser access",
                    )
                )
                await db.commit()
                await ec2.wait_claude_ready(instance.instance_id, max_attempts=60, delay_seconds=10)
                db.add(AuditLog(actor="system", action="lab.claude.ready", resource_id=lab.id, message="Claude Desktop is installed and profile marker is ready"))
                await db.commit()
                await _ensure_guacamole_access(db, lab, raw_password, instance.windows_hostname, rdp_host=rdp_host)
            else:
                await _ensure_guacamole_access(db, lab, raw_password, instance.windows_hostname)
            if await _provisioning_was_cancelled(db, lab):
                return
            ready_at = utcnow()
            if not lab.schedule_enabled:
                batch = await db.get(Batch, lab.batch_id)
                if batch:
                    lab.expiry_time = ready_at + timedelta(hours=batch.duration_hours)
                    try:
                        await ec2.update_instance_expiry_tag(
                            instance.instance_id,
                            lab_id=lab.id,
                            expiry_iso=lab.expiry_time.isoformat(),
                        )
                    except Exception as exc:
                        db.add(AuditLog(actor="system", action="lab.expiry_tag_failed", resource_id=lab.id, message=str(exc)))
            lab.status = LabStatus.running
            lab.last_seen_at = None
            lab.last_started_at = ready_at
            db.add(
                AuditLog(
                    actor="system",
                    action="lab.provisioned",
                    resource_id=lab.id,
                    message=f"Lab is running on {lab.instance_market} ({lab.instance_type})",
                )
            )
            await db.commit()
        except Exception as exc:
            await db.refresh(lab)
            if lab.status != LabStatus.provisioning:
                db.add(
                    AuditLog(
                        actor="system",
                        action="lab.provision.cancelled",
                        resource_id=lab.id,
                        message=f"Provisioning failed after lab moved to {lab.status.value}; leaving current status unchanged: {exc}",
                    )
                )
                await db.commit()
                return
            lab.status = LabStatus.failed
            message = _provisioning_error_message(exc)
            db.add(AuditLog(actor="system", action="lab.failed", resource_id=lab.id, message=message))
            await db.commit()


async def resume_pending_provisioning() -> int:
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        rows = (
            await db.scalars(
                select(Lab).where(
                    Lab.status == LabStatus.provisioning,
                    visible_lab_filter(),
                )
            )
        ).all()
        for lab in rows:
            db.add(AuditLog(actor="system", action="lab.provision.requeued", resource_id=lab.id, message="Requeued unfinished provisioning after backend startup"))
        await db.commit()
        for position, lab in enumerate(rows):
            _schedule_provision_lab(lab.id, position)
        return len(rows)


def _provisioning_error_message(exc: Exception) -> str:
    if isinstance(exc, RetryError) and exc.last_attempt.failed:
        return str(exc.last_attempt.exception())
    return str(exc)


async def recover_failed_lab(db: AsyncSession, lab: Lab) -> None:
    now = utcnow()
    if lab.status != LabStatus.failed:
        raise RuntimeError(f"Lab cannot be recovered from status {lab.status.value}")
    if lab.schedule_enabled and _schedule_state(lab, now) != "active":
        raise RuntimeError("Lab is outside the scheduled time window")
    if not lab.ec2_instance_id:
        raise RuntimeError("Lab has no EC2 instance to recover")
    if _aware(lab.expiry_time) <= now:
        await terminate_lab(db, lab, CleanupReason.expiry)
        raise RuntimeError("Lab has expired")
    if _budget_exhausted(lab, now):
        lab.status = LabStatus.budget_exceeded
        await db.commit()
        raise RuntimeError("Lab budget has been exhausted")

    raw_password = await get_lab_password(lab.aws_region, lab.password_secret_ref, lab.password_ciphertext)
    lab.status = LabStatus.resuming
    db.add(AuditLog(actor="system", action="lab.recover.started", resource_id=lab.id, message="Repairing EC2 and browser access for failed lab"))
    await db.commit()
    try:
        instance = await AwsEc2Service(lab.aws_region).start_instance(lab.ec2_instance_id, lab_id=lab.id)
        lab.private_ip = instance.private_ip
        lab.public_ip = instance.public_ip
        await _ensure_guacamole_access(db, lab, raw_password, instance.windows_hostname)
        lab.status = LabStatus.running
        lab.last_seen_at = None
        lab.last_started_at = lab.last_started_at or utcnow()
        db.add(AuditLog(actor="system", action="lab.recover.finished", resource_id=lab.id, message="Lab access repaired"))
    except Exception as exc:
        lab.status = LabStatus.failed
        db.add(AuditLog(actor="system", action="lab.recover.failed", resource_id=lab.id, message=str(exc)))
    await db.commit()


async def terminate_lab(db: AsyncSession, lab: Lab, reason: CleanupReason) -> None:
    if lab.status in {LabStatus.terminated, LabStatus.expired, LabStatus.interrupted}:
        errors: list[str] = []
        await _delete_lab_password_secret(lab, errors)
        if errors:
            db.add(AuditLog(actor="system", action="lab.cleanup.credential_failed", resource_id=lab.id, message="; ".join(errors)))
        await db.commit()
        return

    now = utcnow()
    if lab.status == LabStatus.running:
        _accrue_running_time(lab, now)
    if lab.status != LabStatus.terminating:
        lab.status = LabStatus.terminating
    job = CleanupJob(lab_id=lab.id, reason=reason, status="running")
    db.add(job)
    await db.commit()

    errors: list[str] = []
    critical_errors: list[str] = []
    if lab.guacamole_connection_id:
        try:
            await GuacamoleService().delete_connection(lab.guacamole_connection_id)
            lab.guacamole_connection_id = None
            lab.access_url = None
        except Exception as exc:
            errors.append(f"guacamole: {exc}")
    try:
        await GuacamoleService().delete_user(lab.username)
    except Exception:
        pass
    if lab.ec2_instance_id:
        try:
            ec2 = AwsEc2Service(lab.aws_region)
            await ec2.terminate_instance(lab.ec2_instance_id, lab_id=lab.id)
            await ec2.delete_available_lab_volumes(lab_id=lab.id)
        except Exception as exc:
            error = f"ec2: {exc}"
            errors.append(error)
            if "was not found" not in str(exc):
                critical_errors.append(error)
    else:
        try:
            await AwsEc2Service(lab.aws_region).delete_available_lab_volumes(lab_id=lab.id)
        except Exception as exc:
            errors.append(f"volumes: {exc}")
    await _delete_lab_password_secret(lab, critical_errors)
    errors.extend(error for error in critical_errors if error not in errors)

    lab.status = LabStatus.failed if critical_errors else LabStatus.terminated
    lab.terminated_at = utcnow()
    job.status = "failed" if critical_errors else "finished"
    job.finished_at = utcnow()
    job.message = "; ".join(errors) if errors else "Cleanup completed"
    db.add(AuditLog(actor="system", action=f"lab.cleanup.{reason.value}", resource_id=lab.id, message=job.message))
    await db.commit()


async def retry_terminating_labs(db: AsyncSession) -> int:
    rows = (await db.scalars(select(Lab).where(Lab.status == LabStatus.terminating))).all()
    for lab in rows:
        await terminate_lab(db, lab, CleanupReason.force)
    return len(rows)


async def stop_lab(db: AsyncSession, lab: Lab, reason: CleanupReason) -> None:
    if lab.status in {LabStatus.stopped, LabStatus.budget_exceeded, LabStatus.terminated, LabStatus.expired, LabStatus.terminating, LabStatus.interrupted}:
        return

    now = utcnow()
    _accrue_running_time(lab, now)
    job = CleanupJob(lab_id=lab.id, reason=reason, status="running")
    db.add(job)
    await db.commit()

    errors: list[str] = []
    if lab.ec2_instance_id:
        try:
            await AwsEc2Service(lab.aws_region).stop_instance(lab.ec2_instance_id, lab_id=lab.id)
        except Exception as exc:
            errors.append(f"ec2: {exc}")

    if errors:
        lab.status = LabStatus.failed
    elif reason == CleanupReason.budget:
        lab.status = LabStatus.budget_exceeded
    else:
        lab.status = LabStatus.stopped

    job.status = "finished" if not errors else "failed"
    job.finished_at = utcnow()
    job.message = "; ".join(errors) if errors else f"Instance stopped due to {reason.value}"
    db.add(AuditLog(actor="system", action=f"lab.stop.{reason.value}", resource_id=lab.id, message=job.message))
    await db.commit()


async def resume_lab(db: AsyncSession, lab: Lab) -> None:
    now = utcnow()
    if lab.schedule_enabled and _schedule_state(lab, now) != "active":
        raise RuntimeError("Lab is outside the scheduled time window")
    if lab.status == LabStatus.running:
        lab.last_seen_at = now
        await db.commit()
        if lab.ec2_instance_id:
            try:
                instance = await AwsEc2Service(lab.aws_region).start_instance(lab.ec2_instance_id, lab_id=lab.id)
                lab.private_ip = instance.private_ip
                lab.public_ip = instance.public_ip
                await _sync_guacamole_rdp_target(lab, instance.windows_hostname)
                lab.last_started_at = lab.last_started_at or utcnow()
            except Exception as exc:
                db.add(AuditLog(actor="system", action="lab.resume.failed", resource_id=lab.id, message=str(exc)))
                await db.commit()
                raise RuntimeError("Lab could not be started") from exc
            await db.commit()
        return
    if lab.status != LabStatus.stopped:
        raise RuntimeError(f"Lab cannot be resumed from status {lab.status.value}")
    if _aware(lab.expiry_time) <= now:
        await terminate_lab(db, lab, CleanupReason.expiry)
        raise RuntimeError("Lab has expired")
    if _budget_exhausted(lab, now):
        lab.status = LabStatus.budget_exceeded
        await db.commit()
        raise RuntimeError("Lab budget has been exhausted")
    if not lab.ec2_instance_id:
        raise RuntimeError("Lab has no EC2 instance to resume")

    lab.status = LabStatus.resuming
    db.add(AuditLog(actor="system", action="lab.resume.started", resource_id=lab.id, message="Starting stopped EC2 instance"))
    await db.commit()
    try:
        instance = await AwsEc2Service(lab.aws_region).start_instance(lab.ec2_instance_id, lab_id=lab.id)
        lab.private_ip = instance.private_ip
        lab.public_ip = instance.public_ip
        await _sync_guacamole_rdp_target(lab, instance.windows_hostname)
        lab.status = LabStatus.running
        lab.last_seen_at = utcnow()
        lab.last_started_at = lab.last_seen_at
        db.add(AuditLog(actor="system", action="lab.resume.finished", resource_id=lab.id, message="Lab is running again"))
    except Exception as exc:
        if _is_spot_lab(lab) and "IncorrectSpotRequestState" in str(exc):
            db.add(
                AuditLog(
                    actor="system",
                    action="lab.resume.spot_relaunch",
                    resource_id=lab.id,
                    message=f"Stopped Spot instance could not be started; relaunching Spot instance: {exc}",
                )
            )
            await db.commit()
            try:
                raw_password = await get_lab_password(lab.aws_region, lab.password_secret_ref, lab.password_ciphertext)
                await _relaunch_spot_lab_instance(db, lab, raw_password)
            except Exception as relaunch_exc:
                lab.status = LabStatus.stopped
                db.add(AuditLog(actor="system", action="lab.resume.failed", resource_id=lab.id, message=str(relaunch_exc)))
        else:
            lab.status = LabStatus.stopped
            db.add(AuditLog(actor="system", action="lab.resume.failed", resource_id=lab.id, message=str(exc)))
    await db.commit()


async def _relaunch_spot_lab_instance(db: AsyncSession, lab: Lab, raw_password: str) -> None:
    old_instance_id = lab.ec2_instance_id
    ec2 = AwsEc2Service(lab.aws_region)
    if old_instance_id:
        try:
            await ec2.terminate_instance(old_instance_id, lab_id=lab.id)
        except Exception as exc:
            db.add(AuditLog(actor="system", action="lab.resume.old_spot_cleanup_failed", resource_id=lab.id, message=str(exc)))
            await db.commit()

    instance = await ec2.launch_windows_instance(
        ami_id=lab.windows_ami,
        instance_type=lab.instance_type,
        username=lab.username,
        password=raw_password,
        display_name=lab.owner_label,
        batch_id=lab.batch_id,
        lab_id=lab.id,
        budget_limit=lab.budget_limit,
        idle_timeout_minutes=lab.idle_timeout_minutes,
        expiry_iso=lab.expiry_time.isoformat(),
        instance_market="spot",
        lab_type=lab.lab_type,
        claude_profile_id=lab.claude_profile_id,
    )
    lab.ec2_instance_id = instance.instance_id
    lab.instance_type = instance.instance_type or lab.instance_type
    lab.instance_market = instance.market
    lab.private_ip = instance.private_ip
    lab.public_ip = instance.public_ip
    db.add(
        AuditLog(
            actor="system",
            action="lab.resume.spot_relaunched",
            resource_id=lab.id,
            message=f"Replaced stopped Spot instance {old_instance_id} with {instance.instance_id}",
        )
    )
    await db.commit()

    await _ensure_guacamole_access(db, lab, raw_password, instance.windows_hostname)
    lab.status = LabStatus.running
    lab.last_seen_at = utcnow()
    lab.last_started_at = lab.last_seen_at
    db.add(AuditLog(actor="system", action="lab.resume.finished", resource_id=lab.id, message="Lab is running again on a replacement Spot instance"))


async def prepare_lab_session(db: AsyncSession, lab: Lab) -> None:
    now = utcnow()
    schedule_state = _schedule_state(lab, now)
    if schedule_state in {"before", "after_day"}:
        raise RuntimeError("Lab is outside the scheduled time window")
    if schedule_state == "after":
        await terminate_lab(db, lab, CleanupReason.expiry)
        raise RuntimeError("Lab schedule has ended")
    if lab.status in {LabStatus.budget_exceeded, LabStatus.expired, LabStatus.terminated, LabStatus.terminating, LabStatus.interrupted}:
        raise RuntimeError(f"Lab is {lab.status.value}")
    if _aware(lab.expiry_time) <= now:
        await terminate_lab(db, lab, CleanupReason.expiry)
        raise RuntimeError("Lab has expired")
    if _budget_exhausted(lab, now):
        await stop_lab(db, lab, CleanupReason.budget)
        raise RuntimeError("Lab budget has been exhausted")
    if lab.status == LabStatus.stopped:
        await resume_lab(db, lab)
        if lab.status != LabStatus.running:
            raise RuntimeError("Lab could not be started")
        return
    if lab.status != LabStatus.running:
        return

    lab.last_seen_at = now
    await db.commit()
    if not lab.ec2_instance_id:
        return

    try:
        state = await AwsEc2Service(lab.aws_region).instance_state(lab.ec2_instance_id, lab_id=lab.id)
    except Exception as exc:
        db.add(AuditLog(actor="system", action="lab.session.state_failed", resource_id=lab.id, message=str(exc)))
        await db.commit()
        return
    if state == "terminated":
        _accrue_running_time(lab, utcnow())
        lab.status = LabStatus.interrupted if _is_spot_lab(lab) else LabStatus.terminated
        lab.interrupted_at = utcnow() if _is_spot_lab(lab) else lab.interrupted_at
        lab.terminated_at = lab.terminated_at or utcnow()
        await _delete_lab_password_secret(lab)
        db.add(AuditLog(actor="system", action="lab.instance.terminated", resource_id=lab.id, message="EC2 instance terminated outside the normal cleanup path"))
        await db.commit()
        raise RuntimeError("Lab instance has ended")
    if state in {"stopped", "stopping"}:
        await resume_lab(db, lab)
        if lab.status != LabStatus.running:
            raise RuntimeError("Lab could not be started")
    elif state == "running":
        await _sync_guacamole_rdp_target(lab)


async def touch_lab_access(db: AsyncSession, connection_id: str) -> None:
    lab = await db.scalar(select(Lab).where(Lab.guacamole_connection_id == connection_id))
    if not lab:
        return
    now = utcnow()
    if lab.status == LabStatus.running and _budget_exhausted(lab, now):
        await stop_lab(db, lab, CleanupReason.budget)
    elif lab.status == LabStatus.running:
        lab.last_seen_at = utcnow()
        await db.commit()
    elif lab.status == LabStatus.stopped:
        await resume_lab(db, lab)


async def refresh_active_lab_presence(db: AsyncSession) -> None:
    try:
        active_connection_ids = await GuacamoleService().active_connection_ids()
    except Exception:
        return
    if not active_connection_ids:
        return
    now = utcnow()
    rows = await db.scalars(
        select(Lab).where(Lab.status == LabStatus.running, Lab.guacamole_connection_id.in_(active_connection_ids))
    )
    for lab in rows:
        lab.last_seen_at = now
    await db.commit()


async def find_due_labs(db: AsyncSession) -> list[tuple[Lab, CleanupReason]]:
    now = utcnow()
    rows = await db.scalars(
        select(Lab).where(Lab.status.notin_([LabStatus.expired, LabStatus.terminated, LabStatus.terminating, LabStatus.interrupted]))
    )
    due: list[tuple[Lab, CleanupReason]] = []
    for lab in rows:
        if lab.status == LabStatus.running and lab.ec2_instance_id:
            try:
                state = await AwsEc2Service(lab.aws_region).instance_state(lab.ec2_instance_id, lab_id=lab.id)
            except Exception as exc:
                db.add(AuditLog(actor="system", action="lab.poll.failed", resource_id=lab.id, message=str(exc)))
                continue
            if state == "terminated":
                _accrue_running_time(lab, now)
                lab.status = LabStatus.interrupted if _is_spot_lab(lab) else LabStatus.terminated
                lab.interrupted_at = now if _is_spot_lab(lab) else lab.interrupted_at
                lab.terminated_at = lab.terminated_at or now
                await _delete_lab_password_secret(lab)
                db.add(AuditLog(actor="system", action="lab.spot.interrupted" if _is_spot_lab(lab) else "lab.instance.terminated", resource_id=lab.id, message=f"EC2 instance is {state}"))
                await db.commit()
                continue
            if _is_spot_lab(lab) and state in {"stopped", "stopping"}:
                _accrue_running_time(lab, now)
                lab.status = LabStatus.stopped
                db.add(AuditLog(actor="system", action="lab.spot.stopped", resource_id=lab.id, message=f"EC2 Spot instance is {state}"))
                await db.commit()
                continue
        expiry_time = _aware(lab.expiry_time)
        if expiry_time <= now and lab.status not in {LabStatus.provisioning, LabStatus.resuming}:
            due.append((lab, CleanupReason.expiry))
        elif (
            lab.status in {LabStatus.running, LabStatus.provisioning, LabStatus.resuming}
            and _budget_exhausted(lab, now)
        ):
            due.append((lab, CleanupReason.budget))
        elif lab.status == LabStatus.running and lab.last_seen_at and (now - _aware(lab.last_seen_at)).total_seconds() > lab.idle_timeout_minutes * 60:
            due.append((lab, CleanupReason.idle))
    return due


async def enforce_scheduled_labs(db: AsyncSession) -> None:
    now = utcnow()
    rows = (
        await db.scalars(
            select(Lab).where(
                Lab.schedule_enabled.is_(True),
                Lab.status.notin_([LabStatus.expired, LabStatus.terminated, LabStatus.terminating]),
            )
        )
    ).all()

    provision_ids: list[str] = []
    resume_ids: list[str] = []
    stop_ids: list[str] = []
    terminate_ids: list[str] = []

    for lab in rows:
        state = _schedule_state(lab, now)
        if state == "active":
            if lab.status == LabStatus.scheduled and not lab.ec2_instance_id:
                lab.status = LabStatus.provisioning
                db.add(AuditLog(actor="system", action="lab.schedule.launch", resource_id=lab.id, message="Scheduled lab window started"))
                provision_ids.append(lab.id)
            elif lab.status == LabStatus.stopped and lab.ec2_instance_id:
                db.add(AuditLog(actor="system", action="lab.schedule.resume", resource_id=lab.id, message="Scheduled lab window started"))
                resume_ids.append(lab.id)
        elif state == "after_day":
            if lab.status in {LabStatus.running, LabStatus.provisioning, LabStatus.resuming} and lab.ec2_instance_id:
                db.add(AuditLog(actor="system", action="lab.schedule.stop", resource_id=lab.id, message="Scheduled lab window ended for today"))
                stop_ids.append(lab.id)
            elif lab.status in {LabStatus.scheduled, LabStatus.provisioning, LabStatus.resuming}:
                lab.status = LabStatus.scheduled
        elif state == "after":
            if lab.status != LabStatus.terminating:
                if lab.status == LabStatus.running:
                    _accrue_running_time(lab, now)
                lab.status = LabStatus.terminating
                db.add(AuditLog(actor="system", action="lab.schedule.expire", resource_id=lab.id, message="Scheduled lab window ended; queued for termination"))
                terminate_ids.append(lab.id)

    await db.commit()

    for lab_id in provision_ids:
        _schedule_provision_lab(lab_id)
    for lab_id in resume_ids:
        _schedule_resume_lab(lab_id)
    for lab_id in stop_ids:
        _schedule_stop_lab(lab_id, CleanupReason.force)
    for lab_id in terminate_ids:
        _schedule_terminate_lab(lab_id, CleanupReason.expiry)


async def dashboard(db: AsyncSession) -> dict[str, int]:
    async def count_labs(*statuses: LabStatus) -> int:
        return await db.scalar(select(func.count()).select_from(Lab).where(visible_lab_filter(), Lab.status.in_(statuses))) or 0

    now = utcnow()
    rows = (await db.scalars(select(Lab).where(visible_lab_filter(now)))).all()
    running = [lab for lab in rows if lab.status == LabStatus.running]
    attention_statuses = {LabStatus.failed, LabStatus.budget_exceeded, LabStatus.expired, LabStatus.interrupted}

    return {
        "batches": await db.scalar(select(func.count()).select_from(Batch)) or 0,
        "active_labs": await count_labs(LabStatus.running),
        "provisioning_labs": await count_labs(LabStatus.provisioning),
        "terminated_labs": await count_labs(LabStatus.terminated),
        "failed_labs": await count_labs(LabStatus.failed),
        "stopped_labs": await count_labs(LabStatus.stopped),
        "budget_exceeded_labs": await count_labs(LabStatus.budget_exceeded),
        "estimated_running_hourly_cost": sum(lab.hourly_cost or 0 for lab in running),
        "estimated_total_spend": sum(_budget_spend(lab, now) for lab in rows),
        "healthy_labs": len([lab for lab in rows if lab.status == LabStatus.running and lab.access_url and lab.ec2_instance_id]),
        "attention_labs": len([lab for lab in rows if lab.status in attention_statuses]),
    }


async def extend_lab(db: AsyncSession, lab: Lab, hours: float, admin_id: str) -> None:
    if lab.status in {LabStatus.terminated, LabStatus.terminating}:
        raise RuntimeError(f"Lab cannot be extended in status {lab.status.value}")

    now = utcnow()
    previous_expiry = _aware(lab.expiry_time)
    base_time = max(previous_expiry, now)
    lab.expiry_time = base_time + timedelta(hours=hours)
    if lab.schedule_enabled and lab.schedule_start_date and lab.schedule_days:
        local_tz = ZoneInfo(lab.schedule_timezone or "Asia/Kolkata")
        local_expiry = lab.expiry_time.astimezone(local_tz)
        extended_days = max((local_expiry.date() - lab.schedule_start_date).days + 1, lab.schedule_days)
        lab.schedule_days = extended_days
        if local_expiry.date() == lab.schedule_start_date + timedelta(days=extended_days - 1):
            lab.schedule_end_time = local_expiry.strftime("%H:%M")
    if lab.status == LabStatus.expired and lab.ec2_instance_id:
        lab.status = LabStatus.stopped
    if lab.status == LabStatus.running:
        lab.last_seen_at = now
    db.add(
        AuditLog(
            actor=admin_id,
            action="lab.extended",
            resource_id=lab.id,
            message=(
                f"Extended lab by {hours:g} hour(s); "
                f"expiry {previous_expiry.isoformat()} -> {lab.expiry_time.isoformat()}"
            ),
        )
    )
    await db.commit()

    if not lab.ec2_instance_id:
        return

    try:
        await AwsEc2Service(lab.aws_region).update_instance_expiry_tag(
            lab.ec2_instance_id,
            lab_id=lab.id,
            expiry_iso=lab.expiry_time.isoformat(),
        )
    except Exception as exc:
        db.add(AuditLog(actor="system", action="lab.extend.tag_failed", resource_id=lab.id, message=str(exc)))
        await db.commit()


async def add_lab_budget_credit(db: AsyncSession, lab: Lab, amount: float, admin_id: str) -> None:
    if lab.status in {LabStatus.expired, LabStatus.terminated, LabStatus.terminating}:
        raise RuntimeError(f"Lab cannot receive credit in status {lab.status.value}")

    lab.budget_limit = (lab.budget_limit or 0) + amount
    db.add(AuditLog(actor=admin_id, action="lab.budget.credit_added", resource_id=lab.id, message=f"Added ${amount:.2f} credit"))

    if lab.status == LabStatus.budget_exceeded:
        lab.status = LabStatus.stopped
    await db.commit()

    if lab.ec2_instance_id:
        try:
            await AwsEc2Service(lab.aws_region).update_instance_budget_tag(
                lab.ec2_instance_id,
                lab_id=lab.id,
                budget_limit=lab.budget_limit,
            )
        except Exception as exc:
            db.add(AuditLog(actor="system", action="lab.budget.tag_failed", resource_id=lab.id, message=str(exc)))
            await db.commit()

    if lab.status == LabStatus.stopped:
        await resume_lab(db, lab)
