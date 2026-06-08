import base64

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi import Query
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_admin
from app.core.config import get_settings
from app.db.session import get_db
from app.models.models import Admin, AuditLog, Batch, CleanupReason, Lab, LabStatus
from app.schemas.schemas import BatchCreate, BatchOut, DashboardOut, LabBudgetCreditIn, LabCredentialsExportOut, LabCredentialOut, LabExtendIn, LabOut, LogOut, StudentLabOut, StudentLoginIn
from app.services.labs import add_lab_budget_credit, create_batch, dashboard, extend_lab, prepare_lab_session, recover_failed_lab, resume_lab, stop_lab, terminate_lab, utcnow, visible_lab_filter
from app.services.secrets import get_lab_password

router = APIRouter(tags=["cloud-labs"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/public/session/{connection_id}", include_in_schema=False)
async def session_redirect(
    connection_id: str,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    lab = await db.scalar(select(Lab).where(Lab.guacamole_connection_id == connection_id))
    redirect_connection_id = connection_id
    if lab:
        try:
            await prepare_lab_session(db, lab)
            redirect_connection_id = lab.guacamole_connection_id or connection_id
        except RuntimeError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
    datasource = get_settings().guacamole_datasource
    raw_identifier = f"{redirect_connection_id}\x00c\x00{datasource}".encode()
    encoded = base64.urlsafe_b64encode(raw_identifier).decode().rstrip("=")
    return RedirectResponse(url=f"/guacamole/#/client/{encoded}")


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard_api(_: Admin = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return await dashboard(db)


def lab_progress(lab: Lab) -> list[str]:
    steps: list[str] = []
    if lab.ec2_instance_id:
        steps.append("EC2 instance created")
    else:
        steps.append("Waiting for EC2 instance")
    if lab.private_ip or lab.public_ip:
        steps.append("Windows network ready")
    if lab.guacamole_connection_id:
        steps.append("Guacamole connection ready")
    if lab.access_url:
        steps.append("Browser access ready")
    if lab.status == LabStatus.running:
        steps.append("Lab is running")
    elif lab.status == LabStatus.scheduled:
        steps.append("Waiting for scheduled window")
    elif lab.status == LabStatus.failed:
        steps.append("Provisioning failed")
    elif lab.status == LabStatus.stopped:
        steps.append("Lab is stopped")
    elif lab.status == LabStatus.interrupted:
        steps.append("Spot lab ended")
    return steps


@router.post("/batches", response_model=BatchOut)
async def create_batch_api(
    payload: BatchCreate,
    admin: Admin = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await create_batch(db, payload, admin.id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/batches", response_model=list[BatchOut])
async def list_batches(_: Admin = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Batch).order_by(desc(Batch.created_at)).limit(100))).all()


@router.get("/labs", response_model=list[LabOut])
async def list_labs(_: Admin = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Lab).where(visible_lab_filter()).order_by(desc(Lab.created_at)).limit(500))).all()


def _credentials_share_text(credentials: list[LabCredentialOut]) -> str:
    return "\n\n".join(
        [
            "\n".join(
                [
                    f"Lab: {credential.owner_label}",
                    f"Status: {credential.status.value}",
                    f"URL: {credential.url or 'Access pending'}",
                    f"Username: {credential.username}",
                    f"Password: {credential.password}",
                    f"Expires: {credential.expires.isoformat()}",
                ]
            )
            for credential in credentials
        ]
    )


@router.get("/labs/recent-credentials", response_model=LabCredentialsExportOut)
async def recent_lab_credentials(
    _: Admin = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
    include_terminated: bool = False,
):
    query = select(Lab).where(visible_lab_filter()).order_by(desc(Lab.created_at)).limit(limit)
    if not include_terminated:
        query = query.where(Lab.status.notin_([LabStatus.terminated, LabStatus.terminating, LabStatus.interrupted]))

    labs = (await db.scalars(query)).all()
    credentials: list[LabCredentialOut] = []
    for lab in labs:
        credentials.append(
            LabCredentialOut(
                lab_id=lab.id,
                owner_label=lab.owner_label,
                status=lab.status,
                url=lab.access_url,
                username=lab.username,
                password=await get_lab_password(lab.aws_region, lab.password_secret_ref, lab.password_ciphertext),
                expires=lab.expiry_time,
            )
        )

    return LabCredentialsExportOut(
        count=len(credentials),
        generated_at=utcnow(),
        credentials=credentials,
        share_text=_credentials_share_text(credentials),
    )


@router.post("/labs/{lab_id}/terminate", response_model=LabOut)
async def force_terminate_lab(
    lab_id: str,
    background: BackgroundTasks,
    _: Admin = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    lab = await db.get(Lab, lab_id)
    if not lab:
        raise HTTPException(status_code=404, detail="Lab not found")
    background.add_task(_terminate_by_id, lab.id)
    return lab


@router.post("/labs/{lab_id}/resume", response_model=LabOut)
async def resume_stopped_lab(
    lab_id: str,
    background: BackgroundTasks,
    _: Admin = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    lab = await db.get(Lab, lab_id)
    if not lab:
        raise HTTPException(status_code=404, detail="Lab not found")
    if lab.status == LabStatus.stopped:
        background.add_task(_resume_by_id, lab.id)
    elif lab.status == LabStatus.failed and lab.ec2_instance_id:
        background.add_task(_recover_by_id, lab.id)
    elif lab.status != LabStatus.running:
        raise HTTPException(status_code=409, detail=f"Lab cannot be resumed from status {lab.status.value}")
    return lab


@router.post("/labs/{lab_id}/stop", response_model=LabOut)
async def stop_running_lab(
    lab_id: str,
    background: BackgroundTasks,
    _: Admin = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    lab = await db.get(Lab, lab_id)
    if not lab:
        raise HTTPException(status_code=404, detail="Lab not found")
    if lab.status == LabStatus.running:
        background.add_task(_stop_by_id, lab.id)
    elif lab.status not in {LabStatus.stopped, LabStatus.budget_exceeded}:
        raise HTTPException(status_code=409, detail=f"Lab cannot be stopped from status {lab.status.value}")
    return lab


@router.post("/labs/{lab_id}/extend", response_model=LabOut)
async def extend_existing_lab(
    lab_id: str,
    payload: LabExtendIn,
    admin: Admin = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    lab = await db.get(Lab, lab_id)
    if not lab:
        raise HTTPException(status_code=404, detail="Lab not found")
    try:
        await extend_lab(db, lab, payload.hours, admin.id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.refresh(lab)
    return lab


@router.get("/labs/{lab_id}/progress")
async def lab_progress_api(lab_id: str, _: Admin = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    lab = await db.get(Lab, lab_id)
    if not lab:
        raise HTTPException(status_code=404, detail="Lab not found")
    return {"steps": lab_progress(lab)}


@router.post("/labs/{lab_id}/budget-credit", response_model=LabOut)
async def add_budget_credit(
    lab_id: str,
    payload: LabBudgetCreditIn,
    admin: Admin = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    lab = await db.get(Lab, lab_id)
    if not lab:
        raise HTTPException(status_code=404, detail="Lab not found")
    try:
        await add_lab_budget_credit(db, lab, payload.amount, admin.id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.refresh(lab)
    return lab


@router.get("/labs/{lab_id}/credentials")
async def lab_credentials(
    lab_id: str,
    background: BackgroundTasks,
    _: Admin = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    lab = await db.get(Lab, lab_id)
    if not lab:
        raise HTTPException(status_code=404, detail="Lab not found")
    if lab.status == LabStatus.stopped:
        background.add_task(_resume_by_id, lab.id)
    return {
        "url": lab.access_url,
        "username": lab.username,
        "password": await get_lab_password(lab.aws_region, lab.password_secret_ref, lab.password_ciphertext),
        "expires_at": lab.expiry_time,
    }


@router.post("/student/login", response_model=StudentLabOut)
async def student_login(payload: StudentLoginIn, background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    lab = await db.scalar(select(Lab).where(visible_lab_filter(), Lab.username == payload.username).order_by(desc(Lab.created_at)))
    if not lab:
        raise HTTPException(status_code=401, detail="Invalid lab username or password")
    password = await get_lab_password(lab.aws_region, lab.password_secret_ref, lab.password_ciphertext)
    if password != payload.password:
        raise HTTPException(status_code=401, detail="Invalid lab username or password")
    progress = lab_progress(lab)
    if lab.status == LabStatus.stopped:
        background.add_task(_resume_by_id, lab.id)
        progress.append("Starting lab automatically")
    return StudentLabOut(lab=lab, access_url=lab.access_url, username=lab.username, password=password, progress=progress)


async def _terminate_by_id(lab_id: str) -> None:
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        lab = await db.get(Lab, lab_id)
        if lab:
            await terminate_lab(db, lab, CleanupReason.force)


async def _resume_by_id(lab_id: str) -> None:
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        lab = await db.get(Lab, lab_id)
        if lab:
            await resume_lab(db, lab)


async def _recover_by_id(lab_id: str) -> None:
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        lab = await db.get(Lab, lab_id)
        if lab:
            await recover_failed_lab(db, lab)


async def _stop_by_id(lab_id: str) -> None:
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        lab = await db.get(Lab, lab_id)
        if lab:
            await stop_lab(db, lab, CleanupReason.force)


@router.get("/logs", response_model=list[LogOut])
async def logs(_: Admin = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(AuditLog).order_by(desc(AuditLog.created_at)).limit(300))).all()
