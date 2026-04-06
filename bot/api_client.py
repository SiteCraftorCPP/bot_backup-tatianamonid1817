"""HTTP client for Backend API."""
import logging
import httpx
from config import get_settings

logger = logging.getLogger(__name__)


async def search_products(q: str, limit: int = 20) -> list[dict]:
    """Search products by query."""
    url = f"{get_settings().BACKEND_URL}/products/search"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params={"q": q, "limit": limit})
        r.raise_for_status()
        return r.json()


async def get_product(product_id: int) -> dict | None:
    """Get product by ID."""
    url = f"{get_settings().BACKEND_URL}/products/{product_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def create_order(data: dict) -> dict:
    """Create order via API."""
    url = f"{get_settings().BACKEND_URL}/orders/"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=data)
        r.raise_for_status()
        return r.json()


async def get_orders(
    author_telegram_id: int | None = None,
    responsible_telegram_id: int | None = None,
    status: str | None = None,
    admin: bool = False,
    limit: int = 50,
    offset: int = 0,
    *,
    include_deleted: bool = False,
    deleted_only: bool = False,
) -> list[dict]:
    """Get orders list.
    - author_telegram_id: заявки, созданные этим пользователем
    - responsible_telegram_id: заявки, где этот админ ответственный
    - include_deleted / deleted_only: только для admin-запросов (история / корзина)
    """
    url = f"{get_settings().BACKEND_URL}/orders/"
    params: dict[str, object] = {"limit": limit, "offset": offset}
    if author_telegram_id is not None:
        params["author_telegram_id"] = author_telegram_id
    if responsible_telegram_id is not None:
        params["responsible_telegram_id"] = responsible_telegram_id
    if status:
        params["status"] = status
    if admin:
        params["admin"] = True
    if include_deleted:
        params["include_deleted"] = True
    if deleted_only:
        params["deleted_only"] = True
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def purge_trash_orders(
    requester_telegram_id: int,
    ids: list[int] | None = None,
) -> dict:
    """Окончательно удалить заявки из корзины (ids=None или [] — все)."""
    url = f"{get_settings().BACKEND_URL}/orders/trash/purge"
    params = {"requester_telegram_id": requester_telegram_id}
    payload = {"ids": ids}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(url, params=params, json=payload)
        r.raise_for_status()
        return r.json()


async def purge_trash_order_one(order_id: int, requester_telegram_id: int) -> dict:
    url = f"{get_settings().BACKEND_URL}/orders/{order_id}/purge"
    params = {"requester_telegram_id": requester_telegram_id}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.delete(url, params=params)
        r.raise_for_status()
        return r.json()


async def get_order(order_id: int) -> dict | None:
    """Get order by ID."""
    url = f"{get_settings().BACKEND_URL}/orders/{order_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def add_order_attachment(
    order_id: int,
    *,
    author_telegram_id: int,
    telegram_file_id: str,
    file_name: str | None = None,
) -> dict:
    """Зарегистрировать дополнительный файл к заявке (только автор заявки)."""
    url = f"{get_settings().BACKEND_URL}/orders/{order_id}/attachments"
    params = {"author_telegram_id": author_telegram_id}
    payload = {"telegram_file_id": telegram_file_id, "file_name": file_name}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, params=params, json=payload)
        r.raise_for_status()
        return r.json()


async def get_order_excel(order_id: int) -> bytes:
    """Download order Excel file."""
    url = f"{get_settings().BACKEND_URL}/orders/{order_id}/excel"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


async def get_markznak_order_excel(order_id: int) -> bytes:
    """Download extended MarkZnak Excel file for order."""
    url = f"{get_settings().BACKEND_URL}/orders/{order_id}/markznak_excel"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


async def get_template_legal_entities(article: str) -> list[str]:
    """Список юр. лиц для повторного товара по артикулу/наименованию (шаг по ТЗ)."""
    url = f"{get_settings().BACKEND_URL}/products/template/legal_entities"
    params: dict[str, str] = {"article": article.strip()}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return list(r.json())


async def get_template_countries(
    article: str,
    *,
    legal_entity: str | None = None,
) -> list[str]:
    """Список стран для артикула и выбранного ЮЛ (шаг по ТЗ)."""
    url = f"{get_settings().BACKEND_URL}/products/template/countries"
    params: dict[str, str] = {"article": article.strip()}
    if legal_entity and legal_entity.strip():
        params["legal_entity"] = legal_entity.strip()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return list(r.json())


async def get_template_excel(
    article: str,
    category: str | None = None,
    legal_entity: str | None = None,
    country: str | None = None,
) -> bytes:
    """Получить шаблон Excel (макет одежда/обувь + отбор по артикулу, ЮЛ, стране)."""
    url = f"{get_settings().BACKEND_URL}/products/template"
    params: dict[str, str] = {"article": article.strip()}
    if category:
        params["category"] = category
    if legal_entity and legal_entity.strip():
        params["legal_entity"] = legal_entity.strip()
    if country and country.strip():
        params["country"] = country.strip()
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.content


async def get_new_template_excel(
    category: str | None = None,
    legal_entity: str | None = None,
    brand: str | None = None,
    country: str | None = None,
    target_gender: str | None = None,
) -> bytes:
    """Получить пустой пользовательский шаблон Excel для новых товаров.

    Для одежды используется старый шаблон, для обуви — расширенный обувной.
    """
    url = f"{get_settings().BACKEND_URL}/products/new_template"
    params: dict[str, str] = {}
    if category:
        params["category"] = category
    if legal_entity:
        params["legal_entity"] = legal_entity
    if brand:
        params["brand"] = brand
    if country:
        params["country"] = country
    if target_gender:
        params["target_gender"] = target_gender
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, params=params or None)
        r.raise_for_status()
        return r.content


async def create_order_from_template(
    file_bytes: bytes,
    author_telegram_id: int,
    author_username: str | None = None,
    author_full_name: str | None = None,
    order_type: str | None = None,
    ms_order_number: str | None = None,
    comment: str | None = None,
) -> dict:
    """Создать заявку из заполненного шаблона."""
    url = f"{get_settings().BACKEND_URL}/orders/from_template"
    data = {
        "author_telegram_id": str(author_telegram_id),
        "author_username": author_username or "",
        "author_full_name": author_full_name or "",
        "order_type": order_type or "",
        "ms_order_number": ms_order_number or "",
        "comment": comment or "",
    }
    files = {"file": ("template.xlsx", file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, data=data, files=files)
        if r.status_code == 400:
            try:
                body = r.json()
                detail = body.get("detail", r.text)
            except Exception:
                detail = r.text or "Ошибка запроса"
            if isinstance(detail, list) and detail:
                detail = detail[0]
            if isinstance(detail, dict):
                detail = str(detail)
            raise ValueError(str(detail))
        r.raise_for_status()
        return r.json()


async def get_brands(legal_entity: str) -> list[str]:
    """Получить список брендов по юридическому лицу."""
    url = f"{get_settings().BACKEND_URL}/products/brands"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params={"legal_entity": legal_entity})
        r.raise_for_status()
        data = r.json()
        # Гарантируем список строк
        return [str(b) for b in data]


async def update_order(
    order_id: int,
    status: str | None = None,
    yandex_link: str | None = None,
    responsible_telegram_id: int | None = None,
    responsible_username: str | None = None,
) -> dict | None:
    """Update order (status, yandex_link, responsible)."""
    url = f"{get_settings().BACKEND_URL}/orders/{order_id}"
    payload = {}
    if status is not None:
        payload["status"] = status
    if yandex_link is not None:
        payload["yandex_link"] = yandex_link
    if responsible_telegram_id is not None:
        payload["responsible_telegram_id"] = responsible_telegram_id
    if responsible_username is not None:
        payload["responsible_username"] = responsible_username
    if not payload:
        return None
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.patch(url, json=payload)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def set_order_comment(order_id: int, comment: str | None) -> dict | None:
    """Обновить только комментарий заявки (null в JSON сбрасывает в БД)."""
    url = f"{get_settings().BACKEND_URL}/orders/{order_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.patch(url, json={"comment": comment})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def upsert_user(
    telegram_id: int,
    username: str | None = None,
    full_name: str | None = None,
    role: str = "user",
) -> dict:
    """Создать или обновить пользователя и назначить ему роль."""
    url = f"{get_settings().BACKEND_URL}/users/"
    payload: dict[str, object] = {
        "telegram_id": telegram_id,
        "role": role,
    }
    if username is not None:
        payload["username"] = username
    if full_name is not None:
        payload["full_name"] = full_name
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()


async def get_user(telegram_id: int) -> dict | None:
    """Получить пользователя по telegram_id."""
    url = f"{get_settings().BACKEND_URL}/users/{telegram_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def list_admins() -> list[dict]:
    """Получить список администраторов (role=admin)."""
    url = f"{get_settings().BACKEND_URL}/users/"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, params={"role": "admin"})
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return []
        return data


async def admin_telegram_ids_for_notify() -> list[int]:
    """Кому слать админ-уведомления: ADMIN_IDS из .env + все role=admin из БД.

    /add_admin добавляет только в БД, без правки .env — без этого merge новый админ
    не получал бы личные уведомления о заявках.
    """
    settings = get_settings()
    ids: set[int] = set(settings.admin_ids_list)
    try:
        for row in await list_admins():
            tid = row.get("telegram_id")
            if tid is None:
                continue
            try:
                ids.add(int(tid))
            except (TypeError, ValueError):
                continue
    except Exception as e:  # noqa: BLE001
        logger.warning("list_admins failed, using ADMIN_IDS only: %s", e)
    return sorted(ids)


async def delete_user(telegram_id: int) -> bool:
    """Удалить пользователя по telegram_id."""
    url = f"{get_settings().BACKEND_URL}/users/{telegram_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.delete(url)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True


async def register_order_telegram_posting(
    order_id: int, chat_id: int, message_id: int
) -> None:
    """Сохранить chat_id/message_id карточки МаркЗнак у админа (переживает перезапуск бота)."""
    url = f"{get_settings().BACKEND_URL}/orders/{order_id}/telegram_postings"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            url, json={"chat_id": chat_id, "message_id": message_id}
        )
        r.raise_for_status()


async def list_order_telegram_postings(order_id: int) -> list[dict]:
    url = f"{get_settings().BACKEND_URL}/orders/{order_id}/telegram_postings"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json()


async def clear_order_telegram_postings(order_id: int) -> None:
    url = f"{get_settings().BACKEND_URL}/orders/{order_id}/telegram_postings"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.delete(url)
        if r.status_code not in (204, 404):
            r.raise_for_status()


async def delete_order(order_id: int, requester_telegram_id: int) -> dict:
    """Удалить заявку пользователем."""
    url = f"{get_settings().BACKEND_URL}/orders/{order_id}"
    params = {"requester_telegram_id": requester_telegram_id}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.delete(url, params=params)
        if r.status_code == 400:
            # Пробрасываем текст ошибки наверх
            try:
                body = r.json()
                detail = body.get("detail", r.text)
            except Exception:  # noqa: BLE001
                detail = r.text or "Ошибка удаления"
            raise ValueError(str(detail))
        r.raise_for_status()
        return r.json()


async def delete_order_admin(order_id: int, requester_telegram_id: int) -> dict:
    """Удалить заявку администратором."""
    url = f"{get_settings().BACKEND_URL}/orders/{order_id}/admin"
    params = {"requester_telegram_id": requester_telegram_id}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.delete(url, params=params)
        r.raise_for_status()
        return r.json()


async def get_stats_summary(
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Получить сводную статистику заявок.

    - month: YYYY-MM (совместимость со старым API)
    - date_from/date_to: период в формате дд.мм.гг
    """
    url = f"{get_settings().BACKEND_URL}/stats/summary"
    params: dict[str, str] = {}
    if month:
        params["month"] = month
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    if not params:
        params = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params or None)
        r.raise_for_status()
        return r.json()
