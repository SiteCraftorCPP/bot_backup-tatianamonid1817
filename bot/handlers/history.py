"""History of orders - admin only."""
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from config import get_settings
from bot.api_client import get_orders, get_order, list_admins, get_user
from bot.keyboards import main_menu_kb, orders_list_inline, order_detail_back_kb
from bot.handlers.main_menu import is_admin as _is_admin

router = Router()
logger = logging.getLogger(__name__)

ORDERS_PER_PAGE = 8


@router.callback_query(F.data == "hist_back")
async def history_back(callback: CallbackQuery, state: FSMContext):
    """Назад в истории заявок (свой callback без коллизий с 'Мои заявки')."""
    # Дальше используем уже существующую логику отката шагов из my_orders.py
    # (там есть корректный "степ-бэк" по фильтрам истории).
    try:
        await state.update_data(mode="history")
    except Exception:
        pass
    from bot.handlers.my_orders import ord_list_back_to_main  # local import to избежать циклов
    await ord_list_back_to_main(callback, state)


def _user_visible_status(status: str) -> str:
    """Маппинг внутренних статусов в пользовательские (для обычного юзера)."""
    if status == "готово":
        return "в работе"
    if status == "отправлена":
        return "готова"
    return status


def _pretty_status_key(status_key: str | None) -> str:
    """Красивое название раздела для UI."""
    if not status_key or status_key == "all":
        return "Все"
    mapping = {
        "создана": "Создана",
        "в работе": "В работе",
        "готово": "Готово",
        "отправлена": "Отправлена",
    }
    return mapping.get(status_key, str(status_key))


COLORS = ["🟢", "🟠", "🔵", "🟣", "🟡", "🟤", "🔴", "⚫", "⚪"]


def _user_key(telegram_id: int | None, username: str | None) -> str:
    """Стабильный ключ пользователя для маппинга цветов.

    Цвет жёстко привязан к username; если username нет, используем telegram_id.
    Один и тот же username → один и тот же цвет во всех местах.
    """
    uname = (username or "").strip().lower()
    if uname:
        return uname
    return str(telegram_id or "")


def _admin_color_label(
    telegram_id: int | None,
    username: str | None,
    user_to_index: dict | None = None,
) -> str:
    """Цветной кружок + username/id. Если передан user_to_index — цвет уникален для пользователя."""
    if not telegram_id and not username:
        return ""
    main = f"@{username}" if username else str(telegram_id or "")
    if user_to_index is not None:
        key = _user_key(telegram_id, username)
        idx = user_to_index.get(key, 0)
        color = COLORS[idx % len(COLORS)]
    else:
        key = (username or "").lower() or str(telegram_id or "")
        color = COLORS[hash(key) % len(COLORS)]
    return f"{color} {main}"


async def _load_admins_tuples() -> list[tuple[int | None, str]]:
    """Список админов как (telegram_id, username) в стабильном порядке."""
    settings = get_settings()
    cfg_ids: set[int] = set(settings.admin_ids_list)
    admins: dict[str, str] = {}
    try:
        admins_db = await list_admins()
    except Exception:  # noqa: BLE001
        admins_db = []
    for a in admins_db or []:
        try:
            tid = int(a.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        admins[str(tid)] = (a.get("username") or "").strip()
        cfg_ids.discard(tid)
    for tid in cfg_ids:
        username = ""
        try:
            u = await get_user(tid)
            if u:
                username = (u.get("username") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        admins.setdefault(str(tid), username)
    return [(int(k) if k.isdigit() else None, admins[k]) for k in sorted(admins.keys(), key=lambda x: (admins[x].lower(), x))]


def _build_user_color_mapping(
    orders: list[dict],
    admins_tuples: list[tuple[int | None, str]],
) -> tuple[dict, list[str]]:
    """Строит стабильный маппинг user_key -> индекс цвета.
    Сначала все админы в фиксированном порядке, затем ответственные из заявок (не админы).
    Один и тот же пользователь всегда получает один и тот же цвет при любом фильтре/пагинации.
    """
    responsibles: set[str] = set()
    for o in orders:
        tid = o.get("responsible_telegram_id")
        uname = o.get("responsible_username")
        if tid or uname:
            responsibles.add(_user_key(tid, uname))
    admins_set: set[str] = {_user_key(tid, uname) for tid, uname in admins_tuples}
    all_users: list[str] = []
    seen: set[str] = set()
    for tid, uname in admins_tuples:
        k = _user_key(tid, uname)
        if k not in seen:
            seen.add(k)
            all_users.append(k)
    for k in sorted(responsibles - admins_set):
        if k not in seen:
            seen.add(k)
            all_users.append(k)
    user_to_index = {u: i for i, u in enumerate(all_users)}
    admin_labels = [_admin_color_label(tid, uname, user_to_index) for tid, uname in admins_tuples]
    return user_to_index, admin_labels


def _build_admin_filter_buttons(
    admins_tuples: list[tuple[int | None, str]],
    user_to_index: dict,
    selected_admin_id: int | None,
    *,
    collapse_others_when_selected: bool = False,
) -> list[tuple[str, str]]:
    """Кнопки фильтрации по админам (ответственным).

    Если collapse_others_when_selected=True и выбран конкретный админ, показываем:
    - «Все админы» (сброс фильтра);
    - выбранного админа (с ✓).
    """
    btns: list[tuple[str, str]] = []
    all_suffix = " ✓" if selected_admin_id is None else ""
    btns.append((f"Все админы{all_suffix}", "admflt:all"))
    if collapse_others_when_selected and selected_admin_id is not None:
        # Только выбранный админ
        for tid, uname in admins_tuples:
            if tid is None or tid != selected_admin_id:
                continue
            label = _admin_color_label(tid, uname, user_to_index)
            btns.append((label + " ✓", f"admflt:{tid}"))
        return btns
    for tid, uname in admins_tuples:
        if tid is None:
            continue
        label = _admin_color_label(tid, uname, user_to_index)
        suffix = " ✓" if selected_admin_id == tid else ""
        btns.append((label + suffix, f"admflt:{tid}"))
    return btns


@router.message(F.text == "📜 История заявок")
async def history_orders(message: Message, state: FSMContext):
    """Admin: show all orders."""
    if not message.from_user or not await _is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    await state.clear()
    try:
        orders = await get_orders(admin=True, limit=100)
    except Exception as e:
        logger.exception("Get orders failed: %s", e)
        await message.answer("Ошибка загрузки заявок. Попробуйте позже.")
        return
    if not orders:
        await message.answer("Заявок пока нет.")
        return

    admins_tuples = await _load_admins_tuples()
    user_to_index, _ = _build_user_color_mapping(orders, admins_tuples)
    admin_buttons = _build_admin_filter_buttons(admins_tuples, user_to_index, selected_admin_id=None)

    def _status_with_responsible(o: dict) -> str:
        resp = o.get("responsible_username") or ""
        if not resp:
            return f"{o['status']}"
        label = _admin_color_label(o.get("responsible_telegram_id"), resp, user_to_index)
        return f"{o['status']} — {label}"

    items = [(o["id"], o["number"], _status_with_responsible(o)) for o in orders]
    has_next = len(orders) > ORDERS_PER_PAGE
    await message.answer(
        "==================================================\nИстория заявок (все):\n\nВыберите заявку для просмотра:",
        reply_markup=orders_list_inline(
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
        filters_collapsed=False,
        admin_filter=None,
        admin_labels=admin_buttons,
    )


@router.callback_query(F.data == "fltmenu")
async def history_filters_menu(callback: CallbackQuery, state: FSMContext):
    """Вернуться к выбору категорий (статусов) в истории заявок."""
    if not callback.from_user:
        await callback.answer("Ошибка.", show_alert=True)
        return
    if not await _is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    data = await state.get_data()
    filters_collapsed = bool(data.get("filters_collapsed", False))
    status_key = data.get("status_filter") or "all"
    status = None if status_key == "all" else status_key
    selected_admin_id = data.get("admin_filter")
    try:
        orders = await get_orders(admin=True, status=status, limit=100)
        admins_tuples = await _load_admins_tuples()
        full_orders = await get_orders(admin=True, limit=100)
        user_to_index, admin_labels = _build_user_color_mapping(full_orders, admins_tuples)
        admin_buttons = _build_admin_filter_buttons(
            admins_tuples,
            user_to_index,
            selected_admin_id=selected_admin_id,
            collapse_others_when_selected=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Get orders failed: %s", e)
        await callback.answer("Ошибка загрузки.", show_alert=True)
        return

    if not orders:
        await callback.message.edit_text(
            f"==================================================\nИстория заявок ({status or 'все'}):\n\nЗаявок не найдено.",
            reply_markup=orders_list_inline(
                [],
                page=0,
                has_next=False,
                prefix="ord",
                show_filters=True,
                current_filter=status_key,
                filter_mode="history",
                admin_labels=admin_buttons,
                back_callback="hist_back",
            ),
        )
    else:
        def _status_with_responsible(o: dict) -> str:
            resp = o.get("responsible_username") or ""
            if not resp:
                return f"{o['status']}"
            label = _admin_color_label(o.get("responsible_telegram_id"), resp, user_to_index)
            return f"{o['status']} — {label}"

        items = [(o["id"], o["number"], _status_with_responsible(o)) for o in orders]
        has_next = len(orders) > ORDERS_PER_PAGE
        title = f"==================================================\nИстория заявок ({status or 'все'}):"
        await callback.message.edit_text(
            f"{title}\n\nВыберите заявку для просмотра:",
            reply_markup=orders_list_inline(
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
        filters_collapsed=False,
        admin_filter=selected_admin_id,
        admin_labels=admin_buttons,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admflt:"))
async def history_admin_filter(callback: CallbackQuery, state: FSMContext):
    """Фильтр истории заявок по ответственному админу."""
    if not callback.from_user:
        await callback.answer("Ошибка.", show_alert=True)
        return
    if not await _is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    data = await state.get_data()
    filters_collapsed = bool(data.get("filters_collapsed", False))
    status_key = data.get("status_filter") or "all"
    status = None if status_key == "all" else status_key

    raw = callback.data.split(":", 1)[1]
    selected_admin_id: int | None
    if raw == "all":
        selected_admin_id = None
    else:
        try:
            selected_admin_id = int(raw)
        except ValueError:
            await callback.answer("Ошибка.", show_alert=True)
            return

    try:
        if selected_admin_id is None:
            orders = await get_orders(admin=True, status=status, limit=100)
        else:
            orders = await get_orders(
                admin=True,
                status=status,
                responsible_telegram_id=selected_admin_id,
                limit=100,
            )
        admins_tuples = await _load_admins_tuples()
        full_orders = await get_orders(admin=True, limit=100)
        user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)
        admin_buttons = _build_admin_filter_buttons(
            admins_tuples,
            user_to_index,
            selected_admin_id=selected_admin_id,
            collapse_others_when_selected=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Admin filter failed: %s", e)
        await callback.answer("Ошибка загрузки.", show_alert=True)
        return

    if not orders:
        await callback.message.edit_text(
            f"==================================================\nИстория заявок ({status or 'все'}):\n\nЗаявок не найдено.",
            reply_markup=orders_list_inline(
                [],
                page=0,
                has_next=False,
                prefix="ord",
                show_filters=not filters_collapsed,
                current_filter=status_key,
                filter_mode="history",
                admin_labels=admin_buttons,
                filters_back_callback=("fltmenu" if filters_collapsed else None),
                filters_back_text=_pretty_status_key(status_key),
                back_callback="hist_back",
            ),
        )
    else:
        def _status_with_responsible(o: dict) -> str:
            resp = o.get("responsible_username") or ""
            if not resp:
                return f"{o['status']}"
            label = _admin_color_label(o.get("responsible_telegram_id"), resp, user_to_index)
            return f"{o['status']} — {label}"

        items = [(o["id"], o["number"], _status_with_responsible(o)) for o in orders]
        has_next = len(orders) > ORDERS_PER_PAGE
        title = f"==================================================\nИстория заявок ({status or 'все'}):"
        await callback.message.edit_text(
            f"{title}\n\nВыберите заявку для просмотра:",
            reply_markup=orders_list_inline(
                items,
                page=0,
                has_next=has_next,
                prefix="ord",
                show_filters=not filters_collapsed,
                current_filter=status_key,
                filter_mode="history",
                admin_labels=admin_buttons,
                filters_back_callback=("fltmenu" if filters_collapsed else None),
                filters_back_text=_pretty_status_key(status_key),
                back_callback="hist_back",
            ),
        )

    await state.update_data(
        orders=orders,
        page=0,
        mode="history",
        status_filter=status_key,
        admin_filter=selected_admin_id,
        admin_labels=admin_buttons,
        filters_collapsed=filters_collapsed,
    )
    await callback.answer()


async def _orders_filter_impl(callback: CallbackQuery, state: FSMContext, *, status_key: str) -> None:
    """Apply status filter to orders list (history mode or my orders)."""
    if not callback.from_user:
        await callback.answer("Ошибка.", show_alert=True)
        return

    data = await state.get_data()
    mode = data.get("mode", "my")
    user_id = callback.from_user.id
    status = None if status_key == "all" else status_key

    # Если FSM state слетел, но клик был в "Истории заявок" (там есть admflt:*),
    # то это history-flow, иначе этот хендлер уедет в ветку "Мои заявки" и перезапишет mode.
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
        if has_admin_filters:
            mode = "history"
    except Exception:
        pass
    try:
        # История заявок (админ)
        if mode == "history":
            if not await _is_admin(user_id):
                await callback.answer("Доступ запрещён.", show_alert=True)
                return
            orders = await get_orders(admin=True, status=status, limit=100)
            admins_tuples = await _load_admins_tuples()
            full_orders = await get_orders(admin=True, limit=100)
            user_to_index, admin_labels = _build_user_color_mapping(full_orders, admins_tuples)
            admin_buttons = _build_admin_filter_buttons(admins_tuples, user_to_index, selected_admin_id=None)

            if not orders:
                await callback.message.edit_text(
                    f"==================================================\nИстория заявок ({status or 'все'}):\n\nЗаявок не найдено.",
                    reply_markup=orders_list_inline(
                        [],
                        page=0,
                        has_next=False,
                        prefix="ord",
                        show_filters=False,
                        current_filter=status_key,
                        filter_mode="history",
                        admin_labels=admin_buttons,
                        filters_back_callback="fltmenu",
                        filters_back_text=_pretty_status_key(status_key),
                        back_callback="hist_back",
                    ),
                )
            else:
                def _status_with_responsible(o: dict) -> str:
                    resp = o.get("responsible_username") or ""
                    if not resp:
                        return f"{o['status']}"
                    label = _admin_color_label(o.get("responsible_telegram_id"), resp, user_to_index)
                    return f"{o['status']} — {label}"

                items = [(o["id"], o["number"], _status_with_responsible(o)) for o in orders]
                has_next = len(orders) > ORDERS_PER_PAGE
                title = f"==================================================\nИстория заявок ({status or 'все'}):"
                await callback.message.edit_text(
                    f"{title}\n\nВыберите заявку для просмотра:",
                    reply_markup=orders_list_inline(
                        items,
                        page=0,
                        has_next=has_next,
                        prefix="ord",
                        show_filters=False,
                        current_filter=status_key,
                        filter_mode="history",
                        admin_labels=admin_buttons,
                        filters_back_callback="fltmenu",
                        filters_back_text=_pretty_status_key(status_key),
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
                filters_collapsed=True,
            )
            return
        # Фильтр для «Моих заявок» (пользователь или админ)
        else:
            is_admin_in_my = data.get("is_admin_in_my", False)
            # «Мои заявки» для админа: только свои заявки в статусах «в работе» и «готово».
            if is_admin_in_my:
                if status is None:
                    in_work = await get_orders(
                        responsible_telegram_id=user_id, status="в работе", limit=100
                    )
                    ready = await get_orders(
                        responsible_telegram_id=user_id, status="готово", limit=100
                    )
                    seen_ids: set[int] = set()
                    orders = []
                    for o in in_work + ready:
                        oid = int(o.get("id"))
                        if oid in seen_ids:
                            continue
                        seen_ids.add(oid)
                        orders.append(o)
                else:
                    # Для фильтров «в работе» и «готово» используем прямой статус.
                    orders = await get_orders(
                        responsible_telegram_id=user_id, status=status, limit=100
                    )
                filter_mode = "my_admin"
                title_base = "Ваши заявки (в работе и выполненные)"
            else:
                # «Мои заявки» для пользователя: авторские заявки.
                if status is None:
                    orders = await get_orders(
                        author_telegram_id=user_id, limit=100
                    )
                elif status == "в работе":
                    # Кнопка «в работе» для пользователя объединяет внутренние статусы
                    # «в работе» и «готово».
                    in_work = await get_orders(
                        author_telegram_id=user_id, status="в работе", limit=100
                    )
                    ready = await get_orders(
                        author_telegram_id=user_id, status="готово", limit=100
                    )
                    seen_ids: set[int] = set()
                    orders = []
                    for o in in_work + ready:
                        oid = int(o.get("id"))
                        if oid in seen_ids:
                            continue
                        seen_ids.add(oid)
                        orders.append(o)
                else:
                    # «создана» и «готова» (внутренний статус «отправлена»).
                    orders = await get_orders(
                        author_telegram_id=user_id, status=status, limit=100
                    )
                filter_mode = "my_user"
                title_base = "Ваши заявки"
            if not orders:
                status_label = status or "все"
                await callback.message.edit_text(
                    f"{title_base} ({status_label}):\n\nЗаявок не найдено.",
                    reply_markup=orders_list_inline(
                        [],
                        page=0,
                        has_next=False,
                        prefix="ord",
                        show_filters=True,
                        current_filter=status_key,
                        filter_mode=filter_mode,
                    ),
                )
            else:
                if is_admin_in_my:
                    items = [(o["id"], o["number"], o["status"]) for o in orders]
                else:
                    # Пользователь видит статусы по маппингу (создана / в работе / готова).
                    items = [
                        (o["id"], o["number"], _user_visible_status(o["status"]))
                        for o in orders
                    ]
                has_next = len(orders) > ORDERS_PER_PAGE
                title = (
                    f"{title_base} ({status or 'все'}):\n\nВыберите заявку:"
                    if is_admin_in_my
                    else "Ваши заявки:\n\nВыберите заявку для просмотра:"
                )
                await callback.message.edit_text(
                    title,
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
                is_admin_in_my=is_admin_in_my,
            )
    except Exception as e:
        logger.exception("Get orders failed: %s", e)
        await callback.answer("Ошибка загрузки.", show_alert=True)
        return

    await callback.answer()


@router.callback_query(F.data.startswith("flt:"))
async def orders_filter(callback: CallbackQuery, state: FSMContext):
    """Фильтр статуса (общий префикс для 'Моих заявок')."""
    status_key = callback.data.split(":", 1)[1]
    await _orders_filter_impl(callback, state, status_key=status_key)


@router.callback_query(F.data.startswith("hflt:"))
async def history_orders_filter(callback: CallbackQuery, state: FSMContext):
    """Фильтр статуса (только для истории заявок, отдельный префикс без коллизий)."""
    status_key = callback.data.split(":", 1)[1]
    try:
        await state.update_data(mode="history")
    except Exception:
        pass
    await _orders_filter_impl(callback, state, status_key=status_key)


@router.callback_query(F.data == "admsep")
async def admin_filter_separator(callback: CallbackQuery, state: FSMContext):
    """Пустая кнопка-отступ под списком админов."""
    await callback.answer()


@router.callback_query(F.data == "admnoop")
async def admin_filter_noop(callback: CallbackQuery, state: FSMContext):
    """Неактивная кнопка с именем админа (визуальный список сотрудников)."""
    await callback.answer()
