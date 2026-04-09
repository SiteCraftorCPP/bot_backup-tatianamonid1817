"""Orders API routes."""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select, func, or_, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import get_settings
from database.session import get_db
from database.models import (
    Order,
    OrderAttachment,
    OrderItem,
    OrderTelegramPosting,
    User,
    Product,
    TZ_UTC3,
)
from backend.schemas import (
    OrderAttachmentCreate,
    OrderAttachmentResponse,
    OrderCreate,
    OrderItemCreate,
    OrderItemResponse,
    OrderListResponse,
    OrderResponse,
    OrderTelegramPostingCreate,
    OrderTelegramPostingResponse,
    OrderUpdate,
    PurgeTrashRequest,
)
from backend.services.order_service import (
    assign_public_order_number,
    generate_order_number,
    get_or_create_user,
)
from backend.services.excel_service import (
    content_disposition_attachment,
    generate_order_excel,
    get_markznak_download_filename,
    get_order_excel_download_filename,
)
from backend.services.template_service import (
    parse_user_template_excel,
    generate_markznak_template_excel,
)
from backend.services.google_sheets_service import sheets_service
from backend.services.audit_service import log_action

router = APIRouter()
logger = logging.getLogger(__name__)


def _clip_str(value: str | None, max_len: int) -> str | None:
    """Обрезка строки под VARCHAR в БД (иначе PostgreSQL даёт DataError → 500)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s[:max_len] if len(s) > max_len else s


def _order_item_create_from_template_row(
    r: dict,
    product: Product | None,
) -> OrderItemCreate:
    """Собрать позицию с учётом лимитов полей order_items."""
    qty = int(r["quantity"])
    if product:
        sz = product.size or _clip_str(r.get("size"), 20) or ""
        return OrderItemCreate(product_id=product.id, size=sz, quantity=qty)
    return OrderItemCreate(
        size=_clip_str(r.get("size"), 20) or "",
        quantity=qty,
        article=_clip_str(r.get("article"), 100),
        name=_clip_str(r.get("name"), 500) or _clip_str(r.get("article"), 100) or "",
        tnved_code=_clip_str(r.get("tnved_code"), 50),
        color=_clip_str(r.get("color"), 100),
        composition=_clip_str(r.get("composition"), 500),
        legal_entity=_clip_str(r.get("legal_entity"), 200),
        brand=_clip_str(r.get("brand"), 200),
        country=_clip_str(r.get("country"), 200),
        target_gender=_clip_str(r.get("target_gender"), 50),
        category=_clip_str(r.get("item_type"), 100),
    )


def _require_admin_telegram(telegram_id: int | None) -> None:
    if telegram_id is None:
        raise HTTPException(status_code=403, detail="Admin only")
    if int(telegram_id) not in get_settings().admin_ids_list:
        raise HTTPException(status_code=403, detail="Admin only")


def _extra_attachments_payload(order: Order) -> list[OrderAttachmentResponse]:
    rows = list(order.attachments or [])
    rows.sort(key=lambda a: (a.created_at, a.id))
    return [OrderAttachmentResponse.model_validate(a) for a in rows]


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
    await assign_public_order_number(db, order)
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
    try:
        sheets_service.append_order_to_registry(
            order_number=order.number,
            created_at=order.created_at.strftime("%d.%m.%Y %H:%M"),
            author=author_str,
            items_summary=items_summary[:500],
            status=order.status,
            author_telegram_id=data.author_telegram_id,
            author_username=data.author_username,
        )
    except Exception:
        logger.exception("append_order_to_registry failed (заявка уже создана)")

    # Eager-load items (async session doesn't support lazy load)
    stmt = (
        select(Order)
        .where(Order.id == order.id)
        .options(selectinload(Order.items), selectinload(Order.attachments))
    )
    result = await db.execute(stmt)
    order = result.scalar_one()

    return OrderResponse(
        id=order.id,
        number=order.number,
        author_id=order.author_id,
        author_telegram_id=user.telegram_id,
        author_username=user.username,
        author_full_name=user.full_name,
        status=order.status,
        order_type=order.order_type,
        ms_order_number=order.ms_order_number,
        comment=order.comment,
        yandex_link=order.yandex_link,
        responsible_telegram_id=order.responsible_telegram_id,
        responsible_username=order.responsible_username,
        created_at=order.created_at,
        updated_at=order.updated_at,
        deleted_at=order.deleted_at,
        items=[OrderItemResponse.model_validate(i) for i in order.items],
        extra_attachments=_extra_attachments_payload(order),
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
    if order.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Order not found")

    number = order.number
    author_username = user.username
    author_telegram_id = user.telegram_id

    order.deleted_at = datetime.now(TZ_UTC3)
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

    if order.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Order not found")

    order.deleted_at = datetime.now(TZ_UTC3)
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


@router.post("/trash/purge")
async def purge_trash_orders(
    data: PurgeTrashRequest,
    requester_telegram_id: int = Query(..., description="Telegram id администратора"),
    db: AsyncSession = Depends(get_db),
):
    """Окончательно удалить заявки из корзины (только с заполненным deleted_at)."""
    _require_admin_telegram(requester_telegram_id)
    if data.ids:
        stmt = select(Order).where(
            Order.id.in_(data.ids),
            Order.deleted_at.isnot(None),
        )
    else:
        stmt = select(Order).where(Order.deleted_at.isnot(None))
    result = await db.execute(stmt)
    rows = result.scalars().all()
    purged_numbers = [o.number for o in rows]
    for o in rows:
        await db.delete(o)
    await log_action(
        db,
        telegram_id=requester_telegram_id,
        action="trash_purged",
        entity_type="order",
        entity_id=0,
        details=f"count={len(rows)} numbers={purged_numbers[:20]}",
    )
    await db.flush()
    return {"purged": len(rows), "numbers": purged_numbers}


@router.delete("/{order_id}/purge")
async def purge_single_trashed_order(
    order_id: int,
    requester_telegram_id: int = Query(..., description="Telegram id администратора"),
    db: AsyncSession = Depends(get_db),
):
    """Окончательно удалить одну заявку из корзины."""
    _require_admin_telegram(requester_telegram_id)
    stmt = select(Order).where(Order.id == order_id)
    result = await db.execute(stmt)
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.deleted_at is None:
        raise HTTPException(
            status_code=400,
            detail="Order is not in trash (soft-delete first)",
        )
    number = order.number
    await db.delete(order)
    await log_action(
        db,
        telegram_id=requester_telegram_id,
        action="order_purged_from_trash",
        entity_type="order",
        entity_id=order_id,
        details=f"number={number}",
    )
    await db.flush()
    return {"id": order_id, "number": number}


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
        # Важно: для повторных шаблонов один и тот же article+size может существовать
        # у разных юрлиц/брендов. Ищем максимально строго, иначе легко "склеить"
        # позицию с чужим product_id и получить неверные поля в МаркЗнак.
        product = None
        # Нормализация строк для устойчивого матчинга (пробелы/регистр/NBSP).
        article_val = str(r.get("article") or "").replace("\u00A0", " ").strip()
        size_val = str(r.get("size") or "").replace("\u00A0", " ").strip()
        name_val = str(r.get("name") or "").replace("\u00A0", " ").strip()
        legal_entity_val = str(r.get("legal_entity") or "").replace("\u00A0", " ").strip()
        brand_val = str(r.get("brand") or "").replace("\u00A0", " ").strip()
        country_val = str(r.get("country") or "").replace("\u00A0", " ").strip()
        color_val = str(r.get("color") or "").replace("\u00A0", " ").strip()
        tnved_val = str(r.get("tnved_code") or "").replace("\u00A0", " ").strip()
        item_type_val = str(r.get("item_type") or "").replace("\u00A0", " ").strip()

        def _norm_text(col):
            # trim(replace(col, NBSP, ' ')); без lower(), чтобы не ломать кириллицу на SQLite
            return func.trim(func.replace(col, "\u00A0", " "))

        # 1) Основной матч: article+size + уточняющие поля (ЮЛ/страна/бренд), без name.
        # Это самый надёжный вариант для GTIN, даже если name слегка отличается.
        base_filters = [
            _norm_text(Product.article) == article_val,
            _norm_text(Product.size) == size_val,
        ]
        if legal_entity_val:
            base_filters.append(_norm_text(Product.legal_entity) == legal_entity_val)
        if country_val:
            base_filters.append(_norm_text(Product.country) == country_val)
        if brand_val:
            base_filters.append(_norm_text(Product.brand) == brand_val)

        result = await db.execute(select(Product).where(*base_filters))
        candidates = result.scalars().all()
        if len(candidates) == 1:
            product = candidates[0]

        # 2) Если всё ещё несколько — дополнительно сужаем по полям из шаблона.
        # Это критично для повторов, где article+size совпадают у нескольких строк.
        if not product and len(candidates) > 1:
            narrowed_filters = list(base_filters)
            if color_val:
                narrowed_filters.append(_norm_text(Product.color) == color_val)
            if tnved_val:
                narrowed_filters.append(_norm_text(Product.tnved_code) == tnved_val)
            if item_type_val:
                narrowed_filters.append(
                    or_(
                        _norm_text(Product.category) == item_type_val,
                        _norm_text(Product.variant) == item_type_val,
                    )
                )
            if len(narrowed_filters) > len(base_filters):
                result = await db.execute(select(Product).where(*narrowed_filters))
                narrowed = result.scalars().all()
                if len(narrowed) == 1:
                    product = narrowed[0]
                elif len(narrowed) > 1:
                    candidates = narrowed

        # 3) Если всё ещё несколько — дополнительно уточняем name.
        if not product and len(candidates) > 1 and name_val:
            result = await db.execute(
                select(Product).where(*base_filters, _norm_text(Product.name) == name_val)
            )
            narrowed = result.scalars().all()
            if len(narrowed) == 1:
                product = narrowed[0]
            elif len(narrowed) > 1:
                # Среди нескольких кандидатов предпочитаем запись с GTIN.
                with_gtin = [p for p in narrowed if getattr(p, "gtin", None)]
                if len(with_gtin) == 1:
                    product = with_gtin[0]

        # 4) Fallback: name+size (+ЮЛ/страна/бренд), только если уникально.
        if not product and name_val:
            name_filters = [
                _norm_text(Product.name) == name_val,
                _norm_text(Product.size) == size_val,
            ]
            if legal_entity_val:
                name_filters.append(_norm_text(Product.legal_entity) == legal_entity_val)
            if country_val:
                name_filters.append(_norm_text(Product.country) == country_val)
            if brand_val:
                name_filters.append(_norm_text(Product.brand) == brand_val)
            result = await db.execute(select(Product).where(*name_filters))
            name_candidates = result.scalars().all()
            if len(name_candidates) == 1:
                product = name_candidates[0]
            elif len(name_candidates) > 1:
                with_gtin = [p for p in name_candidates if getattr(p, "gtin", None)]
                if len(with_gtin) == 1:
                    product = with_gtin[0]

        # 5) Последний безопасный fallback:
        # если после всех попыток осталось несколько кандидатов и только один из них с GTIN,
        # берём его, чтобы не терять GTIN в итоговой заявке.
        if not product and len(candidates) > 1:
            with_gtin = [p for p in candidates if getattr(p, "gtin", None)]
            if len(with_gtin) == 1:
                product = with_gtin[0]

        # 6) Детерминированный fallback: выбираем наиболее похожего кандидата.
        # Используем только когда выбор всё ещё неоднозначный/пустой, чтобы не
        # терять product_id/GTIN на повторных товарах.
        if not product and candidates:
            def _n(v: str | None) -> str:
                return str(v or "").replace("\u00A0", " ").strip().lower()

            row_item_type = _n(item_type_val)

            scored: list[tuple[int, int, Product]] = []
            for c in candidates:
                score = 0
                if _n(getattr(c, "article", None)) == _n(article_val):
                    score += 5
                if _n(getattr(c, "size", None)) == _n(size_val):
                    score += 5
                if legal_entity_val and _n(getattr(c, "legal_entity", None)) == _n(legal_entity_val):
                    score += 6
                if brand_val and _n(getattr(c, "brand", None)) == _n(brand_val):
                    score += 6
                if country_val and _n(getattr(c, "country", None)) == _n(country_val):
                    score += 4
                if color_val and _n(getattr(c, "color", None)) == _n(color_val):
                    score += 3
                if tnved_val and _n(getattr(c, "tnved_code", None)) == _n(tnved_val):
                    score += 3
                if name_val and _n(getattr(c, "name", None)) == _n(name_val):
                    score += 2
                if row_item_type:
                    c_cat = _n(getattr(c, "category", None))
                    c_var = _n(getattr(c, "variant", None))
                    if row_item_type in (c_cat, c_var):
                        score += 2

                gtin_bonus = 1 if _n(getattr(c, "gtin", None)) else 0
                scored.append((score, gtin_bonus, c))

            scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
            if len(scored) == 1:
                product = scored[0][2]
            else:
                best_score, best_gtin, best = scored[0]
                second_score, second_gtin, _ = scored[1]
                if (best_score, best_gtin) > (second_score, second_gtin) and best_score > 0:
                    product = best

        # 7) Последний SQL fallback: иногда размер в шаблоне может отличаться
        # форматом (например, "30 " / "30.0"), но остальные поля совпадают.
        if not product and article_val:
            relaxed_filters = [
                _norm_text(Product.article) == article_val,
            ]
            if legal_entity_val:
                relaxed_filters.append(_norm_text(Product.legal_entity) == legal_entity_val)
            if brand_val:
                relaxed_filters.append(_norm_text(Product.brand) == brand_val)
            if country_val:
                relaxed_filters.append(_norm_text(Product.country) == country_val)
            if color_val:
                relaxed_filters.append(_norm_text(Product.color) == color_val)
            if tnved_val:
                relaxed_filters.append(_norm_text(Product.tnved_code) == tnved_val)
            result = await db.execute(select(Product).where(*relaxed_filters))
            relaxed_candidates = result.scalars().all()
            if len(relaxed_candidates) == 1:
                product = relaxed_candidates[0]

        # 8) Абсолютный fallback: чтобы не терять product_id/GTIN в заявке.
        # Берём наиболее "полезного" кандидата: сначала с GTIN, затем самый новый (id DESC).
        if not product:
            final_candidates = list(candidates)
            if not final_candidates and article_val:
                result = await db.execute(
                    select(Product).where(_norm_text(Product.article) == article_val)
                )
                final_candidates = result.scalars().all()
            if final_candidates:
                final_candidates.sort(
                    key=lambda p: (
                        1 if str(getattr(p, "gtin", "") or "").strip() else 0,
                        int(getattr(p, "id", 0) or 0),
                    ),
                    reverse=True,
                )
                product = final_candidates[0]

        items.append(_order_item_create_from_template_row(r, product))
    data = OrderCreate(
        author_telegram_id=author_telegram_id,
        author_username=_clip_str(author_username, 255),
        author_full_name=_clip_str(author_full_name, 255),
        order_type=_clip_str(order_type, 50),
        ms_order_number=_clip_str(ms_order_number, 100),
        comment=comment,
        items=items,
    )
    try:
        order_resp = await create_order(data, db)
        return order_resp
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "create_order_from_template: ошибка сохранения заявки (см. traceback)"
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Не удалось сохранить заявку. Частая причина — слишком длинный текст "
                "в ячейке шаблона (состав, наименование и т.д.). Сократите значения "
                "или проверьте лог сервера бэкенда."
            ),
        )


@router.get("/", response_model=list[OrderListResponse])
async def list_orders(
    author_telegram_id: int | None = Query(None),
    responsible_telegram_id: int | None = Query(None),
    status: str | None = Query(None),
    admin: bool = Query(False),
    include_deleted: bool = Query(
        False,
        description="Для admin: включить удалённые (мягко) в выборку",
    ),
    deleted_only: bool = Query(
        False,
        description="Только заявки в корзине (deleted_at задан)",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List orders with optional filters.
    - author_telegram_id: заявки, созданные этим пользователем (без удалённых)
    - responsible_telegram_id: заявки, где этот админ ответственный
    - admin + include_deleted: «все» включая корзину
    - admin + deleted_only: только корзина
    """
    stmt = (
        select(Order, User, func.count(OrderItem.id).label("items_count"))
        .join(User, Order.author_id == User.id)
        .outerjoin(OrderItem, Order.id == OrderItem.order_id)
        .group_by(Order.id, User.id)
    )

    if author_telegram_id is not None:
        stmt = stmt.where(User.telegram_id == author_telegram_id)
        stmt = stmt.where(Order.deleted_at.is_(None))
    elif responsible_telegram_id is not None:
        stmt = stmt.where(Order.responsible_telegram_id == responsible_telegram_id)
        if deleted_only:
            stmt = stmt.where(Order.deleted_at.isnot(None))
        elif not include_deleted:
            stmt = stmt.where(Order.deleted_at.is_(None))
    elif admin:
        if deleted_only:
            stmt = stmt.where(Order.deleted_at.isnot(None))
        elif not include_deleted:
            stmt = stmt.where(Order.deleted_at.is_(None))
    else:
        stmt = stmt.where(Order.deleted_at.is_(None))

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
            deleted_at=order.deleted_at,
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
        .options(selectinload(Order.items), selectinload(Order.attachments))
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
        author_full_name=user.full_name,
        status=order.status,
        order_type=order.order_type,
        ms_order_number=order.ms_order_number,
        comment=order.comment,
        yandex_link=order.yandex_link,
        responsible_telegram_id=order.responsible_telegram_id,
        responsible_username=order.responsible_username,
        created_at=order.created_at,
        updated_at=order.updated_at,
        deleted_at=order.deleted_at,
        items=[OrderItemResponse.model_validate(i) for i in order.items],
        extra_attachments=_extra_attachments_payload(order),
    )


@router.post("/{order_id}/attachments", response_model=OrderAttachmentResponse)
async def add_order_attachment(
    order_id: int,
    data: OrderAttachmentCreate,
    author_telegram_id: int = Query(..., description="Telegram id автора заявки"),
    db: AsyncSession = Depends(get_db),
):
    """Зарегистрировать дополнительный файл (file_id бота после загрузки от пользователя)."""
    stmt = select(Order, User).join(User, Order.author_id == User.id).where(Order.id == order_id)
    result = await db.execute(stmt)
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    order, user = row
    if user.telegram_id != author_telegram_id:
        raise HTTPException(
            status_code=403,
            detail="Only the order author can attach files.",
        )
    if order.deleted_at is not None:
        raise HTTPException(status_code=400, detail="ORDER_DELETED")
    att = OrderAttachment(
        order_id=order_id,
        telegram_file_id=data.telegram_file_id,
        file_name=data.file_name,
    )
    db.add(att)
    await db.flush()
    await db.refresh(att)
    await log_action(
        db,
        telegram_id=author_telegram_id,
        action="order_attachment_added",
        entity_type="order",
        entity_id=order_id,
        details=(data.file_name or "")[:200],
    )
    return OrderAttachmentResponse.model_validate(att)


@router.patch("/{order_id}", response_model=OrderResponse)
async def update_order(
    order_id: int,
    data: OrderUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update order status or yandex link."""
    stmt = select(Order).where(Order.id == order_id).options(
        selectinload(Order.items),
        selectinload(Order.attachments),
    )
    result = await db.execute(stmt)
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.deleted_at is not None:
        raise HTTPException(status_code=400, detail="ORDER_DELETED")

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

    patch = data.model_dump(exclude_unset=True)
    if "comment" in patch:
        order.comment = patch["comment"]

    if data.status is not None:
        await log_action(db, action="status_changed", entity_type="order", entity_id=order.id, details=f"to={data.status}")
    
    await db.flush()

    stmt_reload = (
        select(Order)
        .where(Order.id == order.id)
        .options(selectinload(Order.items), selectinload(Order.attachments))
    )
    order = (await db.execute(stmt_reload)).scalar_one()

    author_user = await db.get(User, order.author_id)

    sheets_service.update_order_in_registry(
        order.number,
        status=order.status,
        yandex_link=order.yandex_link,
    )

    u = author_user
    return OrderResponse(
        id=order.id,
        number=order.number,
        author_id=order.author_id,
        author_telegram_id=u.telegram_id if u else None,
        author_username=u.username if u else None,
        author_full_name=u.full_name if u else None,
        status=order.status,
        order_type=order.order_type,
        ms_order_number=order.ms_order_number,
        comment=order.comment,
        yandex_link=order.yandex_link,
        responsible_telegram_id=order.responsible_telegram_id,
        responsible_username=order.responsible_username,
        created_at=order.created_at,
        updated_at=order.updated_at,
        deleted_at=order.deleted_at,
        items=[OrderItemResponse.model_validate(i) for i in order.items],
        extra_attachments=_extra_attachments_payload(order),
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
    filename = get_order_excel_download_filename(order.number)
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition_attachment(filename)},
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
        # Жёсткий fallback для GTIN: если позиция не связана с product_id
        # (или связана с продуктом без GTIN), пытаемся дотянуть товар из справочника
        # по полям позиции заявки.
        if not product or not str(getattr(product, "gtin", "") or "").strip():
            article_val = str(i.article or "").replace("\u00A0", " ").strip()
            size_val = str(i.size or "").replace("\u00A0", " ").strip()
            if article_val:
                def _norm_text(col):
                    return func.trim(func.replace(col, "\u00A0", " "))

                filters = [_norm_text(Product.article) == article_val]
                if size_val:
                    filters.append(_norm_text(Product.size) == size_val)
                if i.legal_entity:
                    filters.append(_norm_text(Product.legal_entity) == str(i.legal_entity).replace("\u00A0", " ").strip())
                if i.brand:
                    filters.append(_norm_text(Product.brand) == str(i.brand).replace("\u00A0", " ").strip())
                if i.country:
                    filters.append(_norm_text(Product.country) == str(i.country).replace("\u00A0", " ").strip())
                if i.color:
                    filters.append(_norm_text(Product.color) == str(i.color).replace("\u00A0", " ").strip())
                if i.tnved_code:
                    filters.append(_norm_text(Product.tnved_code) == str(i.tnved_code).replace("\u00A0", " ").strip())

                res = await db.execute(select(Product).where(*filters).order_by(Product.id.desc()))
                fallback_candidates = res.scalars().all()
                if fallback_candidates:
                    # Приоритет: запись с GTIN, затем наиболее новая.
                    fallback_candidates.sort(
                        key=lambda p: (
                            1 if str(getattr(p, "gtin", "") or "").strip() else 0,
                            int(getattr(p, "id", 0) or 0),
                        ),
                        reverse=True,
                    )
                    product = fallback_candidates[0]

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
        sheet_title=f"№ {order.number}",
        force_mode=force_mode,
    )
    filename = get_markznak_download_filename(order.number)
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition_attachment(filename)},
    )


@router.post(
    "/{order_id}/telegram_postings",
    response_model=OrderTelegramPostingResponse,
)
async def add_order_telegram_posting(
    order_id: int,
    body: OrderTelegramPostingCreate,
    db: AsyncSession = Depends(get_db),
):
    """Бот регистрирует сообщение с МаркЗнак в чате админа (для последующего delete_message)."""
    stmt = select(Order).where(Order.id == order_id)
    result = await db.execute(stmt)
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.deleted_at is not None:
        raise HTTPException(status_code=400, detail="ORDER_DELETED")
    row = OrderTelegramPosting(
        order_id=order_id,
        chat_id=body.chat_id,
        message_id=body.message_id,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return OrderTelegramPostingResponse(chat_id=row.chat_id, message_id=row.message_id)


@router.get(
    "/{order_id}/telegram_postings",
    response_model=list[OrderTelegramPostingResponse],
)
async def list_order_telegram_postings(
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Список зарегистрированных сообщений (в т.ч. для мягко удалённой заявки)."""
    exists = await db.execute(select(Order.id).where(Order.id == order_id))
    if exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Order not found")
    stmt = select(OrderTelegramPosting).where(OrderTelegramPosting.order_id == order_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        OrderTelegramPostingResponse(chat_id=r.chat_id, message_id=r.message_id)
        for r in rows
    ]


@router.delete("/{order_id}/telegram_postings", status_code=204)
async def clear_order_telegram_postings_route(
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        delete(OrderTelegramPosting).where(OrderTelegramPosting.order_id == order_id)
    )
    return Response(status_code=204)
