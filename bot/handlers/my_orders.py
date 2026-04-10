"""My orders handler."""
import html
import logging
import os
import re
import tempfile

import httpx
from aiogram import Router, F, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    BufferedInputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from config import get_settings
from bot.api_client import (
    get_orders,
    get_order,
    update_order,
    get_order_excel,
    get_markznak_order_excel,
    list_admins,
    get_user,
    delete_order as api_delete_order,
    delete_order_admin as api_delete_order_admin,
    admin_telegram_ids_for_notify,
    try_repair_responsible_telegram_self,
    list_order_telegram_postings,
    clear_order_telegram_postings,
    register_order_telegram_posting,
)
from bot.admin_my_orders_list import filter_admin_my_orders_rows, load_admin_my_orders_source
from bot.keyboards import main_menu_kb, orders_list_inline, order_detail_back_kb
from bot.notification_registry import notifications_registry
from bot.handlers.main_menu import is_admin
from bot.handlers.history import active_admin_ids_frozen, active_admin_username_norms_frozen
from backend.services.excel_service import (
    get_markznak_download_filename,
    get_order_excel_download_filename,
)

router = Router()
logger = logging.getLogger(__name__)

ORDERS_PER_PAGE = 8


async def _all_admin_ids_for_broadcast() -> list[int]:
    """Собрать максимальный пул админов: ENV + merged notify + role=admin в БД."""
    ids: set[int] = set()
    try:
        ids.update(int(x) for x in get_settings().admin_ids_list)
    except Exception:
        pass
    try:
        ids.update(await admin_telegram_ids_for_notify())
    except Exception as e:
        logger.warning("admin_telegram_ids_for_notify failed: %s", e)
    try:
        for row in await list_admins():
            tid = row.get("telegram_id")
            if tid is None:
                continue
            ids.add(int(tid))
    except Exception as e:
        logger.warning("list_admins failed while collecting admin broadcast ids: %s", e)
    return sorted(ids)


async def _purge_admin_order_telegram_cards(
    bot,
    order_id: int,
    *,
    skip_chat_id: int | None = None,
    skip_message_id: int | None = None,
) -> None:
    """Удалить у админов сообщения с МаркЗнак: записи в БД + in-memory реестр."""
    try:
        db_rows = await list_order_telegram_postings(order_id)
    except Exception as e:
        logger.warning("list_order_telegram_postings(%s) failed: %s", order_id, e)
        db_rows = []
    mem_entries = notifications_registry.pop_order(order_id)
    seen: set[tuple[int, int]] = set()

    async def _try_delete(cid: int, mid: int) -> None:
        key = (cid, mid)
        if key in seen:
            return
        seen.add(key)
        if skip_chat_id is not None and skip_message_id is not None:
            if cid == skip_chat_id and mid == skip_message_id:
                return
        try:
            await bot.delete_message(chat_id=cid, message_id=mid)
        except Exception:
            pass

    for row in db_rows:
        await _try_delete(int(row["chat_id"]), int(row["message_id"]))
    for entry in mem_entries:
        await _try_delete(int(entry.chat_id), int(entry.message_id))

    try:
        await clear_order_telegram_postings(order_id)
    except Exception as e:
        logger.warning("clear_order_telegram_postings(%s) failed: %s", order_id, e)


async def _show_my_orders_list_for_user(
    bot,
    state: FSMContext,
    *,
    user_id: int,
    chat_id: int,
    header_notice: str | None = None,
) -> None:
    """Тот же список, что по «📦 Мои заявки», опционально с предупреждением сверху."""
    await state.clear()
    is_adm = await is_admin(user_id)
    try:
        if is_adm:
            raw = await load_admin_my_orders_source(user_id)
            orders = filter_admin_my_orders_rows(raw, None)
        else:
            orders = await get_orders(author_telegram_id=user_id, limit=100)
    except Exception as e:
        logger.exception("Get orders for list after notice failed: %s", e)
        tail = "Ошибка загрузки заявок. Попробуйте «📦 Мои заявки»."
        text = f"{header_notice.strip()}\n\n{tail}" if header_notice else tail
        await bot.send_message(chat_id=chat_id, text=text)
        return

    filter_mode = "my_admin" if is_adm else "my_user"
    notice_block = (header_notice.strip() + "\n\n") if header_notice else ""

    if not orders:
        empty = "У вас пока нет заявок в работе." if is_adm else "У вас пока нет заявок."
        await bot.send_message(chat_id=chat_id, text=f"{notice_block}{empty}")
        return

    if is_adm:
        items = [(o["id"], o["number"], o["status"]) for o in orders]
        title_base = "Ваши заявки (все назначенные на вас):\n\nВыберите заявку:"
    else:
        items = [
            (o["id"], o["number"], _user_visible_status(o["status"])) for o in orders
        ]
        title_base = "Ваши заявки:\n\nВыберите заявку для просмотра:"

    has_next = len(orders) > ORDERS_PER_PAGE
    title = f"{notice_block}{title_base}"
    status_key = "all" if is_adm else None
    await bot.send_message(
        chat_id=chat_id,
        text=title,
        reply_markup=orders_list_inline(
            items,
            page=0,
            has_next=has_next,
            prefix="ord",
            show_filters=True,
            current_filter=status_key,
            filter_mode=filter_mode,
        ),
    )
    await state.update_data(
        orders=orders,
        page=0,
        mode="my",
        status_filter=status_key,
        is_admin_in_my=is_adm,
    )


def _fmt_order_dt(val) -> str:
    if val is None:
        return "—"
    s = val if isinstance(val, str) else val.isoformat()
    return s[:19].replace("T", " ")


def _order_codes_total(order: dict) -> int:
    return sum(int(i.get("quantity") or 0) for i in (order.get("items") or []))


def _is_photo_filename(file_name: str | None) -> bool:
    """Определить, что вложение — фото, по расширению имени файла."""
    if not file_name:
        return False
    name = file_name.strip().lower()
    return name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))


def _author_display(order: dict) -> str:
    un = order.get("author_username")
    fn = (order.get("author_full_name") or "").strip()
    tid = order.get("author_telegram_id")
    if un:
        return html.escape(f"@{un}")
    if fn:
        return html.escape(fn)
    return html.escape(str(tid or "—"))


def _format_order_compact_html(
    order: dict,
    *,
    adm: bool,
    user_to_index: dict | None,
) -> str:
    from bot.handlers.history import _admin_color_label as _history_color_label

    shown_status = order["status"]
    if not adm:
        shown_status = _user_visible_status(shown_status)
    lines = [
        f"<b>Заявка № {html.escape(str(order.get('number') or ''))}</b>",
        f"Статус: {html.escape(str(shown_status))}",
    ]
    if order.get("deleted_at"):
        lines.append("<i>В корзине (удалена)</i>")
    resp = order.get("responsible_username")
    resp_id = order.get("responsible_telegram_id")
    if resp or resp_id:
        lines.append(
            f"Ответственный: {_history_color_label(resp_id, resp, user_to_index)}"
        )
    else:
        lines.append("Ответственный: не назначен")
    return "\n".join(lines)


def _format_order_details_html(order: dict) -> str:
    created = _fmt_order_dt(order.get("created_at"))
    codes = _order_codes_total(order)
    author = _author_display(order)
    parts = [
        "<b>Подробности</b>",
        f"Дата создания: {html.escape(created)}",
        f"Количество кодов: {codes}",
    ]
    if order.get("deleted_at"):
        parts.append("<b>Заявка в корзине</b> (мягкое удаление)")
    raw_status = (order.get("status") or "").strip()
    if raw_status == "отправлена":
        ut = order.get("updated_at")
        if ut is not None and str(ut).strip():
            sent_line = _fmt_order_dt(ut)
            if sent_line and sent_line != "—":
                parts.append(f"Дата отправки: {html.escape(sent_line)}")
    parts.append(f"Создал: {author}")
    resp = order.get("responsible_username")
    resp_id = order.get("responsible_telegram_id")
    if resp or resp_id is not None:
        rlabel = f"@{resp}" if resp else str(resp_id or "")
        parts.append(f"Ответственный: {html.escape(rlabel)}")
    else:
        parts.append("Ответственный: не назначен")
    link = order.get("yandex_link")
    if link:
        parts.append(f"Ссылка на файлы: {html.escape(str(link))}")
    extras = order.get("extra_attachments") or []
    if extras:
        parts.append("")
        parts.append("<b>Дополнительные файлы:</b>")
        for i, att in enumerate(extras, 1):
            fn = att.get("file_name") or "файл"
            parts.append(f"{i}. {html.escape(str(fn))}")
    return "\n".join(parts)


# Подпись к документу в Telegram — не длиннее 1024 символов.
_TG_DOCUMENT_CAPTION_MAX = 1024


def _telegram_document_caption(details_html: str) -> tuple[str, ParseMode | None]:
    """Возвращает (caption, parse_mode). При переполнении — подпись без HTML, чтобы не резать теги."""
    if len(details_html) <= _TG_DOCUMENT_CAPTION_MAX:
        return details_html, ParseMode.HTML
    plain = re.sub(r"<[^>]+>", " ", details_html)
    plain = html.unescape(plain)
    plain = " ".join(plain.split())
    if len(plain) <= _TG_DOCUMENT_CAPTION_MAX:
        return plain, None
    return plain[: _TG_DOCUMENT_CAPTION_MAX - 1] + "…", None


async def _safe_edit_card(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    *,
    parse_mode: str = "HTML",
) -> None:
    """Универсально обновить сообщение-карточку (text vs caption)."""
    msg = callback.message
    if not msg:
        return
    try:
        if getattr(msg, "caption", None) is not None and msg.text is None:
            await msg.edit_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        await msg.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def _load_admin_color_index() -> dict:
    from bot.handlers.history import _load_admins_tuples, _build_user_color_mapping

    admins_tuples = await _load_admins_tuples()
    full_orders = await get_orders(admin=True, include_deleted=True, limit=100)
    user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)
    return user_to_index


async def _render_order_card(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    order_id: int,
    show_more_button: bool = True,
) -> None:
    """Перерисовать карточку заявки (работает и для text, и для caption)."""
    try:
        order = await get_order(order_id)
    except Exception as e:
        logger.exception("Get order failed: %s", e)
        await callback.answer("Ошибка загрузки заявки.", show_alert=True)
        return
    if not order:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    adm = await is_admin(callback.from_user.id) if callback.from_user else False
    has_resp = bool(order.get("responsible_username") or order.get("responsible_telegram_id"))
    if adm or has_resp:
        user_to_index = await _load_admin_color_index()
    else:
        user_to_index = None

    show_status_btns = adm
    uid = callback.from_user.id if callback.from_user else 0
    show_user_delete = (
        not adm
        and order.get("author_telegram_id") == uid
        and (order.get("status") or "") == "создана"
        and not order.get("deleted_at")
    )
    text = _format_order_compact_html(order, adm=adm, user_to_index=user_to_index)
    markup = order_detail_back_kb(
        is_admin=show_status_btns,
        order_id=order_id,
        current_status=order.get("status"),
        show_more_button=show_more_button,
        in_trash=bool(order.get("deleted_at")),
        show_user_delete=show_user_delete,
    )
    await _safe_edit_card(callback, text, markup, parse_mode="HTML")
    await state.update_data(selected_order_id=order_id)
    await callback.answer()


def _user_visible_status(status: str) -> str:
    """Маппинг внутренних статусов в пользовательские.

    Для пользователя есть только три состояния:
    - создана;
    - в работе (включает внутренние «в работе» и «готово»);
    - готова (внутренний статус «отправлена»).
    """
    if status == "готово":
        return "в работе"
    if status == "отправлена":
        return "готова"
    return status


@router.message(F.text == "📦 Мои заявки")
async def my_orders(message: Message, state: FSMContext):
    """Список заявок: для пользователя — созданные им; для админа — где он ответственный."""
    if not message.from_user:
        return
    await _show_my_orders_list_for_user(
        message.bot,
        state,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        header_notice=None,
    )


@router.callback_query(F.data.startswith("ord:"))
async def order_select(callback: CallbackQuery, state: FSMContext):
    """Показать краткую карточку заявки (файл — только по кнопке «Подробнее»)."""
    order_id = int(callback.data.split(":")[1])
    await _render_order_card(callback, state, order_id=order_id)


@router.callback_query(F.data.startswith("ordmore:"))
async def order_more_details(callback: CallbackQuery, state: FSMContext):
    """Детали в подписи к одному Excel: админ — расширенный МаркЗнак (GTIN и др.), автор — краткий."""
    if not callback.from_user or not callback.message:
        await callback.answer("Ошибка.", show_alert=True)
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    try:
        order = await get_order(order_id)
    except Exception as e:
        logger.exception("Get order (ordmore) failed: %s", e)
        await callback.answer("Ошибка загрузки заявки.", show_alert=True)
        return
    if not order:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    adm = await is_admin(callback.from_user.id)
    if not adm:
        if order.get("author_telegram_id") != callback.from_user.id:
            await callback.answer("Доступ запрещён.", show_alert=True)
            return

    await callback.answer()

    details_html = _format_order_details_html(order)
    caption, cap_parse_mode = _telegram_document_caption(details_html)

    chat_id = callback.message.chat.id

    async def _send_homogeneous_media_group(
        atts: list[dict],
        *,
        as_photo: bool,
    ) -> None:
        """Альбом только из фото или только из документов (Telegram не смешивает с Excel-документом)."""
        i = 0
        while i < len(atts):
            chunk = atts[i : i + 10]
            i += 10
            if len(chunk) == 1:
                att = chunk[0]
                fid = att["telegram_file_id"]
                if as_photo:
                    await callback.bot.send_photo(chat_id=chat_id, photo=fid)
                else:
                    await callback.bot.send_document(chat_id=chat_id, document=fid)
            else:
                if as_photo:
                    media = [InputMediaPhoto(media=a["telegram_file_id"]) for a in chunk]
                else:
                    media = [InputMediaDocument(media=a["telegram_file_id"]) for a in chunk]
                await callback.bot.send_media_group(chat_id=chat_id, media=media)

    try:
        # Админу в «Мои заявки» и в «Истории» — расширенный файл (GTIN, МаркЗнак);
        # автору-не-админу — краткий Excel по заявке.
        if adm:
            excel_bytes = await get_markznak_order_excel(order_id)
            filename = get_markznak_download_filename(order["number"])
        else:
            excel_bytes = await get_order_excel(order_id)
            filename = get_order_excel_download_filename(order["number"])

        extras = [a for a in (order.get("extra_attachments") or []) if a.get("telegram_file_id")]
        photo_extras = [a for a in extras if _is_photo_filename(a.get("file_name"))]
        doc_extras = [a for a in extras if not _is_photo_filename(a.get("file_name"))]

        # Документы (xlsx + txt и т.д.) можно слить в один send_media_group (до 10 шт.).
        # Фото в тот же альбом положить нельзя — будет второе сообщение (альбом или одно фото).
        if doc_extras:
            rest_docs = list(doc_extras)
            first_cap = 9
            first_batch: list[InputMediaDocument] = [
                InputMediaDocument(
                    media=BufferedInputFile(excel_bytes, filename),
                    caption=caption,
                    parse_mode=cap_parse_mode,
                )
            ]
            for att in rest_docs[:first_cap]:
                first_batch.append(InputMediaDocument(media=att["telegram_file_id"]))
            await callback.bot.send_media_group(chat_id=chat_id, media=first_batch)
            rest_docs = rest_docs[first_cap:]
            while rest_docs:
                chunk = rest_docs[:10]
                rest_docs = rest_docs[10:]
                if len(chunk) == 1:
                    await callback.bot.send_document(
                        chat_id=chat_id,
                        document=chunk[0]["telegram_file_id"],
                    )
                else:
                    await callback.bot.send_media_group(
                        chat_id=chat_id,
                        media=[InputMediaDocument(media=a["telegram_file_id"]) for a in chunk],
                    )
        else:
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
                f.write(excel_bytes)
                tmp_path = f.name
            try:
                doc = FSInputFile(tmp_path, filename=filename)
                await callback.message.answer_document(
                    document=doc,
                    caption=caption,
                    parse_mode=cap_parse_mode,
                )
            finally:
                os.unlink(tmp_path)

        if photo_extras:
            await _send_homogeneous_media_group(photo_extras, as_photo=True)
    except Exception as e:
        logger.exception("Send order details / media group after ordmore failed: %s", e)


@router.callback_query(F.data == "ord_back")
async def ord_list_back_to_main(callback: CallbackQuery, state: FSMContext):
    """Кнопка «Назад» из списка — шаг назад по фильтрам.

    История заявок (админ):
    - если выбран конкретный админ (admin_filter) → сбросить до "Все админы";
    - иначе если выбран конкретный статус (status_filter != all) → сбросить до "Все";
    - иначе если мы внутри раздела (filters_collapsed=True) → вернуться к выбору категорий (показать кнопки статусов);
    - иначе → в главное меню.

    Мои заявки:
    - если выбран фильтр статуса → сбросить фильтр;
    - иначе → в главное меню.
    """
    if not callback.from_user:
        await callback.answer("Ошибка.", show_alert=True)
        return

    data = await state.get_data()
    mode = data.get("mode", "my")
    user_id = callback.from_user.id

    # Если state потерялся/очистился, определяем режим по самой клавиатуре:
    # в истории заявок есть кнопки фильтра админов (admflt:*), в моих заявках их нет.
    try:
        kb = callback.message.reply_markup.inline_keyboard if (callback.message and callback.message.reply_markup) else []
        has_admin_filters = any(
            (btn.callback_data or "").startswith("admflt:")
            for row in kb
            for btn in row
        )
        if has_admin_filters:
            mode = "history"
    except Exception:
        pass

    try:
        # ===== History mode (admin) =====
        if mode == "history":
            from bot.handlers.main_menu import is_admin as _is_admin
            if not await _is_admin(user_id):
                await callback.answer("Доступ запрещён.", show_alert=True)
                return

            from bot.handlers.history import (
                _load_admins_tuples,
                _build_user_color_mapping,
                _build_admin_filter_buttons,
                fetch_history_orders_list,
                fetch_history_full_orders_for_colors,
                _history_order_row_caption,
                _history_trash_inline_kw,
                _pretty_status_key,
            )

            status_key = data.get("status_filter") or "all"
            selected_admin_id = data.get("admin_filter")
            filters_collapsed = bool(data.get("filters_collapsed", False))

            # 1) Сначала откатываем фильтр по админу (к "Все админы").
            # При этом возвращаем UI в исходное состояние с видимыми категориями (show_filters=True),
            # чтобы не прятать категории под одной кнопкой.
            if selected_admin_id is not None:
                selected_admin_id = None
                orders = await fetch_history_orders_list(status_key, admin_filter_id=None)
                full_orders = await fetch_history_full_orders_for_colors()
                admins_tuples = await _load_admins_tuples()
                user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)
                admin_buttons = _build_admin_filter_buttons(
                    admins_tuples, user_to_index, selected_admin_id=None
                )
                aid = await active_admin_ids_frozen()
                unorms = active_admin_username_norms_frozen(admins_tuples)
                items = [
                    (
                        o["id"],
                        o["number"],
                        _history_order_row_caption(
                            o,
                            user_to_index,
                            in_trash_list=(status_key == "trash"),
                            active_admin_ids=aid,
                            active_admin_username_norms=unorms,
                        ),
                    )
                    for o in orders
                ]
                has_next = len(orders) > ORDERS_PER_PAGE
                title = f"==================================================\nИстория заявок ({_pretty_status_key(status_key)}):"
                tw = _history_trash_inline_kw(status_key, data)
                await _safe_edit_card(
                    callback,
                    f"{title}\n\nВыберите заявку для просмотра:",
                    orders_list_inline(
                        items,
                        page=0,
                        has_next=has_next,
                        prefix="ord",
                        show_filters=True,
                        current_filter=status_key,
                        filter_mode="history",
                        admin_labels=admin_buttons,
                        back_callback="hist_back",
                        **tw,
                    ),
                )
                await state.update_data(
                    orders=orders,
                    page=0,
                    mode="history",
                    status_filter=status_key,
                    admin_filter=None,
                    admin_labels=admin_buttons,
                    filters_collapsed=False,
                )
                return

            # 2) Потом откатываем фильтр статуса (к "Все") и тоже возвращаем исходный UI.
            if filters_collapsed and status_key != "all":
                status_key = "all"
                orders = await fetch_history_orders_list("all", admin_filter_id=None)
                full_orders = await fetch_history_full_orders_for_colors()
                admins_tuples = await _load_admins_tuples()
                user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)
                admin_buttons = _build_admin_filter_buttons(
                    admins_tuples, user_to_index, selected_admin_id=None
                )
                aid = await active_admin_ids_frozen()
                unorms = active_admin_username_norms_frozen(admins_tuples)
                items = [
                    (
                        o["id"],
                        o["number"],
                        _history_order_row_caption(
                            o,
                            user_to_index,
                            in_trash_list=False,
                            active_admin_ids=aid,
                            active_admin_username_norms=unorms,
                        ),
                    )
                    for o in orders
                ]
                has_next = len(orders) > ORDERS_PER_PAGE
                title = "==================================================\nИстория заявок (все):"
                tw = _history_trash_inline_kw("all", {**data, "trash_selected_ids": []})
                await _safe_edit_card(
                    callback,
                    f"{title}\n\nВыберите заявку для просмотра:",
                    orders_list_inline(
                        items,
                        page=0,
                        has_next=has_next,
                        prefix="ord",
                        show_filters=True,
                        current_filter="all",
                        filter_mode="history",
                        admin_labels=admin_buttons,
                        back_callback="hist_back",
                        **tw,
                    ),
                )
                await state.update_data(
                    orders=orders,
                    page=0,
                    mode="history",
                    status_filter="all",
                    admin_filter=None,
                    admin_labels=admin_buttons,
                    filters_collapsed=False,
                    trash_selected_ids=[],
                )
                return

            # 3) Потом возвращаемся к выбору категорий
            if filters_collapsed:
                orders = await fetch_history_orders_list("all", admin_filter_id=None)
                admins_tuples = await _load_admins_tuples()
                full_orders = await fetch_history_full_orders_for_colors()
                user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)
                admin_buttons = _build_admin_filter_buttons(
                    admins_tuples, user_to_index, selected_admin_id=None
                )
                aid = await active_admin_ids_frozen()
                unorms = active_admin_username_norms_frozen(admins_tuples)
                items = [
                    (
                        o["id"],
                        o["number"],
                        _history_order_row_caption(
                            o,
                            user_to_index,
                            in_trash_list=False,
                            active_admin_ids=aid,
                            active_admin_username_norms=unorms,
                        ),
                    )
                    for o in orders
                ]
                has_next = len(orders) > ORDERS_PER_PAGE
                tw = _history_trash_inline_kw("all", {**data, "trash_selected_ids": []})
                await _safe_edit_card(
                    callback,
                    "==================================================\nИстория заявок (все):\n\nВыберите заявку для просмотра:",
                    orders_list_inline(
                        items,
                        page=0,
                        has_next=has_next,
                        prefix="ord",
                        show_filters=True,
                        current_filter="all",
                        filter_mode="history",
                        admin_labels=admin_buttons,
                        back_callback="hist_back",
                        **tw,
                    ),
                )
                await state.update_data(
                    orders=orders,
                    page=0,
                    mode="history",
                    status_filter="all",
                    admin_filter=None,
                    admin_labels=admin_buttons,
                    filters_collapsed=False,
                    trash_selected_ids=[],
                )
                return

        # ===== My orders mode =====
        status_filter = data.get("status_filter")
        is_admin_in_my = data.get("is_admin_in_my", False)

        # Узкий фильтр (не «Все») — сбрасываем к полному списку; «Все»/None — выход в главное меню ниже.
        narrow = status_filter and str(status_filter) != "all"
        if narrow:
            if is_admin_in_my:
                raw = await load_admin_my_orders_source(user_id)
                orders = filter_admin_my_orders_rows(raw, None)
                filter_mode = "my_admin"
                items = [(o["id"], o["number"], o["status"]) for o in orders]
            else:
                orders = await get_orders(author_telegram_id=user_id, limit=100)
                filter_mode = "my_user"
                items = [(o["id"], o["number"], _user_visible_status(o["status"])) for o in orders]
            has_next = len(orders) > ORDERS_PER_PAGE
            # После сброса узкого фильтра помечаем «Все», чтобы повторный «Назад» ушёл в главное меню.
            next_filter = "all"
            await _safe_edit_card(
                callback,
                "Ваши заявки:\n\nВыберите заявку для просмотра:",
                orders_list_inline(
                    items,
                    page=0,
                    has_next=has_next,
                    prefix="ord",
                    show_filters=True,
                    current_filter=next_filter,
                    filter_mode=filter_mode,
                ),
            )
            await state.update_data(
                orders=orders,
                page=0,
                mode="my",
                status_filter=next_filter,
                is_admin_in_my=is_admin_in_my,
            )
            return

    except Exception as e:  # noqa: BLE001
        logger.exception("ord_back failed: %s", e)
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)
        return

    # дефолт: главное меню
    await state.clear()
    from bot.handlers.main_menu import is_admin as _is_admin
    is_adm = await _is_admin(user_id)
    await _safe_edit_card(callback, "Главное меню:", None)
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb(is_admin=is_adm))
    await callback.answer()


@router.callback_query(F.data.startswith("ordpg:"))
async def orders_page(callback: CallbackQuery, state: FSMContext):
    """Paginate orders list."""
    page = int(callback.data.split(":")[1])
    data = await state.get_data()
    orders = data.get("orders", [])
    mode = data.get("mode", "my")
    status_filter = data.get("status_filter")
    filters_collapsed = False
    trash_kw: dict = {}
    admin_buttons: list = []

    if mode == "history":
        from bot.handlers.history import (
            _load_admins_tuples,
            _build_user_color_mapping,
            _build_admin_filter_buttons,
            fetch_history_full_orders_for_colors,
            _history_order_row_caption,
            _history_trash_inline_kw,
        )

        filters_collapsed = bool(data.get("filters_collapsed", False))
        status_key = data.get("status_filter") or "all"
        selected_admin_id = data.get("admin_filter")
        admins_tuples = await _load_admins_tuples()
        full_orders = await fetch_history_full_orders_for_colors()
        user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)
        admin_buttons = _build_admin_filter_buttons(
            admins_tuples,
            user_to_index,
            selected_admin_id=selected_admin_id,
            collapse_others_when_selected=True,
        )
        aid = await active_admin_ids_frozen()
        unorms = active_admin_username_norms_frozen(admins_tuples)
        items = [
            (
                o["id"],
                o["number"],
                _history_order_row_caption(
                    o,
                    user_to_index,
                    in_trash_list=(status_key == "trash"),
                    active_admin_ids=aid,
                    active_admin_username_norms=unorms,
                ),
            )
            for o in orders
        ]
        filter_mode = "history"
        trash_kw = _history_trash_inline_kw(status_key, data)
    else:
        is_admin_in_my = data.get("is_admin_in_my", False)
        if is_admin_in_my:
            items = [(o["id"], o["number"], o["status"]) for o in orders]
            filter_mode = "my_admin"
        else:
            # Для пользователя маппим внутренние статусы в пользовательские.
            items = [
                (o["id"], o["number"], _user_visible_status(o["status"]))
                for o in orders
            ]
            filter_mode = "my_user"
    start = page * ORDERS_PER_PAGE
    has_next = len(orders) > start + ORDERS_PER_PAGE
    kw: dict = {}
    if mode in ("history", "my"):
        kw = {
            "show_filters": (mode != "history") or (not filters_collapsed),
            "current_filter": status_filter,
            "filter_mode": filter_mode,
        }
    if mode == "history" and filters_collapsed:
        kw["filters_back_callback"] = "fltmenu"
        kw["back_callback"] = "hist_back"
        kw["admin_labels"] = admin_buttons
        kw.update(trash_kw)
    elif mode == "history":
        kw["admin_labels"] = admin_buttons
        kw["back_callback"] = "hist_back"
        kw.update(trash_kw)
    await callback.message.edit_reply_markup(
        reply_markup=orders_list_inline(items, page=page, has_next=has_next, prefix="ord", **kw),
    )
    await state.update_data(page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("st:"))
async def change_order_status(callback: CallbackQuery, state: FSMContext):
    """Admin: change order status. Delete message -> show temp -> update -> show result."""
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка.", show_alert=True)
        return
    _, status_key, order_id_str = parts
    order_id = int(order_id_str)
    status_map = {"in_progress": "в работе", "ready": "готово", "sent": "отправлена"}
    status = status_map.get(status_key)
    if not status:
        await callback.answer("Неизвестный статус.", show_alert=True)
        return

    if status == "отправлена":
        try:
            order = await get_order(order_id)
        except Exception as e:
            logger.exception("Get order for link check failed: %s", e)
            await callback.answer("Ошибка загрузки заявки.", show_alert=True)
            return
        if not order:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return
        if not order.get("yandex_link"):
            await callback.answer(
                "Нельзя перевести в «отправлена» без прикреплённой ссылки. "
                "Сначала добавьте ссылку на файлы.",
                show_alert=True,
            )
            return

    await callback.answer()
    chat_id = callback.message.chat.id

    try:
        await callback.message.delete()
    except Exception:
        pass

    temp_msg = await callback.bot.send_message(chat_id, "Меняю статус...")
    try:
        updated = await update_order(order_id, status=status)
    except Exception as e:
        logger.exception("Update status failed: %s", e)
        await temp_msg.edit_text("Ошибка обновления. Попробуйте позже.")
        return
    if not updated:
        await temp_msg.edit_text("Ошибка обновления.")
        return

    try:
        await temp_msg.delete()
    except Exception:
        pass

    order = await get_order(order_id)
    if not order:
        return

    user_to_index = await _load_admin_color_index()

    # Уведомление автору при смене статуса
    try:
        author_id = order.get("author_telegram_id")
        # Для статуса "в работе" уведомление отправляет хендлер "take".
        # Здесь для пользователя шлём только при финальном статусе "отправлена",
        # при этом текст говорит, что заявка готова.
        if author_id and status == "отправлена":
            text_parts = [f"Ваша заявка № {order['number']} готова."]
            link = order.get("yandex_link")
            if link:
                text_parts.append(f"Ссылка на файлы: {link}")
            await callback.bot.send_message(
                chat_id=author_id,
                text="\n".join(text_parts),
            )
    except Exception:
        # Не блокируем основной сценарий, если уведомление не получилось отправить
        pass

    await callback.bot.send_message(
        chat_id,
        _format_order_compact_html(order, adm=True, user_to_index=user_to_index),
        reply_markup=order_detail_back_kb(
            is_admin=True,
            order_id=order_id,
            current_status=order.get("status"),
            in_trash=bool(order.get("deleted_at")),
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("purge1:"))
async def admin_purge_one_from_trash(callback: CallbackQuery, state: FSMContext):
    """Окончательно удалить заявку из корзины (админ)."""
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    from bot.api_client import purge_trash_order_one

    try:
        await purge_trash_order_one(order_id, callback.from_user.id)
    except Exception as e:
        logger.exception("Purge one failed: %s", e)
        await callback.answer("Не удалось удалить. Проверьте, что заявка в корзине.", show_alert=True)
        return
    await callback.answer("Удалена навсегда.")
    try:
        await callback.message.edit_text(
            "Заявка удалена из базы.",
            reply_markup=None,
        )
    except Exception:
        pass
    await state.update_data(selected_order_id=None)


@router.callback_query(F.data.startswith("del_confirm:"))
async def confirm_delete_order(callback: CallbackQuery, state: FSMContext):
    """Показать подтверждение удаления заявки пользователем."""
    if not callback.from_user:
        await callback.answer("Ошибка.", show_alert=True)
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка.", show_alert=True)
        return

    try:
        order = await get_order(order_id)
    except Exception as e:
        logger.exception("Get order before delete failed: %s", e)
        await callback.answer("Ошибка загрузки заявки.", show_alert=True)
        return
    if not order:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    # Проверяем, что это его заявка и статус «создана».
    if order.get("author_telegram_id") != callback.from_user.id:
        await callback.answer("Вы можете удалить только свои заявки.", show_alert=True)
        return
    if order.get("status") != "создана":
        await callback.answer(
            "Эту заявку нельзя удалить, так как она уже находится в работе.\n"
            "Обратитесь к администратору",
            show_alert=True,
        )
        return

    text = (
        f"Вы уверены, что хотите удалить заявку № {order['number']}?\n\n"
        "⚠️ Это действие нельзя отменить."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить",
                    callback_data=f"del_yes:{order_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"del_cancel:{order_id}",
                ),
            ]
        ]
    )
    await _safe_edit_card(callback, text, kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("del_cancel:"))
async def cancel_delete_order(callback: CallbackQuery, state: FSMContext):
    """Отмена удаления заявки — просто возвращаем детали заявки."""
    try:
        order_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer()
        return
    await _render_order_card(callback, state, order_id=order_id)


@router.callback_query(F.data.startswith("del_yes:"))
async def delete_order_yes(callback: CallbackQuery, state: FSMContext):
    """Фактическое удаление заявки пользователем."""
    if not callback.from_user:
        await callback.answer("Ошибка.", show_alert=True)
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка.", show_alert=True)
        return

    # Получаем заявку, чтобы знать номер и автора.
    try:
        order = await get_order(order_id)
    except Exception as e:
        logger.exception("Get order before delete (yes) failed: %s", e)
        await callback.answer("Ошибка загрузки заявки.", show_alert=True)
        return
    if not order:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    if order.get("author_telegram_id") != callback.from_user.id:
        await callback.answer("Вы можете удалить только свои заявки.", show_alert=True)
        return
    if order.get("status") != "создана":
        await callback.answer(
            "Эту заявку нельзя удалить, так как она уже находится в работе.\n"
            "Обратитесь к администратору",
            show_alert=True,
        )
        return

    try:
        resp = await api_delete_order(order_id, requester_telegram_id=callback.from_user.id)
    except ValueError as e:
        msg = str(e)
        if "ORDER_NOT_DELETABLE" in msg:
            await callback.answer(
                "Эту заявку нельзя удалить, так как она уже находится в работе.\n"
                "Обратитесь к администратору",
                show_alert=True,
            )
        else:
            await callback.answer(f"Ошибка удаления: {msg}", show_alert=True)
        return
    except Exception as e:
        logger.exception("Delete order failed: %s", e)
        await callback.answer("Ошибка удаления заявки. Попробуйте позже.", show_alert=True)
        return

    number = resp.get("number") or order.get("number")

    # Удаляем у админов исходные уведомления с файлом МаркЗнак и кнопкой «Взять в работу»,
    # чтобы в чате осталось только текстовое уведомление об удалении.
    await _purge_admin_order_telegram_cards(callback.bot, order_id)

    # Удаляем сообщение с деталями и уведомляем пользователя.
    try:
        await callback.message.delete()
    except Exception:
        pass
    is_adm = await is_admin(callback.from_user.id)
    await callback.message.answer(
        f"Заявка № {number} успешно удалена.",
        reply_markup=main_menu_kb(is_admin=is_adm),
    )

    # Уведомляем админов.
    username = callback.from_user.username or resp.get("author_username") or ""
    user_label = f"@{username}" if username else str(callback.from_user.id)
    from datetime import datetime

    now = datetime.now().strftime("%d.%m.%Y")
    text = (
        f"Заявка № {number} была удалена пользователем.\n\n"
        f"Пользователь: {user_label}\n"
        f"Дата удаления: {now}"
    )
    for admin_id in await _all_admin_ids_for_broadcast():
        try:
            await callback.bot.send_message(chat_id=admin_id, text=text)
        except Exception as e:
            logger.warning(
                "Notify admin about deleted order failed admin_id=%s order_id=%s: %s",
                admin_id,
                order_id,
                e,
            )
            continue

    await callback.answer()


@router.callback_query(F.data.startswith("adel_confirm:"))
async def admin_confirm_delete_order(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления заявки администратором."""
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка.", show_alert=True)
        return

    try:
        order = await get_order(order_id)
    except Exception as e:
        logger.exception("Get order before admin delete failed: %s", e)
        await callback.answer("Ошибка загрузки заявки.", show_alert=True)
        return
    if not order:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    text = (
        f"Вы уверены, что хотите удалить заявку № {order['number']}?\n\n"
        "⚠️ Это действие нельзя отменить."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить",
                    callback_data=f"adel_yes:{order_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"adel_cancel:{order_id}",
                ),
            ]
        ]
    )
    await _safe_edit_card(callback, text, kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adel_cancel:"))
async def admin_cancel_delete_order(callback: CallbackQuery, state: FSMContext):
    """Отмена админского удаления — вернуть детали заявки."""
    try:
        order_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer()
        return
    await _render_order_card(callback, state, order_id=order_id)


@router.callback_query(F.data.startswith("adel_yes:"))
async def admin_delete_order_yes(callback: CallbackQuery, state: FSMContext, dispatcher: Dispatcher):
    """Фактическое удаление заявки админом."""
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка.", show_alert=True)
        return

    try:
        order = await get_order(order_id)
    except Exception as e:
        logger.exception("Get order before admin delete (yes) failed: %s", e)
        await callback.answer("Ошибка загрузки заявки.", show_alert=True)
        return
    if not order:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    try:
        resp = await api_delete_order_admin(order_id, requester_telegram_id=callback.from_user.id)
    except Exception as e:
        logger.exception("Admin delete order failed: %s", e)
        await callback.answer("Ошибка удаления заявки. Попробуйте позже.", show_alert=True)
        return

    number = resp.get("number") or order.get("number")
    author_id = resp.get("author_telegram_id") or order.get("author_telegram_id")

    # Убираем у всех админов зарегистрированные уведомления с МаркЗнак (кроме текущего сообщения).
    if callback.message:
        await _purge_admin_order_telegram_cards(
            callback.bot,
            order_id,
            skip_chat_id=callback.message.chat.id,
            skip_message_id=callback.message.message_id,
        )
    else:
        await _purge_admin_order_telegram_cards(callback.bot, order_id)

    # Удаляем сообщение с деталями в чате админа.
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(f"Заявка № {number} удалена.")

    # Уведомляем автора и возвращаем к списку «Мои заявки» (как шаг назад).
    if author_id:
        try:
            aid = int(author_id)
            author_ctx = FSMContext(
                storage=dispatcher.storage,
                key=StorageKey(
                    bot_id=callback.bot.id,
                    chat_id=aid,
                    user_id=aid,
                ),
            )
            await _show_my_orders_list_for_user(
                callback.bot,
                author_ctx,
                user_id=aid,
                chat_id=aid,
                header_notice=(
                    f"⚠️ Ваша заявка № {number} была удалена администратором."
                ),
            )
        except Exception:
            logger.exception("Notify author after admin delete failed")

    await callback.answer()


@router.callback_query(F.data.startswith("change_resp:"))
async def change_responsible_start(callback: CallbackQuery, state: FSMContext):
    """Начало смены ответственного: показать список админов."""
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка.", show_alert=True)
        return

    try:
        order = await get_order(order_id)
    except Exception as e:
        logger.exception("Get order for change_resp failed: %s", e)
        await callback.answer("Ошибка загрузки заявки.", show_alert=True)
        return
    if not order:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    current_resp_id = order.get("responsible_telegram_id")
    # Список админов и цвета берём из той же логики, что и «История заявок»,
    # чтобы кружки и подписи полностью совпадали.
    from bot.handlers.history import (
        _load_admins_tuples,
        _build_user_color_mapping,
        _admin_color_label as _history_color_label,
        active_admin_telegram_ids,
    )

    admins_tuples = await _load_admins_tuples()
    # Тот же пул, что и фильтры «История заявок» (без дублей @ и без снятых админов).
    pick_tuples = admins_tuples
    active_ids = await active_admin_telegram_ids()
    full_orders = await get_orders(admin=True, limit=100)
    user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)

    # Исключаем текущего ответственного, чтобы не предлагать его ещё раз.
    buttons: list[list[InlineKeyboardButton]] = []
    for tid, username in pick_tuples:
        if tid is None:
            continue
        if int(tid) not in active_ids:
            continue
        if current_resp_id and tid == current_resp_id:
            continue
        label = _history_color_label(tid, username, user_to_index)
        buttons.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"set_resp:{order_id}:{tid}",
                )
            ]
        )

    if not buttons:
        await callback.answer("Нет других админов для назначения.", show_alert=True)
        return

    # Кнопка «Назад» просто повторно открывает карточку заявки.
    buttons.append(
        [InlineKeyboardButton(text="« Назад", callback_data=f"ord:{order_id}")]
    )

    await callback.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer("Выберите нового ответственного.")


@router.callback_query(F.data.startswith("set_resp:"))
async def set_responsible(callback: CallbackQuery, state: FSMContext):
    """Установить нового ответственного за заявку."""
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        _, order_id_str, admin_id_str = callback.data.split(":")
        order_id = int(order_id_str)
        new_resp_id = int(admin_id_str)
    except (ValueError, IndexError):
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    # Только активный админ (role=admin в БД + в актуальном пуле UI).
    from bot.handlers.history import is_active_admin_id

    if not await is_active_admin_id(new_resp_id):
        await callback.answer(
            "Невозможно назначить заявку: администратор удалён",
            show_alert=True,
        )
        return

    # Получаем заявку до изменений, чтобы понимать текущий статус.
    try:
        order_before = await get_order(order_id)
    except Exception as e:
        logger.exception("Get order before set_resp failed: %s", e)
        await callback.answer("Ошибка загрузки заявки.", show_alert=True)
        return
    if not order_before:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    old_resp_id = order_before.get("responsible_telegram_id")
    old_resp_username = order_before.get("responsible_username")

    # Пытаемся получить username из БД, чтобы красиво показать в карточке.
    username: str | None
    try:
        user = await get_user(new_resp_id)
    except Exception as e:
        logger.exception("Get user for set_resp failed: %s", e)
        user = None
    # Для админов из ADMIN_IDS запись в БД может отсутствовать — тогда используем id без @username.
    username = user.get("username") if user else None

    # Логика статуса при смене ответственного:
    # - если заявка ещё не отправлена ("создана" / "в работе" / "готово") — переводим в "в работе",
    #   чтобы у нового админа она появилась в «Мои заявки» в нужном статусе;
    # - если уже "отправлена" — статус не трогаем.
    current_status = str(order_before.get("status") or "")
    if current_status in ("создана", "в работе", "готово"):
        new_status: str | None = "в работе"
    else:
        new_status = None

    try:
        updated = await update_order(
            order_id,
            status=new_status,
            responsible_telegram_id=new_resp_id,
            responsible_username=username,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            try:
                body = e.response.json()
                detail = body.get("detail") if isinstance(body, dict) else None
            except Exception:
                detail = None
            if detail == "RESPONSIBLE_NOT_ACTIVE_ADMIN":
                await callback.answer(
                    "Невозможно назначить заявку: администратор удалён",
                    show_alert=True,
                )
                return
        logger.exception("Update responsible failed: %s", e)
        await callback.answer("Ошибка сохранения ответственного.", show_alert=True)
        return
    except Exception as e:
        logger.exception("Update responsible failed: %s", e)
        await callback.answer("Ошибка сохранения ответственного.", show_alert=True)
        return
    if not updated:
        await callback.answer("Заявка не найдена или не обновлена.", show_alert=True)
        return

    await try_repair_responsible_telegram_self(new_resp_id)

    # Перерисовываем карточку заявки с новым ответственным и тем же цветом, что в истории.
    try:
        order = await get_order(order_id)
    except Exception as e:
        logger.exception("Reload order after set_resp failed: %s", e)
        await callback.answer("Ответственный сохранён, но не удалось обновить карточку.", show_alert=True)
        return
    if not order:
        await callback.answer("Ответственный сохранён, но заявка не найдена.", show_alert=True)
        return

    user_to_index = await _load_admin_color_index()

    await callback.message.edit_text(
        _format_order_compact_html(order, adm=True, user_to_index=user_to_index),
        reply_markup=order_detail_back_kb(
            is_admin=True,
            order_id=order_id,
            current_status=order.get("status"),
            in_trash=bool(order.get("deleted_at")),
        ),
        parse_mode="HTML",
    )

    changer_id = callback.from_user.id if callback.from_user else new_resp_id
    changer_username = (
        callback.from_user.username if (callback.from_user and callback.from_user.username) else None
    )
    changer_label = f"@{changer_username}" if changer_username else str(changer_id)

    # username для нового ответственного: берём из свежего апдейта, иначе из user, иначе id.
    new_resp_username = username or (user.get("username") if user else None)
    new_label = f"@{new_resp_username}" if new_resp_username else str(new_resp_id)

    # 1) Уведомление новому администратору о передаче заявки.
    notify_new_ok = True
    try:
        text_new = (
            f"Вам передана заявка № {order['number']}.\n"
            f"Ответственного назначил: {changer_label}"
        )
        await callback.bot.send_message(chat_id=new_resp_id, text=text_new)
    except Exception as e:
        logger.exception("Notify new responsible failed: %s", e)
        notify_new_ok = False

    # 2) Уведомление админу, с которого сняли заявку.
    if old_resp_id and int(old_resp_id) != int(new_resp_id):
        try:
            old_label = f"@{old_resp_username}" if old_resp_username else str(old_resp_id)
            text_old = (
                f"{changer_label} скорректировал ответственного заявки № {order['number']} "
                f"с {old_label} на {new_label}."
            )
            await callback.bot.send_message(chat_id=int(old_resp_id), text=text_old)
        except Exception as e:
            logger.exception("Notify old responsible failed: %s", e)

    done_msg = "Ответственный обновлён."
    if not notify_new_ok:
        done_msg += (
            " Личное сообщение новому ответственному не доставлено "
            "(в Telegram бот не может писать первым — нужен /start у бота). Заявка сохранена."
        )
    await callback.answer(done_msg, show_alert=not notify_new_ok)


@router.callback_query(F.data.startswith("take:"))
async def take_order_in_work(callback: CallbackQuery, state: FSMContext):
    """Admin: take order in work from notification (кнопка \"Взять в работу\")."""
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка.", show_alert=True)
        return

    # Сначала проверяем, не взял ли уже кто-то эту заявку в работу.
    try:
        current = await get_order(order_id)
    except Exception as e:
        logger.exception("Get order before take failed: %s", e)
        await callback.answer("Ошибка загрузки заявки.", show_alert=True)
        return
    if not current:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    responsible_id = current.get("responsible_telegram_id")
    responsible_username = current.get("responsible_username")
    # Если уже есть ответственный и статус "в работе" — считаем, что заявка
    # уже взята, чтобы не плодить дублирующие уведомления пользователю.
    if current.get("status") == "в работе":
        resp_label = (
            f"@{responsible_username}"
            if responsible_username
            else (str(responsible_id) if responsible_id else "администратор")
        )
        note = f"\n\nУже в работе. Ответственный: {resp_label}"
        try:
            if callback.message and callback.message.caption is not None:
                await callback.message.edit_caption(
                    caption=callback.message.caption + note,
                    reply_markup=None,
                )
            elif callback.message and callback.message.text is not None:
                await callback.message.edit_text(
                    callback.message.text + note,
                    reply_markup=None,
                )
        except Exception:
            pass
        await callback.answer(
            f"Эта заявка уже в работе у {resp_label}.", show_alert=True
        )
        return
    if responsible_id and responsible_id != callback.from_user.id:
        # Уже в работе у другого админа — сообщаем и убираем кнопки у этого сообщения.
        resp_label = (
            f"@{responsible_username}"
            if responsible_username
            else str(responsible_id)
        )
        note = f"\n\nУже в работе. Ответственный: {resp_label}"
        try:
            if callback.message.caption is not None:
                new_caption = callback.message.caption + note
                await callback.message.edit_caption(
                    caption=new_caption, reply_markup=None
                )
            elif callback.message.text is not None:
                new_text = callback.message.text + note
                await callback.message.edit_text(new_text, reply_markup=None)
        except Exception:
            pass
        await callback.answer(
            f"Эту заявку уже взял в работу {resp_label}.", show_alert=True
        )
        return

    await callback.answer()

    try:
        updated = await update_order(
            order_id,
            status="в работе",
            responsible_telegram_id=callback.from_user.id,
            responsible_username=callback.from_user.username,
        )
    except Exception as e:
        logger.exception("Take in work failed: %s", e)
        await callback.message.reply("Ошибка: не удалось взять заявку в работу.")
        return
    if not updated:
        await callback.message.reply("Заявка не найдена или не обновлена.")
        return

    # Удаляем карточки у остальных админов (БД + реестр); текущее сообщение оставляем для подписи.
    taken_by = (
        f"Взял в работу: @{callback.from_user.username}"
        if callback.from_user.username
        else "Заявка взята в работу."
    )
    if callback.message:
        await _purge_admin_order_telegram_cards(
            callback.bot,
            order_id,
            skip_chat_id=callback.message.chat.id,
            skip_message_id=callback.message.message_id,
        )
    else:
        await _purge_admin_order_telegram_cards(callback.bot, order_id)

    # Обновляем подпись/текст именно для того сообщения, откуда пришёл callback
    try:
        if callback.message.caption is not None:
            new_caption = callback.message.caption + f"\n\n{taken_by}"
            await callback.message.edit_caption(caption=new_caption, reply_markup=None)
        elif callback.message.text is not None:
            new_text = callback.message.text + f"\n\n{taken_by}"
            await callback.message.edit_text(new_text, reply_markup=None)
    except Exception:
        # Не критично, если не получилось отредактировать
        pass

    # Карточку у этого админа не удалили — снова пишем в БД, иначе при удалении заявки
    # нечем удалить сообщение с МаркЗнак из чата.
    if callback.message:
        try:
            await register_order_telegram_posting(
                order_id,
                callback.message.chat.id,
                callback.message.message_id,
            )
        except Exception as e:
            logger.warning(
                "register_order_telegram_posting after take order=%s: %s", order_id, e
            )

    # Получаем заказ для уведомления автора (отдельное уведомление только здесь)
    try:
        order = await get_order(order_id)
    except Exception as e:
        logger.exception("Get order for notify failed: %s", e)
        return
    if not order:
        return

    try:
        author_id = order.get("author_telegram_id")
        if author_id:
            await callback.bot.send_message(
                chat_id=author_id,
                text=f"Ваша заявка № {order['number']} в работе.",
            )
    except Exception as e:
        logger.exception("Notify author about in-work failed: %s", e)


@router.callback_query(F.data == "orders_back")
async def orders_list_back(callback: CallbackQuery, state: FSMContext):
    """Назад из карточки заявки — возврат к тому же списку (тот же фильтр и страница)."""
    # Сразу отвечаем, чтобы убрать "часики" в интерфейсе
    try:
        await callback.answer()
    except Exception:
        pass

    data = await state.get_data()
    orders = data.get("orders", [])
    page = data.get("page", 0)
    mode = data.get("mode", "my")
    status_filter = data.get("status_filter")
    admin_labels = data.get("admin_labels")
    is_admin_in_my_out = bool(data.get("is_admin_in_my", False))

    # Если state пустой (часто бывает после отправки файлов/новых сообщений),
    # восстанавливаем список заново, чтобы «Назад» всегда работал.
    if not orders and callback.from_user:
        try:
            # Если возвращаемся из истории заявок, это видно по клавиатуре/заголовку,
            # даже если проверка is_admin() временно дала False.
            try:
                kb = (
                    callback.message.reply_markup.inline_keyboard
                    if (callback.message and callback.message.reply_markup)
                    else []
                )
                has_admin_filters = any(
                    (btn.callback_data or "").startswith("admflt:")
                    for row in kb
                    for btn in row
                )
            except Exception:
                has_admin_filters = False
            msg_text = (callback.message.text or callback.message.caption or "") if callback.message else ""
            from_history_context = has_admin_filters or ("История заявок" in msg_text)

            if from_history_context:
                mode = "history"
                status_filter = status_filter or "all"
                status = None if status_filter == "all" else status_filter
                orders = await get_orders(admin=True, status=status, limit=100)
                page = 0
            elif await is_admin(callback.from_user.id):
                mode = "my"
                raw = await load_admin_my_orders_source(callback.from_user.id)
                orders = filter_admin_my_orders_rows(raw, None)
                status_filter = None
                page = 0
                is_admin_in_my_out = True
            else:
                mode = "my"
                status_filter = None
                orders = await get_orders(author_telegram_id=callback.from_user.id, limit=100)
                page = 0
        except Exception:
            pass

    if mode == "history":
        from bot.handlers.history import (
            _load_admins_tuples,
            _build_user_color_mapping,
            _build_admin_filter_buttons,
            _history_order_row_caption,
            fetch_history_orders_list,
            _history_trash_inline_kw,
            _pretty_status_key,
        )

        sf = status_filter or "all"
        filters_collapsed = bool(data.get("filters_collapsed", False))
        admin_filter_id = data.get("admin_filter")
        admins_tuples = await _load_admins_tuples()
        active_ids = {int(t) for t, _ in admins_tuples if t is not None}
        if admin_filter_id is not None and int(admin_filter_id) not in active_ids:
            admin_filter_id = None
            await state.update_data(admin_filter=None)

        prev_orders = orders
        try:
            orders = await fetch_history_orders_list(sf, admin_filter_id=admin_filter_id)
        except Exception:
            logger.exception("orders_list_back: fetch_history_orders_list failed")
            orders = prev_orders

        full_orders = await get_orders(admin=True, include_deleted=True, limit=100)
        user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)
        admin_buttons = _build_admin_filter_buttons(
            admins_tuples,
            user_to_index,
            selected_admin_id=admin_filter_id,
            collapse_others_when_selected=True,
        )
        tw = _history_trash_inline_kw(sf, data)
        aid = await active_admin_ids_frozen()
        unorms = active_admin_username_norms_frozen(admins_tuples)

        items = [
            (
                o["id"],
                o["number"],
                _history_order_row_caption(
                    o,
                    user_to_index,
                    in_trash_list=(sf == "trash"),
                    active_admin_ids=aid,
                    active_admin_username_norms=unorms,
                ),
            )
            for o in orders
        ]
        has_next = len(orders) > (page + 1) * ORDERS_PER_PAGE
        status_label = "все" if (not status_filter or status_filter == "all") else str(status_filter)
        title = f"==================================================\nИстория заявок ({status_label}):"
        kw = {
            "show_filters": not filters_collapsed,
            "current_filter": sf,
            "filter_mode": "history",
            "admin_labels": admin_buttons,
            "back_callback": "hist_back",
            "filters_back_callback": ("fltmenu" if filters_collapsed else None),
            "filters_back_text": _pretty_status_key(sf),
            **tw,
        }
    else:
        if is_admin_in_my_out:
            items = [(o["id"], o["number"], o["status"]) for o in orders]
            filter_mode = "my_admin"
        else:
            items = [
                (o["id"], o["number"], _user_visible_status(o["status"]))
                for o in orders
            ]
            filter_mode = "my_user"
        sf_curr = status_filter
        if is_admin_in_my_out and not sf_curr:
            sf_curr = "all"
        has_next = len(orders) > (page + 1) * ORDERS_PER_PAGE
        title = "Ваши заявки:"
        kw = {
            "show_filters": True,
            "current_filter": sf_curr,
            "filter_mode": filter_mode,
        }

    text = f"{title}\n\nВыберите заявку для просмотра:"
    markup = orders_list_inline(items, page=page, has_next=has_next, prefix="ord", **kw)

    # Максимально надёжно: всегда отправляем новый список и удаляем карточку.
    # Так работает и для text, и для document/caption, и не зависит от ограничений edit_*.
    try:
        await callback.message.answer(text, reply_markup=markup)
    except Exception:
        # Если даже отправка не удалась — покажем алерт, иначе "тихо" кажется что кнопка не работает
        try:
            await callback.answer("Не удалось показать список. Попробуйте /start.", show_alert=True)
        except Exception:
            pass
        return

    # Важно: после возврата из карточки в список восстанавливаем state,
    # иначе следующий «Назад» может отработать как «Мои заявки».
    try:
        if mode == "history":
            await state.update_data(
                orders=orders,
                page=page,
                mode="history",
                status_filter=sf,
                admin_labels=admin_buttons,
                admin_filter=admin_filter_id,
                filters_collapsed=filters_collapsed,
            )
        else:
            await state.update_data(
                orders=orders,
                page=page,
                mode="my",
                status_filter=sf_curr,
                is_admin_in_my=is_admin_in_my_out,
            )
    except Exception:
        pass
    try:
        await callback.message.delete()
    except Exception:
        pass
