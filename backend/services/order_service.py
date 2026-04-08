"""Order creation and numbering logic."""
import re
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, OrderItem, User, Product

# Order.number — VARCHAR(50): id + "_" + артикул первой позиции
_ORDER_NUMBER_MAX_LEN = 50


def _sanitize_article_slug(article: str | None) -> str:
    """Часть номера после id_: безопасные символы, без пробелов в начале/конце."""
    if not article:
        return "na"
    s = str(article).strip()
    if not s:
        return "na"
    for bad in '<>:"/\\|?*\n\r\t':
        s = s.replace(bad, "_")
    s = re.sub(r"\s+", "_", s)
    return s or "na"


def _extract_numeric_article_from_name(name: str | None) -> str | None:
    """Попробовать извлечь артикул-число из наименования (например, из «арт. 33708white»)."""
    if not name:
        return None
    s = str(name).strip()
    if not s:
        return None
    m = re.search(r"(?:арт\.?|article)\s*[:#]?\s*(\d{3,})", s, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(\d{3,})", s)
    if m:
        return m.group(1)
    return None


def _public_article_part(article: str | None, name: str | None = None) -> str:
    """Публичная часть номера: приоритетно цифры артикула; если их нет — цифры из наименования."""
    if article:
        s = str(article).strip()
        if s:
            m = re.match(r"^(\d+)", s)
            if m:
                return m.group(1)
            m_any = re.search(r"(\d{3,})", s)
            if m_any:
                return m_any.group(1)
    by_name = _extract_numeric_article_from_name(name)
    if by_name:
        return by_name
    return _sanitize_article_slug(article)


def build_public_order_number(order_id: int, article: str | None, name: str | None = None) -> str:
    """Публичный номер: «31_2705» → в UI «№ 31_2705» (артикул — числа в начале полного артикула первой позиции)."""
    prefix = f"{order_id}_"
    slug = _public_article_part(article, name)
    room = _ORDER_NUMBER_MAX_LEN - len(prefix)
    if room < 1:
        return str(order_id)[:_ORDER_NUMBER_MAX_LEN]
    if len(slug) > room:
        slug = slug[:room]
    return prefix + slug


async def assign_public_order_number(db: AsyncSession, order: Order) -> None:
    """После сохранения позиций: номер заявки = id + артикул первой строки (по id позиции)."""
    stmt = (
        select(OrderItem)
        .where(OrderItem.order_id == order.id)
        .order_by(OrderItem.id.asc())
        .limit(1)
    )
    result = await db.execute(stmt)
    first = result.scalar_one_or_none()
    art: str | None = None
    name: str | None = None
    if first:
        art = first.article
        name = first.name
        if not art and first.product_id:
            prod = await db.get(Product, first.product_id)
            if prod:
                art = prod.article
                if not name:
                    name = prod.name
    order.number = build_public_order_number(order.id, art, name)


async def generate_order_number(db: AsyncSession) -> str:
    """Уникальный временный номер до финального присвоения id_артикул (после flush)."""
    return f"w-{uuid.uuid4().hex}"


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
