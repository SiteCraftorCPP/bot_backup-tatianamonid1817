"""Order creation and numbering logic."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, OrderItem, User, Product
from database.session import AsyncSessionLocal
from backend.services.excel_service import generate_order_excel, get_excel_filename
from backend.services.google_sheets_service import sheets_service


TZ_UTC3 = timezone(timedelta(hours=3))


async def generate_order_number(db: AsyncSession) -> str:
    """Generate unique order number: YYYY-MM-NNN (e.g. 2026-02-025)."""
    today = datetime.now(TZ_UTC3)
    prefix = today.strftime("%Y-%m")
    stmt = select(func.count(Order.id)).where(Order.number.like(f"{prefix}-%"))
    result = await db.execute(stmt)
    count = result.scalar() or 0
    return f"{prefix}-{count + 1:03d}"


async def get_or_create_user(
    db: AsyncSession,
    telegram_id: int,
    username: str | None = None,
    full_name: str | None = None,
) -> User:
    """Get or create user by telegram_id."""
    from sqlalchemy import select
    stmt = select(User).where(User.telegram_id == telegram_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user:
        if username is not None:
            user.username = username
        if full_name is not None:
            user.full_name = full_name
        return user
    user = User(
        telegram_id=telegram_id,
        username=username,
        full_name=full_name,
        role="user",
    )
    db.add(user)
    await db.flush()
    return user
