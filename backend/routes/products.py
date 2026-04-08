"""Products API routes."""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select, or_, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_db
from database.models import Product
from backend.schemas import ProductResponse
from backend.services.template_service import generate_user_template_excel

router = APIRouter()

def _normalize_category(category: str | None) -> str | None:
    """Нормализовать категорию бота в режим шаблона Excel (только макет колонок)."""
    if not category:
        return None
    c = category.strip().lower()
    if "обув" in c or "shoe" in c:
        return "shoes"
    if "одеж" in c or "cloth" in c:
        return "clothing"
    return None


_REPEAT_TEMPLATE_NOT_FOUND = "Товары по запросу не найдены"


def _category_filters_for_template(category: str | None) -> list:
    """Жесткий фильтр по выбранной категории для шаблонов повторного товара."""
    mode = _normalize_category(category)
    if mode == "shoes":
        return [
            or_(
                Product.product_type == "shoes",
                and_(Product.product_type.is_(None), Product.tnved_code.ilike("64%")),
            )
        ]
    if mode == "clothing":
        return [
            or_(
                Product.product_type == "clothing",
                and_(
                    Product.product_type.is_(None),
                    or_(Product.tnved_code.is_(None), ~Product.tnved_code.ilike("64%")),
                ),
            )
        ]
    return []


def _repeat_article_search_filters(article: str, category: str | None = None) -> list:
    """Повторный товар: по артикулу/наименованию в рамках выбранной категории."""
    q = f"%{article.strip()}%"
    return [
        Product.is_active.is_(True),
        or_(
            Product.name.ilike(q),
            Product.variant.ilike(q),
            Product.article.ilike(q),
        ),
    ] + _category_filters_for_template(category)


async def _require_products_for_repeat(
    db: AsyncSession,
    filters: list,
    *,
    not_found_detail: str,
) -> None:
    check = await db.execute(select(Product.id).where(*filters).limit(1))
    if check.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail=not_found_detail)


@router.get("/brands", response_model=list[str])
async def get_brands(
    legal_entity: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    """
    Получить список брендов по юридическому лицу.

    Используется ботом при создании заявки для новых товаров,
    чтобы показать пользователю список брендов на клавиатуре.
    """
    # Сначала пробуем найти бренды, привязанные к конкретному ЮЛ.
    stmt_specific = (
        select(Product.brand)
        .where(
            func.lower(Product.legal_entity) == legal_entity.strip().lower(),
            Product.brand.is_not(None),
            Product.is_active.is_(True),
        )
        .distinct()
        .order_by(Product.brand)
    )
    result = await db.execute(stmt_specific)
    rows = result.all()
    if rows:
        return [b for (b,) in rows if b]

    # Если для этого ЮЛ в базе ещё нет товаров с брендами,
    # возвращаем общий список всех брендов (чтобы в боте всё равно был выбор).
    stmt_all = (
        select(Product.brand)
        .where(Product.brand.is_not(None), Product.is_active.is_(True))
        .distinct()
        .order_by(Product.brand)
    )
    result_all = await db.execute(stmt_all)
    rows_all = result_all.all()
    return [b for (b,) in rows_all if b]


@router.get("/template/legal_entities", response_model=list[str])
async def get_template_legal_entities(
    article: str = Query(..., min_length=2),
    category: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Список юридических лиц по артикулу/наименованию в выбранной категории."""
    base_filters = _repeat_article_search_filters(article, category)
    await _require_products_for_repeat(
        db,
        base_filters,
        not_found_detail=_REPEAT_TEMPLATE_NOT_FOUND,
    )

    le_nonempty = and_(
        Product.legal_entity.is_not(None),
        func.length(func.trim(Product.legal_entity)) > 0,
    )
    # distinct по trim(ЮЛ), иначе в БД «Малец» и «Малец » дают два ЮЛ, а выбор не совпадает с фильтром.
    le_trim = func.trim(Product.legal_entity)
    stmt = (
        select(le_trim)
        .where(*base_filters, le_nonempty)
        .distinct()
        .order_by(le_trim)
    )
    result = await db.execute(stmt)
    rows = [r for (r,) in result.all() if r and str(r).strip()]
    # Уникальные значения с устойчивым порядком
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        s = str(r).strip()
        key = s.casefold()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


@router.get("/template/countries", response_model=list[str])
async def get_template_countries(
    article: str = Query(..., min_length=2),
    category: str | None = Query(None),
    legal_entity: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Список стран по артикулу и выбранному ЮЛ в выбранной категории."""
    base_filters = _repeat_article_search_filters(article, category)
    filters = list(base_filters)
    le_trim = func.trim(Product.legal_entity)
    if legal_entity and legal_entity.strip():
        filters.append(le_trim == legal_entity.strip())

    await _require_products_for_repeat(
        db,
        filters,
        not_found_detail=_REPEAT_TEMPLATE_NOT_FOUND,
    )

    c_nonempty = and_(
        Product.country.is_not(None),
        func.length(func.trim(Product.country)) > 0,
    )
    co_trim = func.trim(Product.country)
    stmt = (
        select(co_trim)
        .where(*filters, c_nonempty)
        .distinct()
        .order_by(co_trim)
    )
    result = await db.execute(stmt)
    rows = [r for (r,) in result.all() if r and str(r).strip()]
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        s = str(r).strip()
        key = s.casefold()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


@router.get("/template")
async def get_template_by_article(
    article: str = Query(..., min_length=2),
    category: str | None = Query(None),
    legal_entity: str | None = Query(None),
    country: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Шаблон Excel: те же отборы по артикулу + категория + ЮЛ + страна, что и после шагов ТЗ.
    """
    filters = list(_repeat_article_search_filters(article, category))
    if legal_entity and legal_entity.strip():
        filters.append(func.trim(Product.legal_entity) == legal_entity.strip())
    if country and country.strip():
        filters.append(func.trim(Product.country) == country.strip())

    stmt = select(Product).where(*filters)

    stmt = stmt.order_by(Product.article, Product.size).limit(500)
    result = await db.execute(stmt)
    products = result.scalars().all()
    if not products:
        raise HTTPException(status_code=404, detail=_REPEAT_TEMPLATE_NOT_FOUND)
    rows = [ProductResponse.model_validate(p).model_dump() for p in products]
    excel_bytes = generate_user_template_excel(
        rows,
        sheet_title=f"Шаблон {article}",
        category=category,
    )
    # HTTP headers must be latin-1; article может содержать кириллицу, поэтому
    # используем безопасное ASCII-имя файла, чтобы избежать UnicodeEncodeError.
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="template.xlsx"'},
    )


@router.get("/search", response_model=list[ProductResponse])
async def search_products(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    category: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Search products by name/variant + артикул (опционально в рамках категории)."""
    q_lower = f"%{q.lower()}%"
    mode = _normalize_category(category)
    stmt = (
        select(Product)
        .where(
            Product.is_active.is_(True),
            or_(
                Product.name.ilike(q_lower),
                Product.variant.ilike(q_lower),
                Product.article.ilike(q_lower),
            ),
        )
        .limit(limit)
    )
    if mode == "shoes":
        stmt = stmt.where(
            or_(
                Product.product_type == "shoes",
                and_(Product.product_type.is_(None), Product.tnved_code.ilike("64%")),
            )
        )
    elif mode == "clothing":
        stmt = stmt.where(
            or_(
                Product.product_type == "clothing",
                and_(Product.product_type.is_(None), or_(Product.tnved_code.is_(None), ~Product.tnved_code.ilike("64%"))),
            )
        )
    result = await db.execute(stmt)
    products = result.scalars().all()
    return [ProductResponse.model_validate(p) for p in products]


@router.get("/new_template")
async def get_new_template(
    category: str | None = Query(None),
    legal_entity: str | None = Query(None),
    brand: str | None = Query(None),
    country: str | None = Query(None),
    target_gender: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Сгенерировать пустой пользовательский шаблон Excel для новых товаров.

    Для одежды используется старый шаблон, для обуви — расширенный
    обувной шаблон с колонками Бренд, Вид обуви, материалами и т.д.
    """
    excel_bytes = generate_user_template_excel(
        [],
        sheet_title="Новая заявка",
        category=category,
        legal_entity=legal_entity,
        brand=brand,
        country=country,
        target_gender=target_gender,
    )
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="template_new.xlsx"'},
    )


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get product by ID."""
    stmt = select(Product).where(Product.id == product_id)
    result = await db.execute(stmt)
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return ProductResponse.model_validate(product)
