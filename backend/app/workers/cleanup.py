import asyncio
import structlog

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.models import CleanupReason
from app.services.labs import enforce_scheduled_labs, find_due_labs, refresh_active_lab_presence, retry_terminating_labs, stop_lab, terminate_lab

log = structlog.get_logger()


async def cleanup_loop() -> None:
    settings = get_settings()
    while True:
        try:
            async with SessionLocal() as db:
                await refresh_active_lab_presence(db)
                await enforce_scheduled_labs(db)
                await retry_terminating_labs(db)
                for lab, reason in await find_due_labs(db):
                    if reason in {CleanupReason.budget, CleanupReason.idle}:
                        await stop_lab(db, lab, reason)
                    else:
                        await terminate_lab(db, lab, reason)
        except Exception as exc:
            log.exception("cleanup_loop_failed", error=str(exc))
        await asyncio.sleep(settings.cleanup_interval_seconds)
