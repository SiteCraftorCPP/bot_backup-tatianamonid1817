"""Загрузка списка «Мои заявки» для админа (по полю ответственный).

Раньше два запроса по статусам с малым limit теряли хвост; затем один запрос с limit=500
мог отрезать старые заявки при >500 назначений. Сейчас — постраничная загрузка (offset)
без status, фильтрация по вкладкам на клиенте.
"""

from __future__ import annotations

from bot.api_client import get_orders, try_repair_responsible_telegram_self

# Размер страницы при обходе GET /orders/ (max le на backend).
ADMIN_MY_ORDERS_PAGE = 500
# Защита от бесконечного цикла (500 * 200 = 100k заявок на одного ответственного).
ADMIN_MY_ORDERS_MAX_PAGES = 200

# Объединённый вид (вкладка «Все» у админа в «Мои заявки»):
# только то, что требует работы/контроля (без «отправлена» и без «создана»).
COMBINED_STATUSES = frozenset({"в работе", "готово"})


async def load_admin_my_orders_source(user_id: int) -> list[dict]:
    """Все заявки с ответственным user_id: постранично по offset, без потери хвоста за limit.

    Один запрос с limit=500 мог отрезать старые назначения, если у админа >500 строк в выборке БД.
    """
    await try_repair_responsible_telegram_self(user_id)

    merged: list[dict] = []
    seen: set[int] = set()
    offset = 0
    for _ in range(ADMIN_MY_ORDERS_MAX_PAGES):
        batch = await get_orders(
            responsible_telegram_id=user_id,
            limit=ADMIN_MY_ORDERS_PAGE,
            offset=offset,
        )
        if not batch:
            break
        for o in batch:
            try:
                oid = int(o.get("id"))
            except (TypeError, ValueError):
                continue
            if oid in seen:
                continue
            seen.add(oid)
            merged.append(o)
        if len(batch) < ADMIN_MY_ORDERS_PAGE:
            break
        offset += ADMIN_MY_ORDERS_PAGE
    return sorted(merged, key=lambda o: str(o.get("created_at") or ""), reverse=True)


def filter_admin_my_orders_rows(rows: list[dict], status: str | None) -> list[dict]:
    """status=None — объединённый список; иначе только заявки с этим статусом."""
    if status is None:
        return [
            o
            for o in rows
            if (str(o.get("status") or "").strip()) in COMBINED_STATUSES
        ]
    st = str(status).strip()
    return [o for o in rows if (str(o.get("status") or "").strip()) == st]
