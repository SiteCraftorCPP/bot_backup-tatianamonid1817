"""History of orders - admin only."""
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from config import get_settings
from bot.admin_my_orders_list import filter_admin_my_orders_rows, load_admin_my_orders_source
from bot.api_client import get_orders, get_order, list_admins, get_user, purge_trash_orders
from bot.keyboards import main_menu_kb, orders_list_inline
from bot.handlers.main_menu import is_admin as _is_admin

router = Router()
logger = logging.getLogger(__name__)

ORDERS_PER_PAGE = 8


@router.callback_query(F.data == "hist_back")
async def history_back(callback: CallbackQuery, state: FSMContext):
    """Назад в истории заявок (свой callback без коллизий с 'Мои заявки')."""
    if not callback.from_user:
        await callback.answer("Ошибка.", show_alert=True)
        return
    user_id = callback.from_user.id
    if not await _is_admin(user_id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    from bot.handlers.my_orders import _safe_edit_card  # local import to avoid cycles

    data = await state.get_data()
    status_key = data.get("status_filter") or "all"
    selected_admin_id = data.get("admin_filter")
    filters_collapsed = bool(data.get("filters_collapsed", False))

    try:
        admins_tuples = await _load_admins_tuples()
        full_orders = await fetch_history_full_orders_for_colors()
        user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)

        # 1) Сначала откатываем фильтр по админу (к "Все админы")
        if selected_admin_id is not None:
            selected_admin_id = None
            orders = await fetch_history_orders_list(status_key, admin_filter_id=None)
            admin_buttons = _build_admin_filter_buttons(
                admins_tuples,
                user_to_index,
                selected_admin_id=None,
                collapse_others_when_selected=True,
            )
            items = [
                (
                    o["id"],
                    o["number"],
                    _history_order_row_caption(
                        o, user_to_index, in_trash_list=(status_key == "trash")
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
                    show_filters=not filters_collapsed,
                    current_filter=status_key,
                    filter_mode="history",
                    admin_labels=admin_buttons,
                    filters_back_callback=("fltmenu" if filters_collapsed else None),
                    filters_back_text=_pretty_status_key(status_key),
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
                filters_collapsed=filters_collapsed,
            )
            await callback.answer()
            return

        # 2) Потом откатываем статус (к "Все")
        if status_key != "all":
            status_key = "all"
            orders = await fetch_history_orders_list("all", admin_filter_id=None)
            admin_buttons = _build_admin_filter_buttons(
                admins_tuples,
                user_to_index,
                selected_admin_id=None,
                collapse_others_when_selected=True,
            )
            items = [
                (o["id"], o["number"], _history_order_row_caption(o, user_to_index))
                for o in orders
            ]
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
                    **_history_trash_inline_kw("all", {**data, "trash_selected_ids": []}),
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
            await callback.answer()
            return

    except Exception as e:  # noqa: BLE001
        logger.exception("hist_back failed: %s", e)

    # дефолт: главное меню (и чистим state истории)
    await state.clear()
    is_adm = await _is_admin(user_id)
    await _safe_edit_card(callback, "Главное меню:", None)
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb(is_admin=is_adm))
    await callback.answer()


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
    if status_key == "trash":
        return "Корзина"
    mapping = {
        "создана": "Создана",
        "в работе": "В работе",
        "готово": "Готово",
        "отправлена": "Отправлена",
    }
    return mapping.get(status_key, str(status_key))


async def fetch_history_orders_list(
    status_key: str,
    *,
    admin_filter_id: int | None,
) -> list[dict]:
    """Загрузка списка для истории: «все» с удалёнными, корзина, или фильтр по статусу."""
    if status_key == "trash":
        kw: dict = {"admin": True, "deleted_only": True, "limit": 100}
        if admin_filter_id is not None:
            kw["responsible_telegram_id"] = admin_filter_id
        return await get_orders(**kw)
    if status_key == "all":
        kw = {"admin": True, "include_deleted": True, "limit": 100}
        if admin_filter_id is not None:
            kw["responsible_telegram_id"] = admin_filter_id
        return await get_orders(**kw)
    kw = {"admin": True, "status": status_key, "limit": 100}
    if admin_filter_id is not None:
        kw["responsible_telegram_id"] = admin_filter_id
    return await get_orders(**kw)


async def fetch_history_full_orders_for_colors() -> list[dict]:
    """Полный пул заявок (включая корзину) для стабильных цветов админов."""
    return await get_orders(admin=True, include_deleted=True, limit=100)


def _is_order_soft_deleted(o: dict) -> bool:
    """Заявка в корзине (мягкое удаление): в ответе API поле deleted_at непустое."""
    v = o.get("deleted_at")
    if v is None or v is False:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    return True


def _history_order_row_caption(
    o: dict,
    user_to_index: dict,
    *,
    in_trash_list: bool = False,
) -> str:
    """Подпись строки заявки в списке истории (статус + ответственный).

    Для корзины и любой заявки с deleted_at — только статус на момент удаления,
    без ответственного (полная карточка по «Подробнее» не меняется).
    """
    resp = o.get("responsible_username") or ""
    status = o["status"]
    if in_trash_list or _is_order_soft_deleted(o):
        return status
    if not resp:
        return status
    label = _admin_color_label(o.get("responsible_telegram_id"), resp, user_to_index)
    return f"{status} — {label}"


def _history_trash_inline_kw(status_key: str, state_data: dict) -> dict:
    """Параметры клавиатуры для режима корзины (чекбоксы и массовое удаление)."""
    if status_key != "trash":
        return {
            "trash_mode": False,
            "trash_toolbar": False,
            "trash_selected_ids": None,
        }
    raw = state_data.get("trash_selected_ids") or []
    try:
        sel = frozenset(int(x) for x in raw)
    except (TypeError, ValueError):
        sel = frozenset()
    return {
        "trash_mode": True,
        "trash_toolbar": True,
        "trash_selected_ids": sel,
    }


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


def _norm_username_key(username: str | None) -> str:
    """Ключ для дедупа ников: NBSP→пробел, trim, lower."""
    if not username:
        return ""
    return username.replace("\u00A0", " ").strip().lower()


async def active_admin_telegram_ids() -> set[int]:
    """Telegram_id действующих админов — только после перепроверки get_user (как PATCH /orders)."""
    return {int(t) for t, _ in await _load_admins_tuples_raw()}


async def is_active_admin_id(telegram_id: int) -> bool:
    """Можно ли назначить ответственным (совпадает с проверкой на backend PATCH)."""
    return int(telegram_id) in await active_admin_telegram_ids()


async def _load_admins_tuples_raw() -> list[tuple[int, str]]:
    """Собрать админов без дедупа по username.

    Кандидаты: union(ADMIN_IDS, list_admins). Для каждого telegram_id — ровно один get_user;
    в списке только role=admin (как PATCH /orders при назначении ответственного).

    Так не остаются «призраки» из .env после удаления записи из БД и не тянется устаревший
    username из ответа list_admins без сверки с актуальной ролью.
    """
    settings = get_settings()
    candidate_ids: set[int] = set()
    for x in settings.admin_ids_list:
        try:
            candidate_ids.add(int(x))
        except (TypeError, ValueError):
            continue
    try:
        admins_db = await list_admins()
    except Exception:  # noqa: BLE001
        admins_db = []
    for a in admins_db or []:
        try:
            candidate_ids.add(int(a.get("telegram_id")))
        except (TypeError, ValueError):
            continue

    admins_by_tid: dict[int, str] = {}
    for tid in sorted(candidate_ids):
        try:
            u = await get_user(tid)
        except Exception:  # noqa: BLE001
            continue
        if not u or str(u.get("role") or "").strip() != "admin":
            continue
        admins_by_tid[tid] = str(u.get("username") or "").strip()

    return list(admins_by_tid.items())


async def _load_admins_tuples() -> list[tuple[int | None, str]]:
    """Список актуальных админов как (telegram_id, username) без дублей username.

    Источник истины:
    - role=admin в БД;
    - ADMIN_IDS из env добавляем только если пользователь существует и тоже admin.
    """
    pairs = await _load_admins_tuples_raw()
    # Уникальность по telegram_id (на случай сбоев API).
    by_tid: dict[int, str] = {}
    for tid, uname in pairs:
        try:
            it = int(tid)
        except (TypeError, ValueError):
            continue
        by_tid[it] = uname

    # Убираем дубли ников (после удаления/возврата и смены @).
    sorted_items = sorted(by_tid.items(), key=lambda x: (_norm_username_key(x[1]), str(x[0])))
    result: list[tuple[int, str]] = []
    seen_usernames: set[str] = set()
    for tid, uname in sorted_items:
        key = _norm_username_key(uname)
        if key and key in seen_usernames:
            continue
        if key:
            seen_usernames.add(key)
        result.append((tid, uname))
    return result


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
        orders = await fetch_history_orders_list("all", admin_filter_id=None)
    except Exception as e:
        logger.exception("Get orders failed: %s", e)
        await message.answer("Ошибка загрузки заявок. Попробуйте позже.")
        return
    if not orders:
        await message.answer("Заявок пока нет.")
        return

    admins_tuples = await _load_admins_tuples()
    full_orders = await fetch_history_full_orders_for_colors()
    user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)
    admin_buttons = _build_admin_filter_buttons(admins_tuples, user_to_index, selected_admin_id=None)

    items = [
        (o["id"], o["number"], _history_order_row_caption(o, user_to_index, in_trash_list=False))
        for o in orders
    ]
    has_next = len(orders) > ORDERS_PER_PAGE
    tw = _history_trash_inline_kw("all", {"trash_selected_ids": []})
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
            **tw,
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
        trash_selected_ids=[],
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
    selected_admin_id = data.get("admin_filter")
    try:
        orders = await fetch_history_orders_list(
            status_key, admin_filter_id=selected_admin_id
        )
        admins_tuples = await _load_admins_tuples()
        full_orders = await fetch_history_full_orders_for_colors()
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

    tw = _history_trash_inline_kw(status_key, data)
    title_cat = _pretty_status_key(status_key)
    if not orders:
        await callback.message.edit_text(
            f"==================================================\nИстория заявок ({title_cat}):\n\nЗаявок не найдено.",
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
                **tw,
            ),
        )
    else:
        items = [
            (
                o["id"],
                o["number"],
                _history_order_row_caption(
                    o, user_to_index, in_trash_list=(status_key == "trash")
                ),
            )
            for o in orders
        ]
        has_next = len(orders) > ORDERS_PER_PAGE
        title = f"==================================================\nИстория заявок ({title_cat}):"
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
                **tw,
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
        if not await is_active_admin_id(selected_admin_id):
            await callback.answer(
                "Этот администратор удалён. Показаны все заявки.",
                show_alert=True,
            )
            selected_admin_id = None

    try:
        orders = await fetch_history_orders_list(
            status_key, admin_filter_id=selected_admin_id
        )
        admins_tuples = await _load_admins_tuples()
        full_orders = await fetch_history_full_orders_for_colors()
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

    tw = _history_trash_inline_kw(status_key, data)
    title_cat = _pretty_status_key(status_key)
    if not orders:
        await callback.message.edit_text(
            f"==================================================\nИстория заявок ({title_cat}):\n\nЗаявок не найдено.",
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
                **tw,
            ),
        )
    else:
        items = [
            (
                o["id"],
                o["number"],
                _history_order_row_caption(
                    o, user_to_index, in_trash_list=(status_key == "trash")
                ),
            )
            for o in orders
        ]
        has_next = len(orders) > ORDERS_PER_PAGE
        title = f"==================================================\nИстория заявок ({title_cat}):"
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
                **tw,
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
    prev_status = data.get("status_filter") or "all"
    if status_key == "trash":
        trash_selected_ids = (
            []
            if prev_status != "trash"
            else (data.get("trash_selected_ids") or [])
        )
    else:
        trash_selected_ids = []

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
            orders = await fetch_history_orders_list(status_key, admin_filter_id=None)
            admins_tuples = await _load_admins_tuples()
            full_orders = await fetch_history_full_orders_for_colors()
            user_to_index, admin_labels = _build_user_color_mapping(full_orders, admins_tuples)
            admin_buttons = _build_admin_filter_buttons(admins_tuples, user_to_index, selected_admin_id=None)
            tw = _history_trash_inline_kw(
                status_key, {**data, "trash_selected_ids": trash_selected_ids}
            )
            title_cat = _pretty_status_key(status_key)

            if not orders:
                await callback.message.edit_text(
                    f"==================================================\nИстория заявок ({title_cat}):\n\nЗаявок не найдено.",
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
                        **tw,
                    ),
                )
            else:
                items = [
                    (
                        o["id"],
                        o["number"],
                        _history_order_row_caption(
                            o, user_to_index, in_trash_list=(status_key == "trash")
                        ),
                    )
                    for o in orders
                ]
                has_next = len(orders) > ORDERS_PER_PAGE
                title = f"==================================================\nИстория заявок ({title_cat}):"
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
                filters_collapsed=True,
                trash_selected_ids=trash_selected_ids,
            )
            return
        # Фильтр для «Моих заявок» (пользователь или админ)
        else:
            is_admin_in_my = data.get("is_admin_in_my", False)
            # «Мои заявки» для админа: полный пул назначений (фильтр вкладками на клиенте).
            if is_admin_in_my:
                raw = await load_admin_my_orders_source(user_id)
                orders = filter_admin_my_orders_rows(raw, status)
                filter_mode = "my_admin"
                title_base = "Ваши заявки (все назначенные на вас)"
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


async def _history_redraw_current_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Перерисовать текущий список истории (тот же фильтр и страница)."""
    data = await state.get_data()
    status_key = data.get("status_filter") or "all"
    page = int(data.get("page") or 0)
    selected_admin_id = data.get("admin_filter")
    filters_collapsed = bool(data.get("filters_collapsed", False))

    orders = await fetch_history_orders_list(status_key, admin_filter_id=selected_admin_id)
    await state.update_data(orders=orders)

    admins_tuples = await _load_admins_tuples()
    full_orders = await fetch_history_full_orders_for_colors()
    user_to_index, _ = _build_user_color_mapping(full_orders, admins_tuples)
    admin_buttons = _build_admin_filter_buttons(
        admins_tuples,
        user_to_index,
        selected_admin_id=selected_admin_id,
        collapse_others_when_selected=True,
    )
    tw = _history_trash_inline_kw(status_key, await state.get_data())
    title_cat = _pretty_status_key(status_key)
    start = page * ORDERS_PER_PAGE
    has_next = len(orders) > start + ORDERS_PER_PAGE

    if not orders:
        await callback.message.edit_text(
            f"==================================================\nИстория заявок ({title_cat}):\n\nЗаявок не найдено.",
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
                **tw,
            ),
        )
        return

    items = [
        (
            o["id"],
            o["number"],
            _history_order_row_caption(
                o, user_to_index, in_trash_list=(status_key == "trash")
            ),
        )
        for o in orders
    ]
    title = f"==================================================\nИстория заявок ({title_cat}):"
    await callback.message.edit_text(
        f"{title}\n\nВыберите заявку для просмотра:",
        reply_markup=orders_list_inline(
            items,
            page=page,
            has_next=has_next,
            prefix="ord",
            show_filters=not filters_collapsed,
            current_filter=status_key,
            filter_mode="history",
            admin_labels=admin_buttons,
            filters_back_callback=("fltmenu" if filters_collapsed else None),
            filters_back_text=_pretty_status_key(status_key),
            back_callback="hist_back",
            **tw,
        ),
    )


@router.callback_query(F.data.startswith("trshsel:"))
async def history_trash_toggle_select(callback: CallbackQuery, state: FSMContext):
    """Переключить выбор заявки в корзине для массового удаления."""
    if not callback.from_user or not await _is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    data = await state.get_data()
    if data.get("status_filter") != "trash":
        await callback.answer()
        return
    try:
        oid = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    sel = list(data.get("trash_selected_ids") or [])
    if oid in sel:
        sel.remove(oid)
    else:
        sel.append(oid)
    await state.update_data(trash_selected_ids=sel)
    await _history_redraw_current_list(callback, state)
    await callback.answer()


@router.callback_query(F.data == "trshdel:sel")
async def history_trash_delete_selected_ask(callback: CallbackQuery, state: FSMContext):
    if not callback.from_user or not await _is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    data = await state.get_data()
    sel = list(data.get("trash_selected_ids") or [])
    if not sel:
        await callback.answer("Не выбрано ни одной заявки.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Удалить навсегда выбранные заявки ({len(sel)} шт.) из базы?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Да, удалить",
                        callback_data="trshdel_sel_yes",
                    ),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="trshdel_cancel"),
                ],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "trshdel:all")
async def history_trash_delete_all_ask(callback: CallbackQuery, state: FSMContext):
    if not callback.from_user or not await _is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    data = await state.get_data()
    n = len(data.get("orders") or [])
    await callback.message.edit_text(
        f"Удалить навсегда все заявки в корзине ({n} шт.) из базы?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Да, удалить все",
                        callback_data="trshdel_all_yes",
                    ),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="trshdel_cancel"),
                ],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "trshdel_cancel")
async def history_trash_purge_cancel(callback: CallbackQuery, state: FSMContext):
    if not callback.from_user or not await _is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await _history_redraw_current_list(callback, state)
    await callback.answer("Отменено.")


@router.callback_query(F.data == "trshdel_sel_yes")
async def history_trash_delete_selected_do(callback: CallbackQuery, state: FSMContext):
    if not callback.from_user or not await _is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    data = await state.get_data()
    sel = list(data.get("trash_selected_ids") or [])
    if not sel:
        await callback.answer("Список пуст.", show_alert=True)
        await _history_redraw_current_list(callback, state)
        return
    try:
        await purge_trash_orders(callback.from_user.id, ids=sel)
    except Exception as e:
        logger.exception("purge_trash_orders failed: %s", e)
        await callback.answer("Ошибка удаления.", show_alert=True)
        return
    await state.update_data(trash_selected_ids=[], page=0)
    await _history_redraw_current_list(callback, state)
    await callback.answer(f"Удалено: {len(sel)}.")


@router.callback_query(F.data == "trshdel_all_yes")
async def history_trash_delete_all_do(callback: CallbackQuery, state: FSMContext):
    if not callback.from_user or not await _is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        res = await purge_trash_orders(callback.from_user.id, ids=None)
    except Exception as e:
        logger.exception("purge_trash all failed: %s", e)
        await callback.answer("Ошибка удаления.", show_alert=True)
        return
    n = int(res.get("purged") or 0)
    await state.update_data(trash_selected_ids=[], page=0)
    await _history_redraw_current_list(callback, state)
    await callback.answer(f"Удалено из базы: {n}.")
