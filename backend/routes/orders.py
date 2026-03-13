"""Orders API routes."""
from datetime import datetime
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.session import get_db
from database.models import Order, OrderItem, User, Product
from backend.schemas import OrderCreate, OrderResponse, OrderUpdate, OrderListResponse, OrderItemResponse, OrderItemCreate
from backend.services.order_service import generate_order_number, get_or_create_user
from backend.services.excel_service import generate_order_excel, get_excel_filename
from backend.services.template_service import (
    parse_user_template_excel,
    generate_markznak_template_excel,
)
from backend.services.google_sheets_service import sheets_service
from backend.services.audit_service import log_action

router = APIRouter()


@router.post("/", response_model=OrderResponse)
async def create_order(
    data: OrderCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create new order."""
    user = await get_or_create_user(
        db, data.author_telegram_id, data.author_username, data.author_full_name
    )
    
    order_number = await generate_order_number(db)
    order = Order(
        number=order_number,
        author_id=user.id,
        status="создана",
        order_type=data.order_type,
        ms_order_number=data.ms_order_number,
        comment=data.comment,
    )
    db.add(order)
    await db.flush()
    
    for item_data in data.items:
        item = OrderItem(
            order_id=order.id,
            size=item_data.size,
            quantity=item_data.quantity,
        )
        if item_data.product_id:
            stmt = select(Product).where(Product.id == item_data.product_id)
            result = await db.execute(stmt)
            product = result.scalar_one_or_none()
            if product:
                item.product_id = product.id
                item.article = product.article
                item.name = product.name
                item.color = product.color
                item.tnved_code = product.tnved_code
                item.legal_entity = product.legal_entity
                item.brand = product.brand
                item.composition = product.composition
                item.country = product.country
                item.target_gender = product.target_gender
                item.category = product.category
        else:
            item.article = item_data.article
            item.name = item_data.name
            item.color = item_data.color
            item.tnved_code = item_data.tnved_code
            item.legal_entity = item_data.legal_entity
            item.brand = item_data.brand
            item.composition = item_data.composition
            item.country = item_data.country
            item.target_gender = item_data.target_gender
            item.category = item_data.category
        
        db.add(item)
    
    await db.flush()
    await db.refresh(order)
    
    # Add to Google Sheets registry (use data.items — order.items lazy load fails in async)
    items_summary = ", ".join(
        f"{it.article or it.name or '?'} x{it.quantity}" for it in data.items
    )
    author_str = data.author_full_name or (f"@{data.author_username}" if data.author_username else str(data.author_telegram_id))
    await log_action(
        db,
        telegram_id=data.author_telegram_id,
        action="order_created",
        entity_type="order",
        entity_id=order.id,
        details=f"number={order.number}",
    )
    sheets_service.append_order_to_registry(
        order_number=order.number,
        created_at=order.created_at.strftime("%d.%m.%Y %H:%M"),
        author=author_str,
        items_summary=items_summary[:500],
        status=order.status,
        author_telegram_id=data.author_telegram_id,
        author_username=data.author_username,
    )

    # Eager-load items (async session doesn't support lazy load)
    stmt = select(Order).where(Order.id == order.id).options(selectinload(Order.items))
    result = await db.execute(stmt)
    order = result.scalar_one()

    return OrderResponse(
        id=order.id,
        number=order.number,
        author_id=order.author_id,
        status=order.status,
        order_type=order.order_type,
        ms_order_number=order.ms_order_number,
        comment=order.comment,
        yandex_link=order.yandex_link,
        created_at=order.created_at,
        items=[OrderItemResponse.model_validate(i) for i in order.items],
    )


@router.delete("/{order_id}")
async def delete_order(
    order_id: int,
    requester_telegram_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Удалить заявку пользователем.

    Пользователь может удалить только свою заявку и только в статусе «создана».
    """
    stmt = (
        select(Order, User)
        .join(User, Order.author_id == User.id)
        .where(Order.id == order_id)
    )
    result = await db.execute(stmt)
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    order, user = row

    if user.telegram_id != requester_telegram_id:
        raise HTTPException(
            status_code=403,
            detail="You can delete only your own orders.",
        )
    if order.status != "создана":
        raise HTTPException(
            status_code=400,
            detail="ORDER_NOT_DELETABLE",
        )

    number = order.number
    author_username = user.username
    author_telegram_id = user.telegram_id

    await db.delete(order)
    await log_action(
        db,
        telegram_id=requester_telegram_id,
        action="order_deleted_by_user",
        entity_type="order",
        entity_id=order_id,
        details=f"number={number}",
    )
    await db.flush()

    return {
        "id": order_id,
        "number": number,
        "author_telegram_id": author_telegram_id,
        "author_username": author_username,
    }


@router.delete("/{order_id}/admin")
async def admin_delete_order(
    order_id: int,
    requester_telegram_id: int | None = Query(
        default=None,
        description="telegram_id администратора (для аудита, опционально)",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Удалить заявку администратором.

    Админ может удалить любую заявку, независимо от статуса.
    """
    stmt = (
        select(Order, User)
        .join(User, Order.author_id == User.id)
        .where(Order.id == order_id)
    )
    result = await db.execute(stmt)
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    order, user = row

    number = order.number
    author_username = user.username
    author_telegram_id = user.telegram_id

    await db.delete(order)
    await log_action(
        db,
        telegram_id=requester_telegram_id,
        action="order_deleted_by_admin",
        entity_type="order",
        entity_id=order_id,
        details=f"number={number}",
    )
    await db.flush()

    return {
        "id": order_id,
        "number": number,
        "author_telegram_id": author_telegram_id,
        "author_username": author_username,
    }


@router.post("/from_template", response_model=OrderResponse)
async def create_order_from_template(
    file: UploadFile = File(...),
    author_telegram_id: int = Form(...),
    author_username: str | None = Form(None),
    author_full_name: str | None = Form(None),
    order_type: str | None = Form(None),
    ms_order_number: str | None = Form(None),
    comment: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Создать заявку из заполненного пользовательского шаблона Excel."""
    content = await file.read()
    try:
        rows = parse_user_template_excel(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка чтения файла: {e}")
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="В шаблоне нет заполненных позиций (Количество > 0)",
        )
    items: list[OrderItemCreate] = []
    for r in rows:
        # Поиск существующего товара стараемся делать по наименованию и размеру,
        # чтобы избежать ложных совпадений по "общим" артикулам (например, "ДЖЕМПЕР").
        stmt = select(Product).where(
            Product.name == r.get("name"),
            Product.size == r["size"],
        )
        result = await db.execute(stmt)
        product = result.scalars().first()

        # Если по имени не нашли, пробуем старый вариант: по article+size
        # как запасной механизм для уже заведённых данных.
        if not product:
            stmt = select(Product).where(
                Product.article == r["article"],
                Product.size == r["size"],
            )
            result = await db.execute(stmt)
            product = result.scalars().first()

        if product:
            items.append(
                OrderItemCreate(
                    product_id=product.id,
                    size=r["size"],
                    quantity=r["quantity"],
                )
            )
        else:
            items.append(
                OrderItemCreate(
                    size=r["size"],
                    quantity=r["quantity"],
                    article=r["article"],
                    name=r.get("name") or r["article"],
                    tnved_code=r.get("tnved_code"),
                    color=r.get("color"),
                    composition=r.get("composition"),
                    legal_entity=r.get("legal_entity"),
                    brand=r.get("brand"),
                    country=r.get("country"),
                    target_gender=r.get("target_gender"),
                    category=r.get("item_type"),
                )
            )
    data = OrderCreate(
        author_telegram_id=author_telegram_id,
        author_username=author_username,
        author_full_name=author_full_name,
        order_type=order_type,
        ms_order_number=ms_order_number,
        comment=comment,
        items=items,
    )
    order_resp = await create_order(data, db)
    return order_resp


@router.get("/", response_model=list[OrderListResponse])
async def list_orders(
    author_telegram_id: int | None = Query(None),
    responsible_telegram_id: int | None = Query(None),
    status: str | None = Query(None),
    admin: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List orders with optional filters.
    - author_telegram_id: заявки, созданные этим пользователем
    - responsible_telegram_id: заявки, где этот админ ответственный
    """
    stmt = (
        select(Order, User, func.count(OrderItem.id).label("items_count"))
        .join(User, Order.author_id == User.id)
        .outerjoin(OrderItem, Order.id == OrderItem.order_id)
        .group_by(Order.id, User.id)
    )
    
    if responsible_telegram_id is not None:
        stmt = stmt.where(Order.responsible_telegram_id == responsible_telegram_id)
    elif author_telegram_id is not None:
        stmt = stmt.where(User.telegram_id == author_telegram_id)
    if status:
        stmt = stmt.where(Order.status == status)
    
    stmt = stmt.order_by(Order.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    rows = result.all()
    
    return [
        OrderListResponse(
            id=order.id,
            number=order.number,
            status=order.status,
            created_at=order.created_at,
            author_username=user.username,
            responsible_username=order.responsible_username,
            items_count=items_count or 0,
        )
        for order, user, items_count in rows
    ]


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get order by ID with items."""
    stmt = (
        select(Order, User)
        .join(User, Order.author_id == User.id)
        .where(Order.id == order_id)
        .options(selectinload(Order.items))
    )
    result = await db.execute(stmt)
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    order, user = row
    return OrderResponse(
        id=order.id,
        number=order.number,
        author_id=order.author_id,
        author_telegram_id=user.telegram_id,
        author_username=user.username,
        status=order.status,
        order_type=order.order_type,
        ms_order_number=order.ms_order_number,
        comment=order.comment,
        yandex_link=order.yandex_link,
         responsible_telegram_id=order.responsible_telegram_id,
         responsible_username=order.responsible_username,
        created_at=order.created_at,
        items=[OrderItemResponse.model_validate(i) for i in order.items],
    )


@router.patch("/{order_id}", response_model=OrderResponse)
async def update_order(
    order_id: int,
    data: OrderUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update order status or yandex link."""
    stmt = select(Order).where(Order.id == order_id).options(selectinload(Order.items))
    result = await db.execute(stmt)
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if data.status is not None:
        order.status = data.status
    if data.yandex_link is not None:
        order.yandex_link = data.yandex_link
        if order.status == "создана" or order.status == "в работе":
            order.status = "готово"
        await log_action(db, user_id=order.author_id, action="yandex_link_added", entity_type="order", entity_id=order.id, details=data.yandex_link[:100])
    if data.responsible_telegram_id is not None:
        order.responsible_telegram_id = data.responsible_telegram_id
    if data.responsible_username is not None:
        order.responsible_username = data.responsible_username
    if data.status is not None:
        await log_action(db, action="status_changed", entity_type="order", entity_id=order.id, details=f"to={data.status}")
    
    await db.flush()
    
    sheets_service.update_order_in_registry(
        order.number,
        status=order.status,
        yandex_link=order.yandex_link,
    )
    
    return OrderResponse(
        id=order.id,
        number=order.number,
        author_id=order.author_id,
        status=order.status,
        order_type=order.order_type,
        ms_order_number=order.ms_order_number,
        comment=order.comment,
        yandex_link=order.yandex_link,
        responsible_telegram_id=order.responsible_telegram_id,
        responsible_username=order.responsible_username,
        created_at=order.created_at,
        items=[OrderItemResponse.model_validate(i) for i in order.items],
    )


@router.get("/{order_id}/excel")
async def download_order_excel(
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Generate and return Excel file for order."""
    stmt = (
        select(Order, User)
        .join(User, Order.author_id == User.id)
        .where(Order.id == order_id)
        .options(selectinload(Order.items).selectinload(OrderItem.product))
    )
    result = await db.execute(stmt)
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    order, user = row
    
    items_data = []
    for i in order.items:
        d = {
            "article": i.article,
            "name": i.name,
            "color": i.color,
            "size": i.size,
            "quantity": i.quantity,
            "tnved_code": i.tnved_code,
            "composition": i.composition,
        }
        if i.product:
            d["article"] = d["article"] or i.product.article
            d["name"] = d["name"] or i.product.name
            d["color"] = d["color"] or i.product.color
            d["tnved_code"] = d["tnved_code"] or i.product.tnved_code
            d["composition"] = d["composition"] or i.product.composition
        items_data.append(d)
    
    excel_bytes = generate_order_excel(
        order_number=order.number,
        author_username=user.username,
        author_full_name=user.full_name,
        created_at=order.created_at,
        items=items_data,
    )
    filename = get_excel_filename(order.number, order.created_at)
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{order_id}/markznak_excel")
async def download_markznak_order_excel(
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Сформировать расширенный Excel-файл в формате МаркЗнак по заявке.
    Используется отделом для дальнейшей работы в МаркЗнак.
    """
    stmt = (
        select(Order, User)
        .join(User, Order.author_id == User.id)
        .where(Order.id == order_id)
        .options(selectinload(Order.items).selectinload(OrderItem.product))
    )
    result = await db.execute(stmt)
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    order, user = row

    # Собираем данные по позициям (из продукта или из позиции для новых товаров из шаблона).
    # Файл для админов — без шапки «Заявка № / Дата / Автор» (только таблица с 1-й строки).
    items: list[dict] = []
    for i in order.items:
        product = i.product

        preferred_composition: str | None = None
        if product:
            prod_comp = getattr(product, "composition", None)
            if prod_comp and "Материал верха" in prod_comp:
                preferred_composition = prod_comp

        d: dict = {
            "gtin": getattr(product, "gtin", None) if product else None,
            "quantity": i.quantity,
            "article": i.article or (product.article if product else None),
            "size": i.size,
            "name": i.name or (product.name if product else None),
            "brand": i.brand or (getattr(product, "brand", None) if product else None),
            "category": getattr(product, "category", None)
            if product
            else i.category,
            "variant": getattr(product, "variant", None) if product else None,
            "tnved_code": i.tnved_code or (product.tnved_code if product else None),
            "country": i.country or (getattr(product, "country", None) if product else None),
            "color": i.color or (product.color if product else None),
            # Для обуви важны материалы, которые мы сохраняем в product.composition
            # в формате "Материал верха: ...; Материал подкладки: ...; Материал низа / подошвы: ...".
            # Если у продукта уже есть такой состав, используем его, даже если в позиции заявки
            # остался старый "общий" состав.
            "composition": preferred_composition
            or i.composition
            or (product.composition if product else None),
            "target_gender": i.target_gender or (
                getattr(product, "target_gender", None) if product else None
            ),
            "legal_entity": i.legal_entity or (
                getattr(product, "legal_entity", None) if product else None
            ),
            "payment_method": getattr(product, "payment_method", None)
            if product
            else None,
            "status": getattr(product, "status", None) if product else None,
            "extended_status": getattr(product, "extended_status", None)
            if product
            else None,
            "signed": getattr(product, "signed", None) if product else None,
            "ms_order_number": order.ms_order_number,
            "created_at": order.created_at,
        }
        items.append(d)

    force_mode: str | None = None
    if order.order_type:
        t = (order.order_type or "").strip().lower()
        if "обув" in t or "shoe" in t:
            force_mode = "shoes"
        elif "одеж" in t or "cloth" in t:
            force_mode = "clothing"

    excel_bytes = generate_markznak_template_excel(
        items,
        sheet_title=f"Заявка {order.number}",
        force_mode=force_mode,
    )
    filename = get_excel_filename(order.number, order.created_at).replace(
        "order_", "markznak_"
    )
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
