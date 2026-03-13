"""Statistics API routes."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, User
from database.session import get_db
from database.models import TZ_UTC3


router = APIRouter()


def _month_range(year: int, month: int) -> tuple[datetime, datetime]:
    """Return (start, end) range for given month in TZ_UTC3."""
    start = datetime(year, month, 1, tzinfo=TZ_UTC3)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=TZ_UTC3)
    else:
        end = datetime(year, month + 1, 1, tzinfo=TZ_UTC3)
    return start, end


@router.get("/summary")
async def stats_summary(
    month: str | None = Query(
        default=None,
        description="Месяц в формате YYYY-MM. Если не указан — текущий месяц.",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Сводная статистика по заявкам за месяц.

    - total_orders: общее количество созданных заявок за месяц
    - by_user: сколько заявок создал каждый пользователь
    - by_admin_completed: сколько заявок завершил каждый админ
      (статусы 'готово' или 'отправлена', учитывается ответственный).
    """
    # Определяем диапазон дат
    now = datetime.now(TZ_UTC3)
    if month:
        try:
            year_str, mon_str = month.split("-")
            year = int(year_str)
            mon = int(mon_str)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="month must be in format YYYY-MM") from exc
    else:
        year = now.year
        mon = now.month

    start, end = _month_range(year, mon)

    # 1) Общее количество заявок за месяц
    stmt_total = select(func.count()).select_from(Order).where(
        Order.created_at >= start,
        Order.created_at < end,
    )
    total_result = await db.execute(stmt_total)
    total_orders = total_result.scalar_one() or 0

    # 2) Кол-во заявок по авторам (пользователям)
    stmt_by_user = (
        select(
            User.telegram_id,
            User.username,
            func.count(Order.id).label("orders_count"),
        )
        .join(User, Order.author_id == User.id)
        .where(
            Order.created_at >= start,
            Order.created_at < end,
        )
        .group_by(User.id)
        .order_by(func.count(Order.id).desc())
    )
    res_by_user = await db.execute(stmt_by_user)
    by_user = [
        {
            "telegram_id": row.telegram_id,
            "username": row.username,
            "orders_count": row.orders_count,
        }
        for row in res_by_user
    ]

    # 3) Кол-во выполненных заявок по админам (ответственным)
    done_statuses = ("готово", "отправлена")
    stmt_by_admin = (
        select(
            Order.responsible_telegram_id,
            Order.responsible_username,
            func.count(Order.id).label("orders_count"),
        )
        .where(
            Order.created_at >= start,
            Order.created_at < end,
            Order.status.in_(done_statuses),
            Order.responsible_telegram_id.is_not(None),
        )
        .group_by(Order.responsible_telegram_id, Order.responsible_username)
        .order_by(func.count(Order.id).desc())
    )
    res_by_admin = await db.execute(stmt_by_admin)
    by_admin_completed = [
        {
            "telegram_id": row.responsible_telegram_id,
            "username": row.responsible_username,
            "orders_count": row.orders_count,
        }
        for row in res_by_admin
    ]

    return {
        "year": year,
        "month": mon,
        "total_orders": total_orders,
        "by_user": by_user,
        "by_admin_completed": by_admin_completed,
    }

