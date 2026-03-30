"""My orders handler."""
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

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
)
from bot.keyboards import main_menu_kb, orders_list_inline, order_detail_back_kb
from bot.notification_registry import notifications_registry
from bot.handlers.main_menu import is_admin

router = Router()
logger = logging.getLogger(__name__)

ORDERS_PER_PAGE = 16


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


async def _render_order_card(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    order_id: int,
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

    data = await state.get_data()
    mode = data.get("mode", "my")
    _ = mode  # mode используется ниже для файла, но здесь не нужен

    date_str = order["created_at"][:19].replace("T", " ")
    adm = await is_admin(callback.from_user.id) if callback.from_user else False
    shown_status = order["status"]
    if not adm:
        shown_status = _user_visible_status(shown_status)

    lines = [
        f"<b>Заявка №{order['number']}</b>",
        f"Статус: {shown_status}",
    ]

    if adm:
        link = _get_order_file_link(order.get("id") or order_id)
        if link:
            lines.append(f'Дата: <a href="{link}">{date_str}</a>')
        else:
            lines.append(f"Дата: {date_str}")
    else:
        lines.append(f"Дата: {date_str}")

    if adm:
        resp = order.get("responsible_username")
        resp_id = order.get("responsible_telegram_id")
        if resp or resp_id:
            from bot.handlers.history import (
                _load_admins_tuples,
                _build_user_color_mapping,
                _admin_color_label as _history_color_label,
            )
            admins_tuples = await _load_admins_tuples()
            full_orders = await get_orders(admin=True, limit=100)
            user_to_index, _admin_labels = _build_user_color_mapping(full_orders, admins_tuples)
            lines.append(f"Ответственный: {_history_color_label(resp_id, resp, user_to_index)}")

    lines.extend(["", "Позиции:"])
    for i, item in enumerate(order.get("items", []), 1):
        name = item.get("name") or item.get("article") or "?"
        lines.append(f"  {i}. {name} — размер {item.get('size', '?')} x{item.get('quantity', 0)}")

    if order.get("yandex_link"):
        lines.append("")
        lines.append(f"Ссылка на файлы: {order['yandex_link']}")

    show_status_btns = adm
    can_user_delete = (not adm) and (order.get("status") == "создана")
    text = "\n".join(lines)
    markup = order_detail_back_kb(
        is_admin=show_status_btns,
        order_id=order_id,
        current_status=order.get("status"),
        can_user_delete=can_user_delete,
    )
    await _safe_edit_card(callback, text, markup, parse_mode="HTML")
    await state.update_data(selected_order_id=order_id)
    await callback.answer()


def _build_message_link(chat_id: int, message_id: int) -> str | None:
    """
    Построить ссылку на сообщение в чате Telegram.

    Работает для супергрупп/каналов (chat_id < 0). Для приватных чатов
    публичной ссылки нет — возвращаем None.
    """
    if chat_id > 0:
        return None
    chat_id_abs = abs(chat_id)
    # Для супергрупп/каналов внешний id = internal_id - 1000000000000.
    if chat_id_abs > 10**12:
        internal = chat_id_abs - 10**12
    else:
        internal = chat_id_abs
    return f"https://t.me/c/{internal}/{message_id}"


def _get_order_file_link(order_id: int) -> str | None:
    """Получить ссылку на сообщение с файлом заявки, если оно было отправлено админам."""
    entries = notifications_registry.get_for_order(order_id)
    if not entries:
        return None
    entry = entries[0]
    return _build_message_link(entry.chat_id, entry.message_id)


COLORS = ["🟢", "🟠", "🔵", "🟣", "🟡", "🟤"]


def _admin_color_label(telegram_id: int | None, username: str | None) -> str:
    """Вернуть цветной кружок + username/id для администратора (как в истории заявок)."""
    if not telegram_id and not username:
        return ""
    # Ключ для цвета: в первую очередь username (чтобы совпадать с историей),
    # иначе telegram_id.
    if username:
        key = username.lower()
    else:
        key = str(telegram_id or "")
    color = COLORS[hash(key) % len(COLORS)]
    main = f"@{username}" if username else key
    return f"{color} {main}"


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
    await state.clear()
    if not message.from_user:
        return
    uid = message.from_user.id
    is_adm = await is_admin(uid)
    try:
        if is_adm:
            # Для админа в «Мои заявки» показываем только свои заявки
            # в статусах «в работе» и «готово».
            in_work = await get_orders(
                responsible_telegram_id=uid, status="в работе", limit=100
            )
            ready = await get_orders(
                responsible_telegram_id=uid, status="готово", limit=100
            )
            # Убираем дубли по id
            seen: set[int] = set()
            orders: list[dict] = []
            for o in in_work + ready:
                oid = int(o.get("id"))
                if oid in seen:
                    continue
                seen.add(oid)
                orders.append(o)
        else:
            # Пользователь видит только свои заявки (author_telegram_id)
            orders = await get_orders(author_telegram_id=uid, limit=100)
    except Exception as e:
        logger.exception("Get orders failed: %s", e)
        await message.answer("Ошибка загрузки заявок. Попробуйте позже.")
        return
    if not orders:
        if is_adm:
            await message.answer("У вас пока нет заявок в работе.")
        else:
            await message.answer("У вас пока нет заявок.")
        return
    filter_mode = "my_admin" if is_adm else "my_user"
    if is_adm:
        items = [(o["id"], o["number"], o["status"]) for o in orders]
    else:
        # Пользователю показываем «создана / в работе / готова» по маппингу.
        items = [
            (o["id"], o["number"], _user_visible_status(o["status"])) for o in orders
        ]
    has_next = len(orders) > ORDERS_PER_PAGE
    title = "Ваши заявки (в работе и выполненные):\n\nВыберите заявку:" if is_adm else "Ваши заявки:\n\nВыберите заявку для просмотра:"
    await message.answer(
        title,
        reply_markup=orders_list_inline(
            items,
            page=0,
            has_next=has_next,
            prefix="ord",
            show_filters=True,
            current_filter=None,
            filter_mode=filter_mode,
        ),
    )
    await state.update_data(orders=orders, page=0, mode="my", status_filter=None, is_admin_in_my=is_adm)


@router.callback_query(F.data.startswith("ord:"))
async def order_select(callback: CallbackQuery, state: FSMContext):
    """Show order details."""
    order_id = int(callback.data.split(":")[1])
    await _render_order_card(callback, state, order_id=order_id)

    # Режим и права для выбора файла
    data = await state.get_data()
    mode = data.get("mode", "my")
    adm = await is_admin(callback.from_user.id) if callback.from_user else False

    # Нужен номер заявки для имени файла/подписи
    try:
        order = await get_order(order_id)
    except Exception:
        order = None
    if not order:
        return

    # Для администратора:
    # - в истории заявок шлём админский файл МаркЗнак;
    # - в «моих заявках» шлём пользовательский файл заявки.
    try:
        import tempfile
        import os

        if adm and mode == "history":
            excel_bytes = await get_markznak_order_excel(order_id)
            filename = f"Заявка_{order['number']}_markznak.xlsx"
            caption = f"Файл МаркЗнак по заявке №{order['number']}."
        else:
            excel_bytes = await get_order_excel(order_id)
            filename = f"Заявка_{order['number']}.xlsx"
            caption = f"Файл заявки №{order['number']}."

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            f.write(excel_bytes)
            tmp_path = f.name
        try:
            doc = FSInputFile(tmp_path, filename=filename)
            await callback.message.answer_document(document=doc, caption=caption)
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        logger.exception("Send order excel from detail failed: %s", e)


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
            )

            status_key = data.get("status_filter") or "all"
            selected_admin_id = data.get("admin_filter")
            filters_collapsed = bool(data.get("filters_collapsed", False))

            # 1) Сначала откатываем фильтр по админу (к "Все админы").
            # При этом возвращаем UI в исходное состояние с видимыми категориями (show_filters=True),
            # чтобы не прятать категории под одной кнопкой.
            if selected_admin_id is not None:
                selected_admin_id = None
                status = None if status_key == "all" else status_key
                orders = await get_orders(admin=True, status=status, limit=100)
                full_orders = await get_orders(admin=True, limit=100)
                admins_tuples = await _load_admins_tuples()
                user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)
                admin_buttons = _build_admin_filter_buttons(
                    admins_tuples, user_to_index, selected_admin_id=None
                )
                items = [(o["id"], o["number"], o["status"]) for o in orders]
                has_next = len(orders) > ORDERS_PER_PAGE
                title = f"==================================================\nИстория заявок ({'все' if status_key=='all' else status_key}):"
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
                orders = await get_orders(admin=True, limit=100)
                full_orders = orders
                admins_tuples = await _load_admins_tuples()
                user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)
                admin_buttons = _build_admin_filter_buttons(
                    admins_tuples, user_to_index, selected_admin_id=None
                )
                items = [(o["id"], o["number"], o["status"]) for o in orders]
                has_next = len(orders) > ORDERS_PER_PAGE
                title = "==================================================\nИстория заявок (все):"
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
                )
                return

            # 3) Потом возвращаемся к выбору категорий
            if filters_collapsed:
                orders = await get_orders(admin=True, limit=100)
                admins_tuples = await _load_admins_tuples()
                user_to_index, _ = _build_user_color_mapping(orders, admins_tuples)
                admin_buttons = _build_admin_filter_buttons(
                    admins_tuples, user_to_index, selected_admin_id=None
                )
                items = [(o["id"], o["number"], o["status"]) for o in orders]
                has_next = len(orders) > ORDERS_PER_PAGE
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
                )
                return

        # ===== My orders mode =====
        status_filter = data.get("status_filter")
        is_admin_in_my = data.get("is_admin_in_my", False)

        # если есть фильтр статуса — сбрасываем его
        if status_filter:
            if is_admin_in_my:
                in_work = await get_orders(responsible_telegram_id=user_id, status="в работе", limit=100)
                ready = await get_orders(responsible_telegram_id=user_id, status="готово", limit=100)
                seen: set[int] = set()
                orders = []
                for o in in_work + ready:
                    oid = int(o.get("id"))
                    if oid in seen:
                        continue
                    seen.add(oid)
                    orders.append(o)
                filter_mode = "my_admin"
                items = [(o["id"], o["number"], o["status"]) for o in orders]
            else:
                orders = await get_orders(author_telegram_id=user_id, limit=100)
                filter_mode = "my_user"
                items = [(o["id"], o["number"], _user_visible_status(o["status"])) for o in orders]
            has_next = len(orders) > ORDERS_PER_PAGE
            await _safe_edit_card(
                callback,
                "Ваши заявки:\n\nВыберите заявку для просмотра:",
                orders_list_inline(
                    items,
                    page=0,
                    has_next=has_next,
                    prefix="ord",
                    show_filters=True,
                    current_filter=None,
                    filter_mode=filter_mode,
                ),
            )
            await state.update_data(orders=orders, page=0, mode="my", status_filter=None, is_admin_in_my=is_admin_in_my)
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

    if mode == "history":
        filters_collapsed = bool(data.get("filters_collapsed", False))
        def _status_with_responsible(o: dict) -> str:
            resp = o.get("responsible_username") or ""
            if not resp:
                resp = ""
            else:
                resp = f" — {resp}"
            return f"{o['status']}{resp}"

        items = [(o["id"], o["number"], _status_with_responsible(o)) for o in orders]
        filter_mode = "history"
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
    kw = (
        {
            "show_filters": (mode != "history") or (not filters_collapsed),
            "current_filter": status_filter,
            "filter_mode": filter_mode,
        }
        if mode in ("history", "my")
        else {}
    )
    if mode == "history" and filters_collapsed:
        kw["filters_back_callback"] = "fltmenu"
        kw["back_callback"] = "hist_back"
    await callback.message.edit_reply_markup(
        reply_markup=orders_list_inline(items, page=page, has_next=has_next, prefix="ord", **kw),
    )
    await state.update_data(page=page)
    await callback.answer()


def _format_order_message(order: dict, user_to_index: dict | None = None) -> str:
    """Format order details for display. Если передан user_to_index — ответственный с цветным кружком (как в истории)."""
    date_str = order["created_at"][:19].replace("T", " ")
    lines = [
        f"<b>Заявка №{order['number']}</b>",
        f"Статус: {order['status']}",
    ]
    link = _get_order_file_link(order.get("id"))
    if link:
        lines.append(f'Дата: <a href="{link}">{date_str}</a>')
    else:
        lines.append(f"Дата: {date_str}")
    resp = order.get("responsible_username")
    resp_id = order.get("responsible_telegram_id")
    if resp or resp_id:
        if user_to_index is not None:
            from bot.handlers.history import _admin_color_label as _history_color_label
            label = _history_color_label(resp_id, resp, user_to_index)
        else:
            label = f"@{resp}" if resp else str(resp_id or "")
        lines.append(f"Ответственный: {label}")
    lines.extend(
        [
            "",
            "Позиции:",
        ]
    )
    for i, item in enumerate(order.get("items", []), 1):
        name = item.get("name") or item.get("article") or "?"
        lines.append(f"  {i}. {name} — размер {item.get('size', '?')} x{item.get('quantity', 0)}")
    if order.get("yandex_link"):
        lines.append("")
        lines.append(f"Ссылка на файлы: {order['yandex_link']}")
    return "\n".join(lines)


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

    from bot.handlers.history import _load_admins_tuples, _build_user_color_mapping
    admins_tuples = await _load_admins_tuples()
    full_orders = await get_orders(admin=True, limit=100)
    user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)

    # Уведомление автору при смене статуса
    try:
        author_id = order.get("author_telegram_id")
        # Для статуса "в работе" уведомление отправляет хендлер "take".
        # Здесь для пользователя шлём только при финальном статусе "отправлена",
        # при этом текст говорит, что заявка готова.
        if author_id and status == "отправлена":
            text_parts = [f"Ваша заявка №{order['number']} готова."]
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
        _format_order_message(order, user_to_index=user_to_index),
        reply_markup=order_detail_back_kb(
            is_admin=True, order_id=order_id, current_status=order.get("status")
        ),
        parse_mode="HTML",
    )


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
            "Эту заявку нельзя удалить, так как она уже находится в работе.",
            show_alert=True,
        )
        return

    text = (
        f"Вы уверены, что хотите удалить заявку №{order['number']}?\n\n"
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
            "Эту заявку нельзя удалить, так как она уже находится в работе.",
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
                "Обратитесь к администратору.",
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

    # Удаляем сообщение с деталями и уведомляем пользователя.
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(f"Заявка №{number} успешно удалена.")

    # Уведомляем админов.
    settings = get_settings()
    username = callback.from_user.username or resp.get("author_username") or ""
    user_label = f"@{username}" if username else str(callback.from_user.id)
    from datetime import datetime

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    text = (
        f"Заявка №{number} была удалена пользователем.\n\n"
        f"Пользователь: {user_label}\n"
        f"Дата удаления: {now}"
    )
    for admin_id in settings.admin_ids_list:
        try:
            await callback.bot.send_message(chat_id=admin_id, text=text)
        except Exception:
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
        f"Вы уверены, что хотите удалить заявку №{order['number']}?\n\n"
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
async def admin_delete_order_yes(callback: CallbackQuery, state: FSMContext):
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

    # Удаляем сообщение с деталями в чате админа.
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(f"Заявка №{number} удалена.")

    # Уведомляем пользователя, если он есть.
    if author_id:
        try:
            await callback.bot.send_message(
                chat_id=author_id,
                text=f"⚠️ Ваша заявка №{number} была удалена администратором.",
            )
        except Exception:
            pass

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
    )

    admins_tuples = await _load_admins_tuples()
    full_orders = await get_orders(admin=True, limit=100)
    user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)

    # Исключаем текущего ответственного, чтобы не предлагать его ещё раз.
    buttons: list[list[InlineKeyboardButton]] = []
    for tid, username in admins_tuples:
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
    except Exception as e:
        logger.exception("Update responsible failed: %s", e)
        await callback.answer("Ошибка сохранения ответственного.", show_alert=True)
        return
    if not updated:
        await callback.answer("Заявка не найдена или не обновлена.", show_alert=True)
        return

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

    from bot.handlers.history import _load_admins_tuples, _build_user_color_mapping
    admins_tuples = await _load_admins_tuples()
    full_orders = await get_orders(admin=True, limit=100)
    user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)

    await callback.message.edit_text(
        _format_order_message(order, user_to_index=user_to_index),
        reply_markup=order_detail_back_kb(
            is_admin=True, order_id=order_id, current_status=order.get("status")
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
    try:
        text_new = (
            f"Вам передана заявка №{order['number']}.\n"
            f"Ответственного назначил: {changer_label}"
        )
        await callback.bot.send_message(chat_id=new_resp_id, text=text_new)
    except Exception as e:
        logger.exception("Notify new responsible failed: %s", e)
        # Частая причина: пользователь ещё не писал боту → Telegram запрещает писать первым.
        try:
            await callback.bot.send_message(
                chat_id=changer_id,
                text=(
                    f"Не смог уведомить {new_label} о передаче заявки №{order['number']}.\n"
                    f"Пусть он/она нажмёт /start у бота, и повторите назначение."
                ),
            )
        except Exception:
            pass

    # 2) Уведомление админу, с которого сняли заявку.
    if old_resp_id and int(old_resp_id) != int(new_resp_id):
        try:
            old_label = f"@{old_resp_username}" if old_resp_username else str(old_resp_id)
            text_old = (
                f"{changer_label} скорректировал ответственного заявки №{order['number']} "
                f"с {old_label} на {new_label}."
            )
            await callback.bot.send_message(chat_id=int(old_resp_id), text=text_old)
        except Exception as e:
            logger.exception("Notify old responsible failed: %s", e)

    await callback.answer("Ответственный обновлён.")


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

    # Удаляем сообщения с заявкой у всех остальных админов (реестр снимаем сразу, чтобы не копить мусор)
    taken_by = (
        f"Взял в работу: @{callback.from_user.username}"
        if callback.from_user.username
        else "Заявка взята в работу."
    )
    notifications = notifications_registry.pop_order(order_id)
    for entry in notifications:
        try:
            # Сообщение, из которого пришёл callback, не трогаем здесь
            if (
                callback.message
                and callback.message.message_id == entry.message_id
                and callback.message.chat.id == entry.chat_id
            ):
                continue
            # Остальным админам удаляем уведомление целиком
            await callback.bot.delete_message(
                chat_id=entry.chat_id,
                message_id=entry.message_id,
            )
        except Exception:
            # Не блокируем сценарий, если не получилось удалить какое-то уведомление
            continue

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
                text=f"Ваша заявка №{order['number']} в работе.",
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

            if from_history_context or await is_admin(callback.from_user.id):
                mode = "history"
                status_filter = status_filter or "all"
                status = None if status_filter == "all" else status_filter
                orders = await get_orders(admin=True, status=status, limit=100)
                page = 0
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
            _admin_color_label as _history_color_label,
        )
        admins_tuples = await _load_admins_tuples()
        full_orders = await get_orders(admin=True, limit=100)
        user_to_index, admin_labels = _build_user_color_mapping(full_orders, admins_tuples)

        def _status_with_responsible(o: dict) -> str:
            resp = o.get("responsible_username") or ""
            if not resp:
                return f"{o['status']}"
            label = _history_color_label(o.get("responsible_telegram_id"), resp, user_to_index)
            return f"{o['status']} — {label}"

        items = [(o["id"], o["number"], _status_with_responsible(o)) for o in orders]
        has_next = len(orders) > (page + 1) * ORDERS_PER_PAGE
        status_label = "все" if (not status_filter or status_filter == "all") else str(status_filter)
        title = f"==================================================\nИстория заявок ({status_label}):"
        kw = {
            "show_filters": True,
            "current_filter": status_filter,
            "filter_mode": "history",
            "admin_labels": admin_labels,
            "back_callback": "hist_back",
        }
    else:
        items = [(o["id"], o["number"], o["status"]) for o in orders]
        has_next = len(orders) > (page + 1) * ORDERS_PER_PAGE
        title = "Ваши заявки:"
        kw = {}

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
                status_filter=(status_filter or "all"),
                admin_labels=admin_labels,
                admin_filter=data.get("admin_filter"),
                filters_collapsed=bool(data.get("filters_collapsed", False)),
            )
        else:
            await state.update_data(
                orders=orders,
                page=page,
                mode="my",
                status_filter=status_filter,
                is_admin_in_my=data.get("is_admin_in_my", False),
            )
    except Exception:
        pass
    try:
        await callback.message.delete()
    except Exception:
        pass
