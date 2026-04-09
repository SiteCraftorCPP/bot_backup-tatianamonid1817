"""Загрузка списка «Мои заявки» для админа (по полю ответственный).

Раньше делались два запроса get_orders(..., status=в работе|готово, limit=100).
У каждого свой LIMIT в SQL — при >100 заявок в одном статуе хвост терялся.
Сейчас — один запрос без status, лимит повышен, фильтрация по вкладкам на клиенте.
"""

from __future__ import annotations

from bot.api_client import get_orders

# Совпадает с backend list_orders max limit после правки.
ADMIN_MY_ORDERS_LIMIT = 500

# Объединённый вид (как при первом открытии «Мои заявки»): все актуальные назначения.
COMBINED_STATUSES = frozenset({"создана", "в работе", "готово", "отправлена"})


async def load_admin_my_orders_source(user_id: int) -> list[dict]:
    rows = await get_orders(
        responsible_telegram_id=user_id,
        limit=ADMIN_MY_ORDERS_LIMIT,
    )
    return sorted(rows, key=lambda o: str(o.get("created_at") or ""), reverse=True)


def filter_admin_my_orders_rows(rows: list[dict], status: str | None) -> list[dict]:
    """status=None — объединённый список; иначе только заявки с этим статусом."""
    if status is None:
        return [o for o in rows if (o.get("status") or "") in COMBINED_STATUSES]
    return [o for o in rows if (o.get("status") or "") == status]
