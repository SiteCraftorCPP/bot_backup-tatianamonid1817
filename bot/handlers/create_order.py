"""Create order handler - теперь вся логика через Excel-шаблоны."""
import logging
from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from bot.keyboards import (
    category_kb,
    product_choice_kb,
    order_type_kb,
    sizes_kb,
    confirm_kb,
    main_menu_kb,
    legal_entity_kb,
    back_kb,
    skip_inline_kb,
    add_more_kb,
    brands_kb,
    country_kb,
    target_gender_kb,
    attach_file_kb,
)
from bot.api_client import (
    create_order_from_template,
    get_user,
    upsert_user,
    get_new_template_excel,
    get_markznak_order_excel,
    get_brands,
    admin_telegram_ids_for_notify,
    register_order_telegram_posting,
)
from config import get_settings
from bot.notification_registry import notifications_registry
from bot.order_notifier import enqueue_pending, notify_order_to_admins
from backend.services.excel_service import (
    get_markznak_download_filename,
    get_order_excel_download_filename,
)

router = Router()
logger = logging.getLogger(__name__)

LEGAL_ENTITIES = ["Пашкович", "АКС КЭПИТАЛ", "Банишевский", "Малец", "Крикун", "Чайковский", "Чайковская"]
COMMON_SIZES = ["XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL", "36", "37", "38", "39", "40", "41", "42", "43", "44"]


def is_admin(telegram_id: int) -> bool:
    return telegram_id in get_settings().admin_ids_list


# --- Start ---
@router.message(F.text == "📋 Создать заявку")
async def start_create_order(message: Message, state: FSMContext):
    """Start create order flow."""
    await state.clear()
    await state.update_data(items=[])
    await message.answer("Выберите категорию:", reply_markup=category_kb())
    await state.set_state("create_order:category")


# --- Category ---
@router.message(StateFilter("create_order:category"), F.text.in_(["Одежда", "Обувь"]))
async def process_category(message: Message, state: FSMContext):
    await state.update_data(category=message.text)
    await message.answer("Выберите способ добавления товара:", reply_markup=product_choice_kb())
    await state.set_state("create_order:product_choice")


# --- Product choice: Повторный / Новый (редирект в флоу шаблона) ---
@router.message(StateFilter("create_order:product_choice"), F.text == "Повторный товар")
async def repeat_product(message: Message, state: FSMContext):
    """Повторный товар — переходим в флоу «Заявка по шаблону» (категория уже выбрана)."""
    await state.set_state("template:category")
    await message.answer("Выберите категорию:", reply_markup=category_kb())


@router.message(StateFilter("create_order:product_choice"), F.text == "Новый товар")
async def new_product(message: Message, state: FSMContext):
    """Новый товар — переходим в флоу шаблона (выбор ЮЛ → бренд → страна → пол → шаблон)."""
    await state.set_state("template:legal_entity")
    await message.answer("Выберите юридическое лицо:", reply_markup=legal_entity_kb())


# --- Search (repeat product) ---
@router.message(StateFilter("create_order:search"), F.text)
async def process_search(message: Message, state: FSMContext):
    if len(message.text.strip()) < 2:
        await message.answer("Введите минимум 2 символа для поиска.")
        return
    try:
        products = await search_products(message.text.strip(), limit=15)
    except Exception as e:
        logger.exception("Search failed: %s", e)
        await message.answer("Ошибка поиска. Попробуйте позже.")
        return
    if not products:
        await message.answer("Товары не найдены. Попробуйте другой запрос или выберите «Новый товар».")
        return
    # Build inline keyboard — короткий текст: артикул (без дублирования в наименовании)
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    def _short_product_label(p: dict, max_len: int = 36) -> str:
        art = p.get("article", "") or "?"
        size = p.get("size")
        if size:
            return f"{art} ({size})"[:max_len]
        return art[:max_len]

    buttons = [
        [InlineKeyboardButton(
            text=_short_product_label(p),
            callback_data=f"prod:{p['id']}",
        )]
        for p in products[:10]
    ]
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="create_back")])
    await message.answer("Выберите товар:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.update_data(search_results=products)
    await state.set_state("create_order:product_select")


# --- Back from product select ---
@router.callback_query(StateFilter("create_order:product_select"), F.data == "create_back")
async def back_from_product_select(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отменено")
    await callback.message.answer("Выберите способ добавления товара:", reply_markup=product_choice_kb())
    await state.set_state("create_order:product_choice")
    await callback.answer()


# --- Product select (callback) ---
@router.callback_query(StateFilter("create_order:product_select"), F.data.startswith("prod:"))
async def process_product_select(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])
    await state.update_data(product_id=product_id, is_new=False)
    product = await get_product(product_id)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    # Product from catalog has fixed size; use it
    size = product.get("size")
    await state.update_data(
        size=size or "—",
        article=product.get("article"),
        name=product.get("name"),
        color=product.get("color"),
        tnved_code=product.get("tnved_code"),
        legal_entity=product.get("legal_entity"),
        brand=product.get("brand"),
        composition=product.get("composition"),
    )
    await callback.message.edit_text(f"Выбран: {product.get('name', '')[:50]}, размер {size or '—'}")
    await callback.message.answer("Выберите вид заявки:", reply_markup=order_type_kb())
    await state.set_state("create_order:order_type")
    await callback.answer()


# --- Back from legal entity ---
@router.message(StateFilter("create_order:legal_entity"), F.text == "« Назад")
async def back_from_legal_entity_reply(message: Message, state: FSMContext):
    await message.answer(
        "Выберите способ добавления товара:",
        reply_markup=product_choice_kb(),
    )
    await state.set_state("create_order:product_choice")


@router.callback_query(StateFilter("create_order:legal_entity"), F.data == "back")
async def back_from_legal_entity(callback: CallbackQuery, state: FSMContext):
    """Совместимость: старые сообщения с инлайн «Назад»."""
    await callback.message.edit_text("Отменено")
    await callback.message.answer("Выберите способ добавления товара:", reply_markup=product_choice_kb())
    await state.set_state("create_order:product_choice")
    await callback.answer()


# --- New product: Legal entity ---
@router.callback_query(StateFilter("create_order:legal_entity"), F.data.startswith("le:"))
async def process_legal_entity(callback: CallbackQuery, state: FSMContext):
    entity = callback.data[3:]
    await state.update_data(legal_entity=entity)
    await callback.message.edit_text(f"ЮЛ: {entity}")
    brands: list[str] = []
    try:
        brands = await get_brands(entity)
    except Exception as e:
        logger.exception("Get brands failed: %s", e)
    if brands:
        await callback.message.answer("Выберите бренд:", reply_markup=brands_kb(brands))
    else:
        await callback.message.answer("Введите бренд:", reply_markup=back_kb())
    await state.set_state("create_order:new_brand")
    await callback.answer()


# --- New product: manual fields ---
@router.message(StateFilter("create_order:new_brand"), F.text)
async def process_new_brand(message: Message, state: FSMContext):
    if message.text == "« Назад":
        await message.answer("Выберите юридическое лицо:", reply_markup=legal_entity_kb())
        await state.set_state("create_order:legal_entity")
        return
    await state.update_data(brand=message.text)
    await message.answer("Введите страну производства:")
    await state.set_state("create_order:new_country")


@router.message(StateFilter("create_order:new_country"), F.text)
async def process_new_country(message: Message, state: FSMContext):
    await state.update_data(country=message.text)
    await message.answer(
        "Выберите целевой пол:",
        reply_markup=target_gender_kb(),
    )
    await state.set_state("create_order:new_gender")


@router.message(StateFilter("create_order:new_gender"), F.text == "« Назад")
async def create_order_new_gender_back(message: Message, state: FSMContext):
    await state.update_data(target_gender=None)
    await message.answer(
        "Выберите страну производства:",
        reply_markup=country_kb(),
    )
    await state.set_state("create_order:new_country")


@router.message(StateFilter("create_order:new_gender"), F.text)
async def process_new_gender(message: Message, state: FSMContext):
    await state.update_data(target_gender=message.text)
    await message.answer("Готовлю шаблон для новых товаров, подождите...")
    try:
        excel_bytes = await get_new_template_excel()
    except Exception as e:
        logger.exception("New template fetch failed: %s", e)
        await message.answer("Ошибка генерации шаблона. Попробуйте позже.")
        await state.clear()
        return
    doc = BufferedInputFile(excel_bytes, filename="Шаблон_новый.xlsx")
    await message.answer_document(
        doc,
        caption=(
            "Шаблон для НОВЫХ товаров.\n\n"
            "Заполните столбцы (Количество, Артикул, Размер, Наименование, Вид товара, "
            "Код ТН ВЭД, Цвет, Состав, Номер заказа МС, Юридическое лицо) и отправьте файл обратно в чат."
        ),
    )
    await state.set_state("create_order:new_template_file")
    await message.answer(
        "Отправьте заполненный файл или нажмите «Назад»",
        reply_markup=attach_file_kb(),
    )


@router.message(StateFilter("create_order:new_template_file"), F.text == "« Назад")
async def new_template_back(message: Message, state: FSMContext):
    """Шаг назад внутри создания заявки по НОВОМУ шаблону."""
    # Возвращаемся на шаг выбора пола, чтобы шаблон мог быть перегенерирован.
    await state.update_data(target_gender=None)
    await state.set_state("create_order:new_gender")
    await message.answer(
        "Выберите целевой пол:",
        reply_markup=target_gender_kb(),
    )


@router.message(StateFilter("create_order:new_template_file"), F.document)
async def process_new_template_file(message: Message, state: FSMContext):
    """Приём заполненного шаблона для НОВЫХ товаров."""
    doc = message.document
    if not doc.file_name or not doc.file_name.endswith((".xlsx", ".xls")):
        await message.answer("Отправьте файл Excel (.xlsx).")
        return
    await message.answer("Обрабатываю шаблон...")
    file = await message.bot.get_file(doc.file_id)
    bytes_io = await message.bot.download_file(file.file_path)
    file_bytes = bytes_io.read()
    user = message.from_user
    if not user:
        await message.answer("Ошибка: не удалось определить пользователя.")
        return
    # Сохраняем/обновляем автора в БД до создания заявки, чтобы статистика показывала @username.
    try:
        existing = await get_user(user.id)
        role = (existing.get("role") if existing else None) or ("admin" if is_admin(user.id) else "user")
        await upsert_user(
            telegram_id=user.id,
            username=user.username,
            full_name=user.full_name,
            role=str(role),
        )
    except Exception:
        # Не блокируем создание заявки, если синк не удался
        pass
    try:
        order = await create_order_from_template(
            file_bytes=file_bytes,
            author_telegram_id=user.id,
            author_username=user.username,
            author_full_name=user.full_name,
            order_type=None,
            ms_order_number=None,
            comment=None,
        )
    except ValueError as e:
        logger.warning("Create from new-template validation: %s", e)
        raw = str(e)
        reason = raw.lower()
        if "не заполнены обязательные поля" in reason:
            # Собираем список всех упомянутых полей из текста ошибки.
            missing_fields: set[str] = set()
            parts = raw.split(";")
            marker = "не заполнены обязательные поля:"
            for part in parts:
                if marker not in part:
                    continue
                tail = part.split(marker, 1)[1]
                for name in tail.split(","):
                    cleaned = name.strip(" .;")
                    if cleaned:
                        missing_fields.add(cleaned)
            if missing_fields:
                fields_str = ", ".join(sorted(missing_fields))
                text = (
                    "Ошибка шаблона: заполните, пожалуйста, поля: "
                    f"{fields_str}."
                )
            else:
                text = f"Ошибка шаблона: {raw}"
        elif "юридическое лицо" in reason:
            text = (
                "Ошибка шаблона: заполните, пожалуйста, поле «Юридическое лицо» "
                "для каждой позиции."
            )
        elif "целевой пол" in reason:
            text = "Ошибка шаблона: заполните поле «Целевой пол»."
        elif "колич" in reason:
            text = (
                "Ошибка шаблона: заполните, пожалуйста, колонку «Количество» "
                "для тех позиций, которые нужно заказать."
            )
        else:
            text = f"Ошибка шаблона: {raw}"
        await message.answer(text)
        return
    except Exception as e:
        logger.exception("Create from new-template failed: %s", e)
        await message.answer("Ошибка при создании заявки. Попробуйте позже.")
        return

    codes_total = sum(
        int(i.get("quantity") or 0) for i in (order.get("items") or [])
    )
    settings = get_settings()
    try:
        # Надёжная доставка: документ + fallback на текст, плюс фоновые ретраи
        # (WORK_CHAT_ID может быть не задан — тогда канал только админы).
        res = await notify_order_to_admins(message.bot, order)
        if not res.delivered_any:
            enqueue_pending(int(order["id"]))
    except Exception as e:
        logger.exception("Notify admins about new order failed: %s", e)
        try:
            enqueue_pending(int(order["id"]))
        except Exception:
            pass
    await state.clear()
    await message.answer(
        f"Заявка № {order['number']} создана. Количество кодов: {codes_total}.",
        reply_markup=main_menu_kb(is_admin=is_admin(user.id)),
    )


"""
Далее в файле остаётся старая пошаговая логика создания заявки без шаблонов.
Она больше не используется из новых состояний, но оставлена для совместимости.
"""


# --- Quantity ---
def _build_item_from_state(data: dict) -> dict:
    """Build item dict for payload from current state."""
    return {
        "product_id": data.get("product_id"),
        "size": data.get("size", ""),
        "quantity": data.get("quantity", 1),
        "article": data.get("article"),
        "name": data.get("name"),
        "color": data.get("color"),
        "tnved_code": data.get("tnved_code"),
        "legal_entity": data.get("legal_entity"),
        "brand": data.get("brand"),
        "composition": data.get("composition"),
        "country": data.get("country"),
        "target_gender": data.get("target_gender"),
    }


@router.message(StateFilter("create_order:quantity"), F.text)
async def process_quantity(message: Message, state: FSMContext):
    try:
        qty = int(message.text.strip())
        if qty < 1 or qty > 9999:
            raise ValueError("out of range")
    except ValueError:
        await message.answer("Введите целое число от 1 до 9999.")
        return
    await state.update_data(quantity=qty)
    data = await state.get_data()
    items = data.get("items", []).copy()
    items.append(_build_item_from_state(data))
    await state.update_data(items=items)
    summary = ", ".join(
        f"{it.get('article') or it.get('name') or '?'} x{it.get('quantity', 1)}"
        for it in items
    )
    await message.answer(
        f"Добавлено. Позиций в заявке: {len(items)}\n{summary}\n\nДобавить ещё товар или завершить?",
        reply_markup=add_more_kb(),
    )
    await state.set_state("create_order:add_more")


# --- Add more or finish ---
@router.message(StateFilter("create_order:add_more"), F.text == "➕ Добавить ещё")
async def add_more_product(message: Message, state: FSMContext):
    await message.answer("Выберите способ добавления товара:", reply_markup=product_choice_kb())
    await state.set_state("create_order:product_choice")


@router.message(StateFilter("create_order:add_more"), F.text == "✅ Завершить")
async def finish_adding_items(message: Message, state: FSMContext):
    await message.answer(
        "Введите номер заявки МС:",
        reply_markup=skip_inline_kb("skip_ms"),
    )
    await state.set_state("create_order:ms_number")


# --- MS number ---
@router.callback_query(StateFilter("create_order:ms_number"), F.data == "skip_ms")
async def skip_ms_number(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(ms_order_number=None)
    await callback.message.answer(
        "Введите комментарий:",
        reply_markup=skip_inline_kb("skip_comment"),
    )
    await state.set_state("create_order:comment")


@router.message(StateFilter("create_order:ms_number"), F.text)
async def process_ms_number(message: Message, state: FSMContext):
    await state.update_data(ms_order_number=message.text.strip())
    await message.answer(
        "Введите комментарий:",
        reply_markup=skip_inline_kb("skip_comment"),
    )
    await state.set_state("create_order:comment")


# --- Comment ---
@router.callback_query(StateFilter("create_order:comment"), F.data == "skip_comment")
async def skip_comment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(comment=None)
    data = await state.get_data()
    summary = _format_summary(data)
    await callback.message.answer(
        f"Проверьте заявку:\n\n{summary}\n\nПодтвердите:",
        reply_markup=confirm_kb(),
    )
    await state.set_state("create_order:confirm")


@router.message(StateFilter("create_order:comment"), F.text)
async def process_comment(message: Message, state: FSMContext):
    await state.update_data(comment=message.text.strip())
    data = await state.get_data()
    # Summary
    summary = _format_summary(data)
    await message.answer(
        f"Проверьте заявку:\n\n{summary}\n\nПодтвердите:",
        reply_markup=confirm_kb(),
    )
    await state.set_state("create_order:confirm")


def _format_summary(data: dict) -> str:
    items = data.get("items", [])
    parts = [f"Вид заявки: {data.get('order_type', '—')}"]
    for i, it in enumerate(items, 1):
        name = it.get("name") or it.get("article") or "?"
        parts.append(f"{i}. {name} — размер {it.get('size', '?')} x{it.get('quantity', 1)}")
    return "\n".join(parts)


# --- Confirm ---
@router.message(StateFilter("create_order:confirm"), F.text == "✅ Сформировать заявку")
async def process_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    user = message.from_user
    if not user:
        await message.answer("Ошибка: не удалось определить пользователя.")
        return
    items = data.get("items", [])
    if not items:
        await message.answer("В заявке нет позиций. Добавьте товар.")
        return
    payload = {
        "author_telegram_id": user.id,
        "author_username": user.username,
        "author_full_name": user.full_name,
        "order_type": data.get("order_type"),
        "ms_order_number": data.get("ms_order_number"),
        "comment": data.get("comment"),
        "items": items,
    }
    # Сохраняем/обновляем автора в БД до создания заявки, чтобы статистика показывала @username.
    try:
        existing = await get_user(user.id)
        role = (existing.get("role") if existing else None) or ("admin" if is_admin(user.id) else "user")
        await upsert_user(
            telegram_id=user.id,
            username=user.username,
            full_name=user.full_name,
            role=str(role),
        )
    except Exception:
        pass
    try:
        order = await create_order(payload)
    except Exception as e:
        logger.exception("Create order failed: %s", e)
        await message.answer("Ошибка при создании заявки. Попробуйте позже.")
        return
    # Download Excel и отправить его администраторам в личку
    settings = get_settings()
    try:
        excel_bytes = await get_order_excel(order["id"])
        # Save temp file and send
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            f.write(excel_bytes)
            tmp_path = f.name
        try:
            admin_ids = await admin_telegram_ids_for_notify()
            doc = FSInputFile(
                tmp_path, filename=get_order_excel_download_filename(order["number"])
            )
            codes_total = sum(
                int(i.get("quantity") or 0) for i in (order.get("items") or [])
            )
            caption_lines = [
                f"Новая заявка № {order['number']}",
                f"Создал: @{user.username or 'user'}",
                f"Количество кодов: {codes_total}",
                f"Дата: {order['created_at'][:19].replace('T', ' ')}",
            ]
            if data.get("comment"):
                caption_lines.append(f"Комментарий: {data['comment']}")
            caption_lines.append("Файл во вложении")
            caption = "\n".join(caption_lines)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Взять в работу",
                            callback_data=f"take:{order['id']}",
                        )
                    ]
                ]
            )
            for admin_id in admin_ids:
                try:
                    sent = await message.bot.send_document(
                        chat_id=admin_id,
                        document=doc,
                        caption=caption,
                        reply_markup=markup,
                    )
                    notifications_registry.add(
                        order_id=order["id"],
                        chat_id=sent.chat.id,
                        message_id=sent.message_id,
                        is_document=True,
                        file_id=sent.document.file_id if sent.document else None,
                    )
                    try:
                        await register_order_telegram_posting(
                            order["id"], sent.chat.id, sent.message_id
                        )
                    except Exception as reg_err:
                        logger.warning(
                            "register_order_telegram_posting order=%s: %s",
                            order["id"],
                            reg_err,
                        )
                except Exception as e:
                    logger.exception("Send to admin %s failed: %s", admin_id, e)
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        logger.exception("Send to admins failed: %s", e)
    await state.clear()
    is_adm = is_admin(user.id)
    await message.answer(
        f"Заявка № {order['number']} создана.",
        reply_markup=main_menu_kb(is_admin=is_adm),
    )


@router.message(StateFilter("create_order:confirm"), F.text == "❌ Отмена")
async def process_cancel(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id if message.from_user else 0
    await message.answer("Заявка отменена.", reply_markup=main_menu_kb(is_admin=is_admin(user_id)))
