"""Заявка по шаблону — категория → Повторный/Новый товар → шаблон → файл."""
import logging
from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from bot.keyboards import (
    main_menu_kb,
    back_kb,
    attach_file_kb,
    done_extra_kb,
    final_comment_kb,
    final_approval_kb,
    category_kb,
    product_choice_kb,
    legal_entity_kb,
    brands_kb,
    country_kb,
    target_gender_kb,
    repeat_template_choices_kb,
)
from bot.api_client import (
    add_order_attachment,
    get_template_excel,
    get_template_legal_entities,
    get_template_countries,
    get_new_template_excel,
    create_order_from_template,
    delete_order,
    get_markznak_order_excel,
    get_order,
    get_brands,
    get_user,
    upsert_user,
    admin_telegram_ids_for_notify,
    set_order_comment,
    register_order_telegram_posting,
)
from config import get_settings
from bot.notification_registry import notifications_registry
from backend.services.excel_service import get_markznak_download_filename
from bot.order_notifier import enqueue_pending, notify_order_to_admins

router = Router()


def _is_photo_filename(file_name: str | None) -> bool:
    """Совпадает с логикой в my_orders (отправка превью доп. вложений)."""
    if not file_name:
        return False
    name = file_name.strip().lower()
    return name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))
logger = logging.getLogger(__name__)


def _repeat_country_display(db_country: str) -> str:
    """Подпись кнопки страны для повторного товара (как в ТЗ)."""
    c = (db_country or "").strip()
    if not c:
        return c
    cl = c.casefold()
    if cl == "кнр":
        return "Китай (КНР)"
    if cl in ("россия", "рф"):
        return "Россия (РФ)"
    if cl == "киргизия":
        return "Киргизия"
    return c


def _build_repeat_country_keyboard(
    countries_db: list[str],
) -> tuple[ReplyKeyboardMarkup, dict[str, str]]:
    """Текст кнопки -> значение country в БД для запроса шаблона."""
    label_to_db: dict[str, str] = {}
    taken: set[str] = set()
    ordered_labels: list[str] = []
    for c in countries_db:
        base = _repeat_country_display(c)
        candidate = base
        if candidate in taken:
            candidate = f"{base} — {c}"
        n = 0
        while candidate in taken:
            n += 1
            candidate = f"{c} ({n})"
        taken.add(candidate)
        label_to_db[candidate] = c
        ordered_labels.append(candidate)
    return repeat_template_choices_kb(ordered_labels), label_to_db


async def _proceed_repeat_country_step(message: Message, state: FSMContext) -> None:
    """После выбора (или авто) юр. лица — проверить страны и выдать шаблон или клавиатуру стран."""
    data = await state.get_data()
    article = data.get("article")
    if not article:
        await message.answer("Сессия сброшена. Начните запрос шаблона заново.")
        await state.clear()
        return
    category = data.get("category")
    le = data.get("repeat_legal_entity")
    le_param = le.strip() if isinstance(le, str) and le.strip() else None
    try:
        countries = await get_template_countries(
            article,
            category=category,
            legal_entity=le_param,
        )
    except Exception as e:
        logger.exception("Template countries fetch failed: %s", e)
        await message.answer("Ошибка загрузки данных. Попробуйте позже.")
        return
    if len(countries) <= 1:
        country_val = countries[0] if countries else None
        await state.update_data(repeat_country=country_val)
        await _send_repeat_template_excel(message, state)
        return
    kb, mapping = _build_repeat_country_keyboard(countries)
    await state.update_data(repeat_country_label_to_db=mapping)
    await message.answer("Выберите страну производства:", reply_markup=kb)
    await state.set_state("template:repeat_country")


async def _send_repeat_template_excel(message: Message, state: FSMContext) -> None:
    """Скачать шаблон с учётом выбранных ЮЛ/страны и перейти к ожиданию файла."""
    data = await state.get_data()
    article = data.get("article")
    if not article:
        await message.answer("Сессия сброшена. Начните запрос шаблона заново.")
        await state.clear()
        return
    category = data.get("category")
    le = data.get("repeat_legal_entity")
    co = data.get("repeat_country")
    le_param = le.strip() if isinstance(le, str) and le.strip() else None
    co_param = co.strip() if isinstance(co, str) and co.strip() else None
    await message.answer("Готовлю шаблон…")
    try:
        excel_bytes = await get_template_excel(
            article,
            category=category,
            legal_entity=le_param,
            country=co_param,
        )
    except Exception as e:
        logger.exception("Template fetch failed: %s", e)
        if "404" in str(e) or "не найдены" in str(e).lower():
            await message.answer("Товары по запросу не найдены.")
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
    await state.update_data(
        article=article,
        ms_number=None,
        comment=None,
        repeat_legal_entity=None,
        repeat_country=None,
        repeat_legal_entity_options=None,
        repeat_country_label_to_db=None,
    )
    await state.set_state("template:await_file")
    await message.answer(
        "Отправьте заполненный шаблон в чат или воспользуйтесь кнопками ниже.",
        reply_markup=attach_file_kb(),
    )


def _template_flow_label(template_flow: str) -> str:
    """Подпись в скобках: повторный / новый товар."""
    return "новый товар" if template_flow == "new" else "повторный товар"


async def _rollback_template_order_to_await_file(message: Message, state: FSMContext) -> None:
    """Удалить только что созданную заявку и вернуться к отправке шаблона."""
    user = message.from_user
    if not user:
        return

    data = await state.get_data()
    order_id = data.get("order_id")
    author_id = data.get("author_id") or user.id

    if order_id:
        try:
            await delete_order(order_id=order_id, requester_telegram_id=author_id)
        except Exception:
            pass

    data.update(
        order_id=None,
        order_number=None,
        order_items_count=None,
        order_codes_total=None,
        markznak_sent=False,
        author_username=None,
        author_full_name=None,
        author_id=author_id,
        comment=None,
    )
    await state.update_data(**data)
    await state.set_state("template:await_file")
    await message.answer(
        "Отправьте заполненный шаблон в формате Excel (.xlsx).",
        reply_markup=attach_file_kb(),
    )


async def _send_markznak_to_admins(
    message: Message,
    order_id: int,
    order_number: str,
    codes_total: int,
    comment: str | None = None,
    *,
    template_flow: str = "repeat",
    author_username: str | None = None,
    author_full_name: str | None = None,
    author_id: int | None = None,
) -> None:
    """Сформировать файл МаркЗнак по заявке и отправить админам (fallback на текст)."""
    admin_ids = await admin_telegram_ids_for_notify()
    try:
        from aiogram.types import FSInputFile
        import tempfile
        import os

        markznak_bytes = await get_markznak_order_excel(order_id)
        order_payload = await get_order(order_id)
        effective_comment = comment or (order_payload or {}).get("comment")
        extras = (order_payload or {}).get("extra_attachments") or []

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            f.write(markznak_bytes)
            tmp_path = f.name
        try:
            doc_out = FSInputFile(
                tmp_path, filename=get_markznak_download_filename(order_number)
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

            flow_label = _template_flow_label(
                template_flow if template_flow in ("new", "repeat") else "repeat"
            )
            caption_lines = [
                f"Новая заявка № {order_number} ({flow_label})",
            ]
            if author_line:
                caption_lines.append(author_line)
            caption_lines.append(f"Количество кодов: {codes_total}")
            caption_lines.append(
                f"Комментарий: {effective_comment if effective_comment else '—'}"
            )
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

            # Лички админам
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
                    try:
                        await register_order_telegram_posting(
                            order_id, sent.chat.id, sent.message_id
                        )
                    except Exception as reg_err:
                        logger.warning(
                            "register_order_telegram_posting order=%s: %s",
                            order_id,
                            reg_err,
                        )
                except Exception as e:
                    logger.exception("Send markznak to admin %s failed: %s", admin_id, e)
        finally:
            os.unlink(tmp_path)

        for att in extras:
            fid = att.get("telegram_file_id")
            if not fid:
                continue
            fn = att.get("file_name") or "файл"
            for admin_id in admin_ids:
                try:
                    if _is_photo_filename(fn):
                        await message.bot.send_photo(
                            chat_id=admin_id,
                            photo=fid,
                        )
                    else:
                        await message.bot.send_document(
                            chat_id=admin_id,
                            document=fid,
                        )
                except Exception as e:
                    logger.exception(
                        "Send extra attachment to admin %s failed: %s", admin_id, e
                    )
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
    await state.update_data(template_flow="repeat")
    await message.answer(
        "Введите артикул или часть наименования товара для поиска.\n"
        "Вы получите шаблон — заполните колонку «Количество» и отправьте файл обратно.",
        reply_markup=back_kb(),
    )
    await state.set_state("template:article")


@router.message(StateFilter("template:product_choice"), F.text == "Новый товар")
async def template_new_product(message: Message, state: FSMContext):
    """Новый: справочники (ЮЛ, бренд, страна, пол) → пустой шаблон → файл."""
    await state.update_data(template_flow="new")
    await message.answer("Выберите юридическое лицо:", reply_markup=legal_entity_kb())
    await state.set_state("template:legal_entity")


# --- Новый товар: юридическое лицо (inline) ---
@router.message(StateFilter("template:legal_entity"), F.text == "« Назад")
async def template_legal_entity_back_reply(message: Message, state: FSMContext):
    """Назад на выбор Повторный/Новый — только reply-кнопка «Назад» (инлайн убран)."""
    await message.answer(
        "Выберите способ добавления товара:",
        reply_markup=product_choice_kb(),
    )
    await state.set_state("template:product_choice")


@router.callback_query(StateFilter("template:legal_entity"), F.data == "back")
async def template_legal_entity_back(callback: CallbackQuery, state: FSMContext):
    """Совместимость: старые сообщения с инлайн «Назад» до обновления."""
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
        # Если справочник брендов пуст/недоступен, даём ввод вручную и показываем «Назад»,
        # чтобы не оставалась старая клавиатура предыдущего шага.
        await callback.message.answer("Введите бренд:", reply_markup=back_kb())
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
    await message.answer(
        "Отправьте заполненный шаблон в чат или воспользуйтесь кнопками ниже.",
        reply_markup=attach_file_kb(),
    )


# --- Повторный: ввод артикула/наименования ---
@router.message(StateFilter("template:article"), F.text == "« Назад")
async def template_article_back(message: Message, state: FSMContext):
    await message.answer("Выберите способ добавления товара:", reply_markup=product_choice_kb())
    await state.set_state("template:product_choice")
    return


@router.message(StateFilter("template:article"), F.text)
async def process_article_template(message: Message, state: FSMContext):
    """Повторный товар: артикул/наименование → при необходимости ЮЛ и страна → шаблон."""
    article = message.text.strip()
    if len(article) < 2:
        await message.answer("Введите минимум 2 символа.")
        return
    await state.update_data(
        article=article,
        repeat_legal_entity=None,
        repeat_country=None,
        repeat_legal_entity_options=None,
        repeat_country_label_to_db=None,
    )
    data = await state.get_data()
    category = data.get("category")
    try:
        legal_entities = await get_template_legal_entities(article, category=category)
    except Exception as e:
        logger.exception("Template legal entities fetch failed: %s", e)
        if "404" in str(e) or "не найдены" in str(e).lower():
            await message.answer("Товары по запросу не найдены.")
        else:
            await message.answer("Ошибка загрузки данных. Попробуйте позже.")
        return
    if len(legal_entities) <= 1:
        le = legal_entities[0] if legal_entities else None
        await state.update_data(repeat_legal_entity=le)
        await _proceed_repeat_country_step(message, state)
        return
    await state.update_data(repeat_legal_entity_options=legal_entities)
    await message.answer(
        "Найдено несколько юридических лиц. Выберите нужное:",
        reply_markup=repeat_template_choices_kb(legal_entities),
    )
    await state.set_state("template:repeat_legal_entity")


@router.message(StateFilter("template:repeat_legal_entity"), F.text == "« Назад")
async def template_repeat_legal_entity_back(message: Message, state: FSMContext):
    await state.update_data(repeat_legal_entity_options=None)
    await message.answer(
        "Введите артикул или часть наименования товара для поиска.\n"
        "Вы получите шаблон — заполните колонку «Количество» и отправьте файл обратно.",
        reply_markup=back_kb(),
    )
    await state.set_state("template:article")


@router.message(StateFilter("template:repeat_legal_entity"), F.text)
async def template_repeat_legal_entity_pick(message: Message, state: FSMContext):
    choice = (message.text or "").strip()
    data = await state.get_data()
    options = data.get("repeat_legal_entity_options") or []
    if choice not in options:
        await message.answer("Выберите юридическое лицо с клавиатуры ниже.")
        return
    await state.update_data(repeat_legal_entity=choice)
    await _proceed_repeat_country_step(message, state)


@router.message(StateFilter("template:repeat_country"), F.text == "« Назад")
async def template_repeat_country_back(message: Message, state: FSMContext):
    data = await state.get_data()
    options = data.get("repeat_legal_entity_options") or []
    await state.update_data(repeat_country_label_to_db=None)
    if len(options) > 1:
        await message.answer(
            "Найдено несколько юридических лиц. Выберите нужное:",
            reply_markup=repeat_template_choices_kb(options),
        )
        await state.set_state("template:repeat_legal_entity")
        return
    await message.answer(
        "Введите артикул или часть наименования товара для поиска.\n"
        "Вы получите шаблон — заполните колонку «Количество» и отправьте файл обратно.",
        reply_markup=back_kb(),
    )
    await state.set_state("template:article")


@router.message(StateFilter("template:repeat_country"), F.text)
async def template_repeat_country_pick(message: Message, state: FSMContext):
    data = await state.get_data()
    mapping = data.get("repeat_country_label_to_db") or {}
    label = (message.text or "").strip()
    db_country = mapping.get(label)
    if db_country is None:
        await message.answer("Выберите страну с клавиатуры ниже.")
        return
    await state.update_data(repeat_country=db_country)
    await _send_repeat_template_excel(message, state)


# --- Комментарий к заявке (общий для нового/повторного товара по шаблону) ---
@router.message(StateFilter("template:comment"), F.text)
async def template_comment_set(message: Message, state: FSMContext):
    # Старая точка входа комментария больше не используется в новом сценарии,
    # но обрабатываем её безопасно, если пользователь каким-то образом сюда попал.
    await state.update_data(comment=message.text.strip())
    await message.answer(
        "Комментарий сохранён. Отправьте заполненный шаблон в чат (файл Excel .xlsx).",
        reply_markup=attach_file_kb(),
    )
    await state.set_state("template:await_file")


@router.message(StateFilter("template:await_file"), F.text == "« Назад")
async def template_file_back(message: Message, state: FSMContext):
    # Шаг назад внутри процесса «Заявка по шаблону», а не выход в главное меню.
    from_user = message.from_user
    if not from_user:
        return

    data = await state.get_data()
    # Если article присутствует — это сценарий "повторный товар".
    prev_state = "template:article" if data.get("article") else "template:new_gender"

    # Сбрасываем данные уже созданной заявки (если пользователь вернулся назад после оформления).
    order_keys = [
        "order_id",
        "order_number",
        "order_items_count",
        "order_codes_total",
        "markznak_sent",
        "author_username",
        "author_full_name",
        "author_id",
        "comment",
    ]
    for k in order_keys:
        data.pop(k, None)
    await state.update_data(**data)
    await state.set_state(prev_state)

    if prev_state == "template:article":
        await message.answer(
            "Введите наименование товара (для запроса шаблона):",
            reply_markup=back_kb(),
        )
    else:
        await message.answer(
            "Выберите целевой пол:",
            reply_markup=target_gender_kb(),
        )
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
    await message.answer(
        "Отправьте заполненный шаблон в формате Excel (.xlsx).",
        reply_markup=attach_file_kb(),
    )


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
    codes_total = sum(int(i.get("quantity") or 0) for i in (order.get("items") or []))
    tf = data.get("template_flow")
    if not tf:
        tf = "repeat" if data.get("article") else "new"

    await state.update_data(
        order_id=order["id"],
        order_number=order["number"],
        order_items_count=len(order.get("items", [])),
        order_codes_total=codes_total,
        template_flow=tf,
        # Отправка админам должна происходить только после опросника/подтверждения.
        markznak_sent=False,
        author_username=user.username,
        author_full_name=user.full_name,
        author_id=user.id,
    )
    await state.set_state("template:order_comment")
    await message.answer(
        f"Заявка № {order['number']} создана. Количество кодов: {codes_total}.\n\n"
        "Введите комментарий к заявке или нажмите «Пропустить».",
        reply_markup=final_comment_kb(),
    )


# --- Комментарий сразу после создания заявки ---
@router.message(StateFilter("template:order_comment"), F.text == "« Назад")
async def template_order_comment_back(message: Message, state: FSMContext):
    await _rollback_template_order_to_await_file(message, state)


@router.message(StateFilter("template:order_comment"), F.text == "⏭ Пропустить")
async def template_order_comment_skip(message: Message, state: FSMContext):
    await state.update_data(comment=None)
    await state.set_state("template:await_extra_file")
    await message.answer(
        "Хотите прикрепить дополнительный файл? (любой формат)",
        reply_markup=done_extra_kb(),
    )


@router.message(StateFilter("template:order_comment"), F.text)
async def template_order_comment_set(message: Message, state: FSMContext):
    text = message.text.strip()
    await state.update_data(comment=text)
    data = await state.get_data()
    oid = data.get("order_id")
    if oid:
        try:
            await set_order_comment(int(oid), text)
        except Exception as e:
            logger.exception("set_order_comment failed: %s", e)
    await state.set_state("template:await_extra_file")
    await message.answer(
        "Комментарий сохранён.\n\n"
        "Хотите прикрепить дополнительный файл? (любой формат)",
        reply_markup=done_extra_kb(),
    )


# --- Доп. файл после создания заявки ---
@router.message(StateFilter("template:await_extra_file"), F.text == "« Назад")
async def template_extra_back(message: Message, state: FSMContext):
    await _rollback_template_order_to_await_file(message, state)


@router.message(StateFilter("template:await_extra_file"), F.text == "✅ Готово")
async def template_extra_done(message: Message, state: FSMContext):
    data = await state.get_data()
    order_number = data.get("order_number", "")
    order_id = data.get("order_id")
    items_count = data.get("order_items_count") or 0
    markznak_sent = data.get("markznak_sent")

    if order_id and order_number and not markznak_sent:
        await state.set_state("template:final_approval")
        await message.answer(
            "Заявка может быть оформлена?",
            reply_markup=final_approval_kb(),
        )
        return

    await state.clear()
    from bot.handlers.main_menu import is_admin as _is_admin
    uid = message.from_user.id if message.from_user else 0
    is_adm = await _is_admin(uid)
    await message.answer("Готово.", reply_markup=main_menu_kb(is_admin=is_adm))
    return


@router.message(StateFilter("template:await_extra_file"), F.text == "📎 Прикрепить доп. файл")
async def template_extra_ask(message: Message, state: FSMContext):
    await message.answer("Отправьте документ или фото сообщением в чат.")


async def _finalize_order_with_comment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    order_number = data.get("order_number", "")
    order_id = data.get("order_id")
    items_count = data.get("order_items_count") or 0
    markznak_sent = data.get("markznak_sent")

    if order_id and order_number and not markznak_sent:
        # Финальная отправка админам только после подтверждения.
        try:
            from bot.api_client import get_order as _get_order

            full = await _get_order(int(order_id))
        except Exception as e:
            logger.exception("Finalize: get_order failed: %s", e)
            full = None
        if full:
            try:
                res = await notify_order_to_admins(message.bot, full)
                if not res.delivered_any:
                    enqueue_pending(int(order_id))
            except Exception as e:
                logger.exception("Finalize: notify_order_to_admins failed: %s", e)
                enqueue_pending(int(order_id))
        else:
            enqueue_pending(int(order_id))
        await state.update_data(markznak_sent=True)

    await state.clear()
    from bot.handlers.main_menu import is_admin as _is_admin
    uid = message.from_user.id if message.from_user else 0
    is_adm = await _is_admin(uid)
    await message.answer("Готово.", reply_markup=main_menu_kb(is_admin=is_adm))


@router.callback_query(StateFilter("template:order_comment"), F.data == "tmpl_final_skip_comment")
async def template_order_comment_skip_cb(callback: CallbackQuery, state: FSMContext):
    await state.update_data(comment=None)
    await state.set_state("template:await_extra_file")
    await callback.message.answer(
        "Хотите прикрепить дополнительный файл? (любой формат)",
        reply_markup=done_extra_kb(),
    )
    await callback.answer()


@router.callback_query(StateFilter("template:final_comment"), F.data == "tmpl_final_skip_comment")
async def template_final_comment_skip(callback: CallbackQuery, state: FSMContext):
    # Совместимость: старые сессии после шага доп. файла.
    await state.update_data(comment=None)
    await state.set_state("template:final_approval")
    await callback.message.answer(
        "Комментарий пропущен.\n\n"
        "Заявка может быть оформлена?",
        reply_markup=final_approval_kb(),
    )
    await callback.answer()


@router.message(StateFilter("template:final_comment"), F.text == "⏭ Пропустить")
async def template_final_comment_skip_text(message: Message, state: FSMContext):
    await state.update_data(comment=None)
    await state.set_state("template:final_approval")
    await message.answer(
        "Комментарий пропущен.\n\n"
        "Заявка может быть оформлена?",
        reply_markup=final_approval_kb(),
    )


@router.message(StateFilter("template:final_comment"), F.text)
async def template_final_comment_set(message: Message, state: FSMContext):
    if message.text == "« Назад":
        # Возврат в этап доп. файлов без завершения заявки
        await state.update_data(comment=None)
        await state.set_state("template:await_extra_file")
        await message.answer(
            "Вы можете прикрепить дополнительный файл или нажать «Готово».",
            reply_markup=done_extra_kb(),
        )
        return

    await state.update_data(comment=message.text.strip())
    await state.set_state("template:final_approval")
    await message.answer(
        "Комментарий сохранён.\n\n"
        "Заявка может быть оформлена?",
        reply_markup=final_approval_kb(),
    )


@router.message(StateFilter("template:final_approval"), F.text == "« Назад")
async def template_final_approval_back(message: Message, state: FSMContext):
    await state.set_state("template:await_extra_file")
    await message.answer(
        "Вы можете прикрепить дополнительный файл или нажать «Готово».",
        reply_markup=done_extra_kb(),
    )


@router.message(StateFilter("template:final_approval"), F.text.in_(["Да", "✅ Да"]))
async def template_final_approval_yes(message: Message, state: FSMContext):
    await _finalize_order_with_comment(message, state)


@router.message(StateFilter("template:final_approval"), F.text.in_(["Нет", "❌ Нет"]))
async def template_final_approval_no(message: Message, state: FSMContext):
    await state.set_state("template:await_extra_file")
    await message.answer(
        "Ок, заявку пока не оформляю.\n"
        "Вы можете прикрепить дополнительный файл или снова нажать «Готово».",
        reply_markup=done_extra_kb(),
    )


async def _register_template_extra_attachment(
    message: Message,
    state: FSMContext,
    *,
    telegram_file_id: str,
    file_name: str | None,
) -> None:
    """Приём доп. файла/фото — в БД; админам вложения уйдут после «Готово» вместе с МаркЗнак."""
    user = message.from_user
    if not user:
        return

    data = await state.get_data()
    order_id = data.get("order_id")

    attached_to_order = False
    if order_id:
        try:
            await add_order_attachment(
                int(order_id),
                author_telegram_id=user.id,
                telegram_file_id=telegram_file_id,
                file_name=file_name,
            )
            attached_to_order = True
        except Exception as e:
            logger.exception("Register order attachment failed: %s", e)
    else:
        logger.warning("Extra file: no order_id in FSM, file not linked to order")

    if attached_to_order:
        user_reply = (
            "Файл сохранён в заявке.\n\n"
            "Прикрепить ещё один файл или фото или нажмите «Готово»."
        )
    elif order_id:
        user_reply = (
            "Не удалось привязать файл к заявке на сервере. "
            "Попробуйте отправить файл ещё раз; если снова не получится — напишите администратору.\n\n"
            "Прикрепить ещё или нажмите «Готово»."
        )
    else:
        user_reply = (
            "Заявка в сессии не найдена — файл не сохранён. Начните оформление заявки заново.\n\n"
            "Прикрепить ещё или нажмите «Готово»."
        )

    await message.answer(user_reply, reply_markup=done_extra_kb())


@router.message(StateFilter("template:await_extra_file"), F.document)
async def process_extra_file(message: Message, state: FSMContext):
    doc = message.document
    if not doc:
        return
    await _register_template_extra_attachment(
        message,
        state,
        telegram_file_id=doc.file_id,
        file_name=doc.file_name,
    )


@router.message(StateFilter("template:await_extra_file"), F.photo)
async def process_extra_photo(message: Message, state: FSMContext):
    """Фото в чат (не как документ): сохраняем file_id крупнейшего размера."""
    if not message.photo:
        return
    ph = message.photo[-1]
    name = f"photo_{ph.file_unique_id}.jpg"
    await _register_template_extra_attachment(
        message,
        state,
        telegram_file_id=ph.file_id,
        file_name=name,
    )


@router.message(StateFilter("template:await_extra_file"))
async def template_extra_unexpected(message: Message, _state: FSMContext):
    """Не документ/фото — подсказка вместо общего fallback (кнопки обрабатываются выше по регистрации)."""
    await message.answer(
        "На этом шаге отправьте файл или фото вложением либо нажмите кнопку ниже.",
        reply_markup=done_extra_kb(),
    )
