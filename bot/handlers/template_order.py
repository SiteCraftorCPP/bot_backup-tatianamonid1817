"""Заявка по шаблону — категория → Повторный/Новый товар → шаблон → файл."""
import logging
from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from bot.keyboards import (
    main_menu_kb,
    back_kb,
    attach_file_kb,
    done_extra_kb,
    skip_inline_kb,
    category_kb,
    product_choice_kb,
    legal_entity_kb,
    brands_kb,
    country_kb,
    target_gender_kb,
)
from bot.api_client import (
    get_template_excel,
    get_new_template_excel,
    create_order_from_template,
    get_markznak_order_excel,
    get_brands,
    get_user,
    upsert_user,
)
from bot.notification_registry import notifications_registry
from config import get_settings

router = Router()
logger = logging.getLogger(__name__)


async def _send_markznak_to_admins(
    message: Message,
    order_id: int,
    order_number: str,
    items_count: int,
    comment: str | None = None,
    *,
    author_username: str | None = None,
    author_full_name: str | None = None,
    author_id: int | None = None,
) -> None:
    """Сформировать файл МаркЗнак по заявке и отправить всем администраторам."""
    settings = get_settings()
    admin_ids = settings.admin_ids_list
    try:
        from aiogram.types import FSInputFile
        import tempfile
        import os

        markznak_bytes = await get_markznak_order_excel(order_id)
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            f.write(markznak_bytes)
            tmp_path = f.name
        try:
            doc_out = FSInputFile(
                tmp_path, filename=f"Заявка_{order_number}_markznak.xlsx"
            )

            # Определяем автора: сначала из сохранённых данных, затем из message.from_user,
            # но игнорируем сообщения от самого бота.
            username = author_username
            full_name = author_full_name
            uid = author_id
            if message.from_user and not message.from_user.is_bot:
                if not username:
                    username = message.from_user.username
                if not full_name:
                    full_name = message.from_user.full_name
                if not uid:
                    uid = message.from_user.id

            if username:
                author_line = f"Создал: @{username}"
            elif full_name:
                author_line = f"Создал: {full_name}"
            elif uid:
                author_line = f"Создал: {uid}"
            else:
                author_line = None

            caption_lines = [
                f"Новая заявка №{order_number.split('-')[-1]} (по шаблону)",
            ]
            if author_line:
                caption_lines.append(author_line)
            caption_lines.append(f"Позиций: {items_count}")
            if comment:
                caption_lines.append(f"Комментарий: {comment}")
            caption_lines.append("Расширенный файл МаркЗнак.")
            caption = "\n".join(caption_lines)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Взять в работу",
                            callback_data=f"take:{order_id}",
                        )
                    ]
                ]
            )
            for admin_id in admin_ids:
                try:
                    sent = await message.bot.send_document(
                        chat_id=admin_id,
                        document=doc_out,
                        caption=caption,
                        reply_markup=markup,
                    )
                    notifications_registry.add(
                        order_id=order_id,
                        chat_id=sent.chat.id,
                        message_id=sent.message_id,
                        is_document=True,
                        file_id=sent.document.file_id if sent.document else None,
                    )
                except Exception as e:
                    logger.exception("Send markznak to admin %s failed: %s", admin_id, e)
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        logger.exception("Send markznak to admins failed: %s", e)


def _main_menu(message: Message, is_admin: bool):
    """Построение главного меню без повторной проверки прав.

    Флаг is_admin передаётся вызывающим кодом.
    """
    return main_menu_kb(is_admin=is_admin)


# --- Вход: Заявка по шаблону → выбор категории ---
@router.message(F.text == "📄 Заявка по шаблону")
async def start_template_order(message: Message, state: FSMContext):
    """Начало: категория (Одежда / Обувь), затем Повторный/Новый товар."""
    await state.clear()
    await message.answer("Выберите категорию:", reply_markup=category_kb())
    await state.set_state("template:category")


@router.message(StateFilter("template:category"), F.text == "« Назад")
async def template_category_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=_main_menu(message, False))
    return


@router.message(StateFilter("template:category"), F.text.in_(["Одежда", "Обувь"]))
async def template_process_category(message: Message, state: FSMContext):
    await state.update_data(category=message.text, order_type=message.text)
    await message.answer("Выберите способ добавления товара:", reply_markup=product_choice_kb())
    await state.set_state("template:product_choice")


# --- Повторный / Новый ---
@router.message(StateFilter("template:product_choice"), F.text == "« Назад")
async def template_product_choice_back(message: Message, state: FSMContext):
    await message.answer("Выберите категорию:", reply_markup=category_kb())
    await state.set_state("template:category")
    return


@router.message(StateFilter("template:product_choice"), F.text == "Повторный товар")
async def template_repeat_product(message: Message, state: FSMContext):
    """Повторный: поиск по наименованию → шаблон → файл."""
    await message.answer(
        "Введите артикул или часть наименования товара для поиска.\n"
        "Вы получите шаблон — заполните колонку «Количество» и отправьте файл обратно.",
        reply_markup=back_kb(),
    )
    await state.set_state("template:article")


@router.message(StateFilter("template:product_choice"), F.text == "Новый товар")
async def template_new_product(message: Message, state: FSMContext):
    """Новый: справочники (ЮЛ, бренд, страна, пол) → пустой шаблон → файл."""
    await message.answer("Выберите юридическое лицо:", reply_markup=legal_entity_kb())
    await state.set_state("template:legal_entity")


# --- Новый товар: юридическое лицо (inline) ---
@router.callback_query(StateFilter("template:legal_entity"), F.data == "back")
async def template_legal_entity_back(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отменено")
    await callback.message.answer("Выберите способ добавления товара:", reply_markup=product_choice_kb())
    await state.set_state("template:product_choice")
    await callback.answer()


@router.callback_query(StateFilter("template:legal_entity"), F.data.startswith("le:"))
async def template_legal_entity_select(callback: CallbackQuery, state: FSMContext):
    entity = callback.data[3:]
    await state.update_data(legal_entity=entity)
    await callback.message.edit_text(f"ЮЛ: {entity}")
    # Попробуем получить список брендов по ЮЛ и показать клавиатуру.
    brands: list[str] = []
    try:
        brands = await get_brands(entity)
    except Exception as e:
        logger.exception("Get brands failed: %s", e)
    if brands:
        await callback.message.answer("Выберите бренд:", reply_markup=brands_kb(brands))
    else:
        await callback.message.answer("Введите бренд:")
    await state.set_state("template:new_brand")
    await callback.answer()


# --- Новый товар: бренд, страна, пол ---
@router.message(StateFilter("template:new_brand"), F.text)
async def template_new_brand(message: Message, state: FSMContext):
    if message.text == "« Назад":
        await message.answer("Выберите юридическое лицо:", reply_markup=legal_entity_kb())
        await state.set_state("template:legal_entity")
        return
    await state.update_data(brand=message.text.strip())
    await message.answer("Выберите страну производства:", reply_markup=country_kb())
    await state.set_state("template:new_country")


@router.message(StateFilter("template:new_country"), F.text == "« Назад")
async def template_new_country_back(message: Message, state: FSMContext):
    data = await state.get_data()
    legal_entity = data.get("legal_entity")
    brands: list[str] = []
    if legal_entity:
        try:
            brands = await get_brands(legal_entity)
        except Exception:
            pass
    if brands:
        await message.answer("Выберите бренд:", reply_markup=brands_kb(brands))
    else:
        await message.answer("Введите бренд:", reply_markup=back_kb())
    await state.set_state("template:new_brand")
    return


@router.message(StateFilter("template:new_country"), F.text.in_(["КНР", "Киргизия", "Россия"]))
async def template_new_country(message: Message, state: FSMContext):
    await state.update_data(country=message.text.strip())
    await message.answer("Выберите целевой пол:", reply_markup=target_gender_kb())
    await state.set_state("template:new_gender")


@router.message(StateFilter("template:new_gender"), F.text == "« Назад")
async def template_new_gender_back(message: Message, state: FSMContext):
    await message.answer("Выберите страну производства:", reply_markup=country_kb())
    await state.set_state("template:new_country")
    return


@router.message(
    StateFilter("template:new_gender"),
    F.text.in_(["Мужской", "Женский", "Универсальный"]),
)
async def template_new_gender(message: Message, state: FSMContext):
    await state.update_data(target_gender=message.text.strip())
    await message.answer("Готовлю шаблон для новых товаров...")
    try:
        data = await state.get_data()
        category = data.get("category")
        excel_bytes = await get_new_template_excel(
            category=category,
            legal_entity=data.get("legal_entity"),
            brand=data.get("brand"),
            country=data.get("country"),
            target_gender=data.get("target_gender"),
        )
    except Exception as e:
        logger.exception("New template fetch failed: %s", e)
        await message.answer("Ошибка генерации шаблона. Попробуйте позже.")
        await state.clear()
        return
    doc = BufferedInputFile(excel_bytes, filename="Шаблон_новый.xlsx")
    await message.answer_document(
        doc,
        caption=(
            "Шаблон для новых товаров.\n\n"
            "Заполните столбцы (Количество, Артикул, Размер, Наименование, Вид товара, "
            "Код ТН ВЭД, Цвет, Состав, Номер заказа МС, Юридическое лицо) и отправьте файл обратно в чат."
        ),
    )
    await state.update_data(comment=None)
    await state.set_state("template:await_file")


# --- Повторный: ввод артикула/наименования ---
@router.message(StateFilter("template:article"), F.text == "« Назад")
async def template_article_back(message: Message, state: FSMContext):
    await message.answer("Выберите способ добавления товара:", reply_markup=product_choice_kb())
    await state.set_state("template:product_choice")
    return


@router.message(StateFilter("template:article"), F.text)
async def process_article_template(message: Message, state: FSMContext):
    """Запрос шаблона по наименованию (поиск по столбцу «Наименование товаров»)."""
    article = message.text.strip()
    if len(article) < 2:
        await message.answer("Введите минимум 2 символа.")
        return
    data = await state.get_data()
    category = data.get("category")
    try:
        excel_bytes = await get_template_excel(article, category=category)
    except Exception as e:
        logger.exception("Template fetch failed: %s", e)
        if "404" in str(e) or "не найдены" in str(e).lower():
            await message.answer("Товары по этому запросу не найдены. Проверьте наименование.")
        else:
            await message.answer("Ошибка загрузки шаблона. Попробуйте позже.")
        return
    doc = BufferedInputFile(excel_bytes, filename=f"Шаблон_{article}.xlsx")
    await message.answer_document(
        doc,
        caption=(
            f"Шаблон по запросу «{article}».\n\n"
            "Заполните колонку «Количество» и отправьте файл в чат."
        ),
    )
    await state.update_data(article=article, ms_number=None, comment=None)
    await state.set_state("template:await_file")


# --- Комментарий к заявке (общий для нового/повторного товара по шаблону) ---
@router.message(StateFilter("template:comment"), F.text)
async def template_comment_set(message: Message, state: FSMContext):
    # Старая точка входа комментария больше не используется в новом сценарии,
    # но обрабатываем её безопасно, если пользователь каким-то образом сюда попал.
    await state.update_data(comment=message.text.strip())
    await message.answer(
        "Комментарий сохранён. Отправьте заполненный шаблон в чат (файл Excel .xlsx).",
        reply_markup=back_kb(),
    )
    await state.set_state("template:await_file")


@router.message(StateFilter("template:await_file"), F.text == "« Назад")
async def template_file_back(message: Message, state: FSMContext):
    await state.clear()
    from bot.handlers.main_menu import is_admin
    uid = message.from_user.id if message.from_user else 0
    await message.answer("Главное меню:", reply_markup=main_menu_kb(is_admin=is_admin(uid)))
    return


@router.message(StateFilter("template:await_file"), F.text == "⏭ Пропустить")
async def template_file_skip(message: Message, state: FSMContext):
    """Пропустить = отмена, возврат в главное меню."""
    await state.clear()
    from bot.handlers.main_menu import is_admin
    uid = message.from_user.id if message.from_user else 0
    await message.answer("Главное меню:", reply_markup=main_menu_kb(is_admin=is_admin(uid)))
    return


@router.message(StateFilter("template:await_file"), F.text == "📎 Прикрепить файл")
async def template_ask_file(message: Message, state: FSMContext):
    """Пользователь нажал «Прикрепить файл» — просим отправить заполненный шаблон (Excel)."""
    await message.answer("Отправьте заполненный шаблон в формате Excel (.xlsx).")


@router.message(StateFilter("template:await_file"), F.document)
async def process_template_file(message: Message, state: FSMContext):
    """Приём заполненного шаблона (только Excel) → создаём заявку, затем предлагаем доп. файл."""
    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith((".xlsx", ".xls")):
        await message.answer("Сначала отправьте заполненный шаблон в формате Excel (.xlsx).")
        return

    file = await message.bot.get_file(doc.file_id)
    bytes_io = await message.bot.download_file(file.file_path)
    file_bytes = bytes_io.read()
    user = message.from_user
    if not user:
        await message.answer("Ошибка: не удалось определить пользователя.")
        return

    await message.answer("Обрабатываю...")
    data = await state.get_data()
    # Сохраняем/обновляем автора в БД до создания заявки, чтобы статистика показывала @username.
    try:
        existing = await get_user(user.id)
        role = (existing.get("role") if existing else None) or "user"
        await upsert_user(
            telegram_id=user.id,
            username=user.username,
            full_name=user.full_name,
            role=str(role),
        )
    except Exception:
        pass
    try:
        order = await create_order_from_template(
            file_bytes=file_bytes,
            author_telegram_id=user.id,
            author_username=user.username,
            author_full_name=user.full_name,
            order_type=data.get("order_type"),
            ms_order_number=data.get("ms_number"),
            comment=data.get("comment"),
        )
    except ValueError as e:
        logger.warning("Create from template validation: %s", e)
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
        logger.exception("Create from template failed: %s", e)
        await message.answer("Ошибка при создании заявки. Попробуйте позже.")
        return

    # Предлагаем прикрепить доп. файл (любой формат)
    await state.update_data(
        order_id=order["id"],
        order_number=order["number"],
        order_items_count=len(order.get("items", [])),
        markznak_sent=False,
        author_username=user.username,
        author_full_name=user.full_name,
        author_id=user.id,
    )
    await state.set_state("template:await_extra_file")
    from bot.handlers.main_menu import is_admin
    await message.answer(
        f"Заявка №{order['number']} создана. Позиций: {len(order.get('items', []))}.\n\n"
        "Хотите прикрепить дополнительный файл? (любой формат)",
        reply_markup=done_extra_kb(),
    )


# --- Доп. файл после создания заявки ---
@router.message(StateFilter("template:await_extra_file"), F.text == "« Назад")
async def template_extra_back(message: Message, state: FSMContext):
    await state.clear()
    from bot.handlers.main_menu import is_admin as _is_admin
    uid = message.from_user.id if message.from_user else 0
    is_adm = await _is_admin(uid)
    await message.answer("Главное меню:", reply_markup=main_menu_kb(is_admin=is_adm))
    return


@router.message(StateFilter("template:await_extra_file"), F.text == "✅ Готово")
async def template_extra_done(message: Message, state: FSMContext):
    data = await state.get_data()
    order_number = data.get("order_number", "")
    order_id = data.get("order_id")
    items_count = data.get("order_items_count") or 0
    markznak_sent = data.get("markznak_sent")

    if order_id and order_number and not markznak_sent:
        await state.set_state("template:final_comment")
        await message.answer(
            "Введите комментарий к заявке или нажмите «Пропустить».",
            reply_markup=skip_inline_kb("tmpl_final_skip_comment"),
        )
        return

    await state.clear()
    from bot.handlers.main_menu import is_admin as _is_admin
    uid = message.from_user.id if message.from_user else 0
    is_adm = await _is_admin(uid)
    text = f"Заявка №{order_number} оформлена." if order_number else "Готово."
    await message.answer(text, reply_markup=main_menu_kb(is_admin=is_adm))
    return


@router.message(StateFilter("template:await_extra_file"), F.text == "📎 Прикрепить доп. файл")
async def template_extra_ask(message: Message, state: FSMContext):
    await message.answer("Отправьте файл (любой формат).")


async def _finalize_order_with_comment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    order_number = data.get("order_number", "")
    order_id = data.get("order_id")
    items_count = data.get("order_items_count") or 0
    markznak_sent = data.get("markznak_sent")

    if order_id and order_number and not markznak_sent:
        await _send_markznak_to_admins(
            message=message,
            order_id=order_id,
            order_number=order_number,
            items_count=items_count,
            comment=data.get("comment"),
            author_username=data.get("author_username"),
            author_full_name=data.get("author_full_name"),
            author_id=data.get("author_id"),
        )
        await state.update_data(markznak_sent=True)

    await state.clear()
    from bot.handlers.main_menu import is_admin as _is_admin
    uid = message.from_user.id if message.from_user else 0
    is_adm = await _is_admin(uid)
    text = f"Заявка №{order_number} оформлена." if order_number else "Готово."
    await message.answer(text, reply_markup=main_menu_kb(is_admin=is_adm))


@router.callback_query(StateFilter("template:final_comment"), F.data == "tmpl_final_skip_comment")
async def template_final_comment_skip(callback: CallbackQuery, state: FSMContext):
    await state.update_data(comment=None)
    await _finalize_order_with_comment(callback.message, state)
    await callback.answer()


@router.message(StateFilter("template:final_comment"), F.text)
async def template_final_comment_set(message: Message, state: FSMContext):
    if message.text == "« Назад":
        # Возврат в этап доп. файлов без завершения заявки
        await state.set_state("template:await_extra_file")
        await message.answer(
            "Вы можете прикрепить дополнительный файл или нажать «Готово».",
            reply_markup=done_extra_kb(),
        )
        return

    await state.update_data(comment=message.text.strip())
    await _finalize_order_with_comment(message, state)


@router.message(StateFilter("template:await_extra_file"), F.document)
async def process_extra_file(message: Message, state: FSMContext):
    """Приём доп. файла — пересылаем админам с подписью о заявке."""
    doc = message.document
    file = await message.bot.get_file(doc.file_id)
    bytes_io = await message.bot.download_file(file.file_path)
    file_bytes = bytes_io.read()
    user = message.from_user
    if not user:
        return

    data = await state.get_data()
    order_number = data.get("order_number", "")
    order_id = data.get("order_id")
    items_count = data.get("order_items_count") or 0
    who = f"@{user.username}" if user.username else (user.full_name or "пользователь")

    caption = (
        f"Доп. файл к заявке №{order_number.split('-')[-1]}\n"
        f"От: {who}\n"
        f"Файл: {doc.file_name or 'файл'}"
    )
    user_doc = BufferedInputFile(file_bytes, filename=doc.file_name or "файл")
    settings = get_settings()
    for admin_id in settings.admin_ids_list:
        try:
            await message.bot.send_document(
                chat_id=admin_id,
                document=user_doc,
                caption=caption,
            )
        except Exception as e:
            logger.exception("Send extra file to admin %s failed: %s", admin_id, e)

    await message.answer(
        "Файл отправлен админам. Прикрепить ещё или нажмите «Готово».",
        reply_markup=done_extra_kb(),
    )
