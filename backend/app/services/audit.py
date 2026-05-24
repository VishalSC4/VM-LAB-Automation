from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AuditLog


async def audit(db: AsyncSession, actor: str, action: str, message: str, resource_id: str | None = None) -> None:
    db.add(AuditLog(actor=actor, action=action, message=message, resource_id=resource_id))
    await db.commit()

