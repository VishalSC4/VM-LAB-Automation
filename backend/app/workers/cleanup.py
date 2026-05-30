import asyncio
from datetime import datetime, timedelta, timezone

import structlog

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.models import CleanupReason
from app.services.labs import cleanup_terminal_lab_artifacts, enforce_scheduled_labs, find_due_labs, refresh_active_lab_presence, retry_terminating_labs, stop_lab, terminate_lab

log = structlog.get_logger()


def seconds_until_next_cleanup_tick(interval_seconds: int) -> float:
    now = datetime.now(timezone.utc)
    next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    return max(1, min(interval_seconds, (next_minute - now).total_seconds()))


async def cleanup_loop() -> None:
    settings = get_settings()
    while True:
        try:
            async with SessionLocal() as db:
                await refresh_active_lab_presence(db)
                await enforce_scheduled_labs(db)
                await retry_terminating_labs(db)
                await cleanup_terminal_lab_artifacts(db)
                for lab, reason in await find_due_labs(db):
                    if reason in {CleanupReason.budget, CleanupReason.idle}:
                        await stop_lab(db, lab, reason)
                    else:
                        await terminate_lab(db, lab, reason)
        except Exception as exc:
            log.exception("cleanup_loop_failed", error=str(exc))
        await asyncio.sleep(seconds_until_next_cleanup_tick(settings.cleanup_interval_seconds))
