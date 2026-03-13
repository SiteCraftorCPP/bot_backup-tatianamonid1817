"""Products API routes."""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_db
from database.models import Product
from backend.schemas import ProductResponse
from backend.services.template_service import generate_user_template_excel

router = APIRouter()


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


@router.get("/template")
async def get_template_by_article(
    article: str = Query(..., min_length=2),
    category: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Сгенерировать пользовательский шаблон Excel по запросу.

    Поиск только по столбцу «Наименование товаров» (name, variant),
    не по артикулу.
    """
    q = f"%{article.strip()}%"
    stmt = select(Product).where(
        Product.is_active.is_(True),
        or_(
            Product.name.ilike(q),
            Product.variant.ilike(q),
            Product.article.ilike(q),
        ),
    )

    stmt = stmt.order_by(Product.article, Product.size).limit(500)
    result = await db.execute(stmt)
    products = result.scalars().all()
    if not products:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Товары по запросу не найдены")
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
    db: AsyncSession = Depends(get_db),
):
    """Search products by name/variant + артикул."""
    q_lower = f"%{q.lower()}%"
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
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Product not found")
    return ProductResponse.model_validate(product)
