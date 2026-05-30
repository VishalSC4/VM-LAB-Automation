import asyncio

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api.auth import router as auth_router
from app.api.routes import router as api_router
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import SessionLocal, init_db
from app.models.models import Admin
from app.services.labs import resume_pending_provisioning
from app.workers.cleanup import cleanup_loop

settings = get_settings()
log = structlog.get_logger()

app = FastAPI(title=settings.app_name, version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router, prefix="/api")
app.include_router(api_router, prefix="/api")


@app.on_event("startup")
async def startup() -> None:
    await init_db()
    async with SessionLocal() as db:
        existing = await db.scalar(select(Admin).where(Admin.email == settings.admin_bootstrap_email.lower()))
        if not existing:
            db.add(
                Admin(
                    email=settings.admin_bootstrap_email.lower(),
                    password_hash=hash_password(settings.admin_bootstrap_password),
                )
            )
            await db.commit()
    asyncio.create_task(cleanup_loop())
    requeued = await resume_pending_provisioning()
    log.info("cloud_lab_platform_started", environment=settings.environment, requeued_provisioning_labs=requeued)
