import asyncio
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import RetryError

from app.core.config import get_settings
from app.core.security import generate_windows_password
from app.models.models import AuditLog, Batch, CleanupJob, CleanupReason, Lab, LabStatus, uuid_str
from app.schemas.schemas import BatchCreate
from app.services.aws_ec2 import AwsEc2Service
from app.services.guacamole import GuacamoleService
from app.services.pricing import get_estimated_spot_windows_price, get_hourly_windows_price
from app.services.secrets import delete_lab_password, get_lab_password, store_lab_password

TERMINATED_LAB_VISIBLE_HOURS = 24


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
    if len(lab.username) <= 20:
        return lab.username
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
    hostname = await _reachable_rdp_host(lab, attempts=6, delay_seconds=5)
    password = await get_lab_password(lab.aws_region, lab.password_secret_ref, lab.password_ciphertext)
    await GuacamoleService().update_rdp_connection(
        lab.guacamole_connection_id,
        hostname=hostname,
        username=_rdp_username(lab),
        password=password,
        domain=windows_hostname,
    )


async def _delete_lab_password_secret(lab: Lab, errors: list[str] | None = None) -> None:
    try:
        deleted = await delete_lab_password(lab.aws_region, lab.password_secret_ref)
        if deleted:
            lab.password_secret_ref = "deleted"
            lab.password_ciphertext = ""
    except Exception as exc:
        if errors is not None:
            errors.append(f"secret: {exc}")


async def create_batch(db: AsyncSession, payload: BatchCreate, admin_id: str) -> Batch:
    settings = get_settings()
    on_demand_hourly_cost = await get_hourly_windows_price(payload.aws_region, payload.instance_type, settings.pricing_cache_ttl_seconds)
    spot_hourly_cost = await get_estimated_spot_windows_price(payload.aws_region, payload.instance_type, settings.pricing_cache_ttl_seconds)
    requested_market = _requested_market(on_demand_hourly_cost, spot_hourly_cost)
    hourly_cost = spot_hourly_cost if requested_market == "spot" and spot_hourly_cost is not None else on_demand_hourly_cost
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
    await db.commit()
    for lab in labs:
        if lab.status == LabStatus.provisioning:
            asyncio.create_task(provision_lab(lab.id))
    return batch


async def provision_lab(lab_id: str) -> None:
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        lab = await db.get(Lab, lab_id)
        if not lab:
            return
        raw_password = (
            await get_lab_password(lab.aws_region, lab.password_secret_ref, lab.password_ciphertext)
            if lab.password_secret_ref.startswith("local-dev:")
            else lab.password_ciphertext
        )
        try:
            secret_ref, stored_value = await store_lab_password(lab.aws_region, lab.id, raw_password)
            lab.password_secret_ref = secret_ref
            lab.password_ciphertext = stored_value
            await db.commit()

            ec2 = AwsEc2Service(lab.aws_region)
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
            await db.commit()

            guac_host = await _reachable_rdp_host(lab)
            connection_id, access_url = await GuacamoleService().create_rdp_connection(
                name=f"{lab.owner_label}-{lab.id[:8]}",
                hostname=guac_host,
                username=_rdp_username(lab),
                password=raw_password,
                domain=instance.windows_hostname,
            )
            await GuacamoleService().create_user_mapping(username=lab.username, password=raw_password, connection_id=connection_id)
            lab.guacamole_connection_id = connection_id
            lab.access_url = access_url
            lab.status = LabStatus.running
            lab.last_seen_at = utcnow()
            lab.last_started_at = lab.last_seen_at
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
            await _delete_lab_password_secret(lab)
            lab.status = LabStatus.failed
            message = _provisioning_error_message(exc)
            db.add(AuditLog(actor="system", action="lab.failed", resource_id=lab.id, message=message))
            await db.commit()


def _provisioning_error_message(exc: Exception) -> str:
    if isinstance(exc, RetryError) and exc.last_attempt.failed:
        return str(exc.last_attempt.exception())
    return str(exc)


async def terminate_lab(db: AsyncSession, lab: Lab, reason: CleanupReason) -> None:
    if reason in {CleanupReason.budget, CleanupReason.idle}:
        await stop_lab(db, lab, reason)
        return

    if lab.status in {LabStatus.terminated, LabStatus.terminating, LabStatus.expired, LabStatus.interrupted}:
        return
    lab.status = LabStatus.terminating
    job = CleanupJob(lab_id=lab.id, reason=reason, status="running")
    db.add(job)
    await db.commit()

    errors: list[str] = []
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
            await AwsEc2Service(lab.aws_region).terminate_instance(lab.ec2_instance_id, lab_id=lab.id)
        except Exception as exc:
            errors.append(f"ec2: {exc}")
    if not errors:
        await _delete_lab_password_secret(lab, errors)

    lab.status = LabStatus.terminated if not errors else LabStatus.failed
    lab.terminated_at = utcnow()
    job.status = "finished" if not errors else "failed"
    job.finished_at = utcnow()
    job.message = "; ".join(errors) if errors else "Cleanup completed"
    db.add(AuditLog(actor="system", action=f"lab.cleanup.{reason.value}", resource_id=lab.id, message=job.message))
    await db.commit()


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
            if _is_spot_lab(lab):
                await AwsEc2Service(lab.aws_region).terminate_instance(lab.ec2_instance_id, lab_id=lab.id)
            else:
                await AwsEc2Service(lab.aws_region).stop_instance(lab.ec2_instance_id, lab_id=lab.id)
        except Exception as exc:
            errors.append(f"ec2: {exc}")

    if errors:
        lab.status = LabStatus.failed
    elif _is_spot_lab(lab):
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
        if not errors:
            await _delete_lab_password_secret(lab, errors)
        lab.status = LabStatus.failed if errors else (LabStatus.interrupted if reason == CleanupReason.orphan else LabStatus.terminated)
        lab.terminated_at = utcnow()
    elif reason == CleanupReason.budget:
        lab.status = LabStatus.budget_exceeded
    else:
        lab.status = LabStatus.stopped

    job.status = "finished" if not errors else "failed"
    job.finished_at = utcnow()
    action = "terminated" if _is_spot_lab(lab) and not errors else "stopped"
    job.message = "; ".join(errors) if errors else f"Instance {action} due to {reason.value}"
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
    if _is_spot_lab(lab):
        raise RuntimeError("Spot labs cannot be resumed after stop or interruption")
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
        lab.status = LabStatus.stopped
        db.add(AuditLog(actor="system", action="lab.resume.failed", resource_id=lab.id, message=str(exc)))
    await db.commit()


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
        if _is_spot_lab(lab):
            _accrue_running_time(lab, utcnow())
            lab.status = LabStatus.interrupted
            lab.interrupted_at = utcnow()
            db.add(AuditLog(actor="system", action="lab.spot.interrupted", resource_id=lab.id, message="Spot lab instance stopped or was interrupted"))
            await db.commit()
            raise RuntimeError("Spot lab has ended")
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
                lab.status = LabStatus.interrupted
                lab.interrupted_at = now
                await _delete_lab_password_secret(lab)
                db.add(AuditLog(actor="system", action="lab.spot.interrupted", resource_id=lab.id, message=f"EC2 Spot instance is {state}"))
                await db.commit()
                continue
        expiry_time = _aware(lab.expiry_time)
        if expiry_time <= now:
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

    for lab in rows:
        state = _schedule_state(lab, now)
        if state == "active":
            if lab.status == LabStatus.scheduled and not lab.ec2_instance_id:
                lab.status = LabStatus.provisioning
                db.add(AuditLog(actor="system", action="lab.schedule.launch", resource_id=lab.id, message="Scheduled lab window started"))
                await db.commit()
                asyncio.create_task(provision_lab(lab.id))
            elif lab.status == LabStatus.stopped and lab.ec2_instance_id:
                db.add(AuditLog(actor="system", action="lab.schedule.resume", resource_id=lab.id, message="Scheduled lab window started"))
                await db.commit()
                await resume_lab(db, lab)
        elif state == "after_day":
            if lab.status == LabStatus.running:
                db.add(AuditLog(actor="system", action="lab.schedule.stop", resource_id=lab.id, message="Scheduled lab window ended for today"))
                await db.commit()
                await stop_lab(db, lab, CleanupReason.force)
            elif lab.status == LabStatus.scheduled:
                lab.status = LabStatus.scheduled
                await db.commit()
        elif state == "after":
            await terminate_lab(db, lab, CleanupReason.expiry)


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

    if lab.status == LabStatus.stopped:
        await resume_lab(db, lab)
