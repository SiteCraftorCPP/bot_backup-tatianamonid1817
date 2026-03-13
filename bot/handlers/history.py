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


def _user_visible_status(status: str) -> str:
    """Маппинг внутренних статусов в пользовательские (для обычного юзера)."""
    if status == "готово":
        return "в работе"
    if status == "отправлена":
        return "готова"
    return status


COLORS = ["🟢", "🟠", "🔵", "🟣", "🟡", "🟤"]


def _admin_color_label(telegram_id: int | None, username: str | None) -> str:
    """Вернуть цветной кружок + username/id для администратора."""
    if not telegram_id and not username:
        return ""
    # Ключ для цвета: в первую очередь username, чтобы везде было одинаково,
    # даже если где-то нет telegram_id; иначе используем сам id.
    if username:
        key = username.lower()
    else:
        key = str(telegram_id or "")
    color = COLORS[hash(key) % len(COLORS)]
    main = f"@{username}" if username else key
    return f"{color} {main}"


async def _load_admin_filters() -> list[str]:
    """Полный список текущих админов проекта (ADMIN_IDS + role=admin в БД).

    Для кнопки показываем username (user в телеге), если он есть, иначе id.
    Возвращаем список меток для отображения.
    """
    settings = get_settings()

    # все id из ENV
    cfg_ids: set[int] = set(settings.admin_ids_list)

    # ключ: str(telegram_id), значение: username или ""
    admins: dict[str, str] = {}

    # 1) админы из БД (users с role='admin')
    try:
        admins_db = await list_admins()
    except Exception:  # noqa: BLE001
        admins_db = []

    for a in admins_db or []:
        try:
            tid = int(a.get("telegram_id"))
        except (TypeError, ValueError):
            continue
        key = str(tid)
        username = (a.get("username") or "").strip()
        admins[key] = username
        cfg_ids.discard(tid)

    # 2) админы только из ENV, которых ещё нет в словаре
    for tid in cfg_ids:
        key = str(tid)
        username = ""
        try:
            u = await get_user(tid)
        except Exception:  # noqa: BLE001
            u = None
        if u:
            username = (u.get("username") or "").strip()
        admins.setdefault(key, username)

    # 3) превращаем в подписи для кнопок с авто-раскраской
    filters: list[str] = []
    for key, username in admins.items():
        tid = int(key) if key.isdigit() else None
        label = _admin_color_label(tid, username)
        filters.append(label)
    return filters


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

    def _status_with_responsible(o: dict) -> str:
        resp = o.get("responsible_username") or ""
        if not resp:
            return f"{o['status']}"
        label = _admin_color_label(o.get("responsible_telegram_id"), resp)
        return f"{o['status']} — {label}"

    # Ряд с юзернеймами всех админов проекта под фильтрами статуса.
    admin_labels = await _load_admin_filters()

    items = [(o["id"], o["number"], _status_with_responsible(o)) for o in orders]
    has_next = len(orders) > ORDERS_PER_PAGE
    await message.answer(
        "==================================================\nИстория заявок:\n\nВыберите заявку для просмотра:",
        reply_markup=orders_list_inline(
            items,
            page=0,
            has_next=has_next,
            prefix="ord",
            show_filters=True,
            current_filter=None,
            filter_mode="history",
            admin_labels=admin_labels,
        ),
    )
    await state.update_data(
        orders=orders,
        page=0,
        mode="history",
        status_filter=None,
        admin_filter=None,
    )


@router.callback_query(F.data.startswith("flt:"))
async def orders_filter(callback: CallbackQuery, state: FSMContext):
    """Apply status filter to orders list (history mode)."""
    if not callback.from_user:
        await callback.answer("Ошибка.", show_alert=True)
        return

    data = await state.get_data()
    mode = data.get("mode", "my")
    user_id = callback.from_user.id
    status_key = callback.data.split(":", 1)[1]
    status = None if status_key == "all" else status_key
    try:
        # История заявок (админ)
        if mode == "history":
            if not await _is_admin(user_id):
                await callback.answer("Доступ запрещён.", show_alert=True)
                return
            orders = await get_orders(admin=True, status=status, limit=100)
            admin_labels = await _load_admin_filters()

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
                        admin_labels=admin_labels,
                    ),
                )
            else:
                def _status_with_responsible(o: dict) -> str:
                    resp = o.get("responsible_username") or ""
                    if not resp:
                        return f"{o['status']}"
                    label = _admin_color_label(o.get("responsible_telegram_id"), resp)
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
                        admin_labels=admin_labels,
                    ),
                )
            await state.update_data(
                orders=orders,
                page=0,
                mode="history",
                status_filter=status_key,
                admin_filter=None,
            )
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


@router.callback_query(F.data == "admsep")
async def admin_filter_separator(callback: CallbackQuery, state: FSMContext):
    """Пустая кнопка-отступ под списком админов."""
    await callback.answer()


@router.callback_query(F.data == "admnoop")
async def admin_filter_noop(callback: CallbackQuery, state: FSMContext):
    """Неактивная кнопка с именем админа (визуальный список сотрудников)."""
    await callback.answer()
