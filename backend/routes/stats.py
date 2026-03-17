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


def _parse_ddmmyy(value: str) -> datetime:
    """Парсинг даты в формате дд.мм.гг в TZ_UTC3 (00:00)."""
    try:
        dt = datetime.strptime(value.strip(), "%d.%m.%y")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="date must be in format dd.mm.yy") from exc
    return dt.replace(tzinfo=TZ_UTC3)


@router.get("/summary")
async def stats_summary(
    month: str | None = Query(
        default=None,
        description="Месяц в формате YYYY-MM. Если не указан — текущий месяц.",
    ),
    date_from: str | None = Query(
        default=None,
        description="Дата начала периода в формате дд.мм.гг (включительно).",
    ),
    date_to: str | None = Query(
        default=None,
        description="Дата конца периода в формате дд.мм.гг (включительно).",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Сводная статистика по заявкам за месяц или период.

    - total_orders: общее количество созданных заявок за месяц
    - by_user: сколько заявок создал каждый пользователь
    - by_admin_completed: сколько заявок завершил каждый админ
      (статусы 'готово' или 'отправлена', учитывается ответственный).
    """
    # Определяем диапазон дат
    now = datetime.now(TZ_UTC3)
    if (date_from is not None) or (date_to is not None):
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to must be provided together")
        start = _parse_ddmmyy(date_from)
        end_inclusive = _parse_ddmmyy(date_to)
        if start > end_inclusive:
            raise HTTPException(status_code=400, detail="date_from must be <= date_to")
        # end — следующий день (исключая), чтобы включить весь date_to
        end = end_inclusive + timedelta(days=1)
        year = start.year
        mon = start.month
    elif month:
        try:
            year_str, mon_str = month.split("-")
            year = int(year_str)
            mon = int(mon_str)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="month must be in format YYYY-MM") from exc
    else:
        year = now.year
        mon = now.month

    if (date_from is None) and (date_to is None):
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
        "date_from": date_from,
        "date_to": date_to,
        "total_orders": total_orders,
        "by_user": by_user,
        "by_admin_completed": by_admin_completed,
    }

