"""Audit logging service."""
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AuditLog


async def log_action(
    db: AsyncSession,
    user_id: int | None = None,
    telegram_id: int | None = None,
    action: str = "",
    entity_type: str | None = None,
    entity_id: int | None = None,
    details: str | None = None,
):
    """Write audit log entry."""
    entry = AuditLog(
        user_id=user_id,
        telegram_id=telegram_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    )
    db.add(entry)
