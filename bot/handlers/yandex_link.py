"""Add Yandex.Disk link to order."""
import re
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from bot.api_client import get_orders, get_order, update_order
from bot.keyboards import main_menu_kb, orders_list_inline
from config import get_settings

router = Router()
logger = logging.getLogger(__name__)

ORDERS_PER_PAGE = 8
YANDEX_DISK_PATTERN = re.compile(
    r"https?://(?:disk\.yandex\.(?:ru|com)|yadi\.sk)/[^\s]+",
    re.IGNORECASE,
)


def is_admin(telegram_id: int) -> bool:
    return telegram_id in get_settings().admin_ids_list


@router.message(F.text == "🔗 Добавить ссылку на файлы")
async def add_yandex_link_start(message: Message, state: FSMContext):
    """Start add Yandex link flow - show orders."""
    await state.clear()
    try:
        # Берём все заявки для админа и фильтруем по статусам:
        # сюда должны попадать заявки "в работе" и "готово".
        orders = await get_orders(admin=True, limit=100)
        orders = [o for o in orders if o.get("status") in ("в работе", "готово")]
    except Exception as e:
        logger.exception("Get orders failed: %s", e)
        await message.answer("Ошибка загрузки заявок. Попробуйте позже.")
        return
    if not orders:
        # Никаких дополнительных заявок не подгружаем — явно нет подходящих статусов.
        orders = []
    if not orders:
        await message.answer("Нет заявок со статусом «в работе» или «готово» для добавления ссылки.")
        return
    items = [(o["id"], o["number"], o["status"]) for o in orders]
    has_next = len(orders) > ORDERS_PER_PAGE
    await message.answer(
        "Выберите заявку для добавления ссылки на Яндекс.Диск:",
        reply_markup=orders_list_inline(items, page=0, has_next=has_next, prefix="ylink"),
    )
    await state.update_data(orders=orders, page=0)
    await state.set_state("yandex_link:select_order")


@router.callback_query(F.data == "ylink_back")
async def ylink_back_to_main(callback: CallbackQuery, state: FSMContext):
    """Back from ylink flow to main menu."""
    await state.clear()
    uid = callback.from_user.id if callback.from_user else 0
    await callback.message.edit_text("Главное меню:")
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb(is_admin=is_admin(uid)))
    await callback.answer()


@router.callback_query(F.data.startswith("ylinkpg:"))
async def ylink_page(callback: CallbackQuery, state: FSMContext):
    """Paginate ylink orders list."""
    page = int(callback.data.split(":")[1])
    data = await state.get_data()
    orders = data.get("orders", [])
    items = [(o["id"], o["number"], o["status"]) for o in orders]
    has_next = len(orders) > (page + 1) * ORDERS_PER_PAGE
    await callback.message.edit_reply_markup(
        reply_markup=orders_list_inline(items, page=page, has_next=has_next, prefix="ylink"),
    )
    await state.update_data(page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("ylink:"))
async def ylink_order_select(callback: CallbackQuery, state: FSMContext):
    """Order selected - ask for Yandex link URL."""
    order_id = int(callback.data.split(":")[1])
    try:
        order = await get_order(order_id)
    except Exception as e:
        logger.exception("Get order failed: %s", e)
        await callback.answer("Ошибка загрузки заявки.", show_alert=True)
        return
    if not order:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Заявка №{order['number']}\n\nВставьте ссылку на папку Яндекс.Диск:"
    )
    await state.update_data(selected_order_id=order_id, selected_order_number=order["number"])
    await state.set_state("yandex_link:enter_url")
    await callback.answer()


@router.message(StateFilter("yandex_link:enter_url"), F.text)
async def ylink_enter_url(message: Message, state: FSMContext):
    """Process entered URL."""
    url = message.text.strip()
    if not YANDEX_DISK_PATTERN.search(url):
        await message.answer("Введите корректную ссылку Яндекс.Диск (например: https://disk.yandex.ru/...)")
        return
    data = await state.get_data()
    order_id = data.get("selected_order_id")
    order_number = data.get("selected_order_number", "")
    if not order_id:
        await message.answer("Ошибка: заявка не выбрана. Начните заново.")
        await state.clear()
        return

    # Строго: прикреплять ссылку может только ответственный админ.
    # Если ответственный уже назначен на другого — блокируем с причиной.
    try:
        current = await get_order(int(order_id))
    except Exception as e:  # noqa: BLE001
        logger.exception("Get order before ylink update failed: %s", e)
        await message.answer("Ошибка загрузки заявки. Попробуйте позже.")
        return
    if not current:
        await message.answer("Заявка не найдена.")
        await state.clear()
        return

    actor_id = message.from_user.id if message.from_user else 0
    actor_username = message.from_user.username if message.from_user else None
    resp_id = current.get("responsible_telegram_id")
    resp_username = current.get("responsible_username")
    if resp_id and int(resp_id) != int(actor_id):
        who = f"@{resp_username}" if resp_username else str(resp_id)
        await message.answer(
            "Нельзя прикрепить ссылку: вы не являетесь ответственным по этой заявке.\n"
            f"Ответственный: {who}\n\n"
            "Сначала поменяйте ответственного, затем прикрепляйте ссылку."
        )
        return
    try:
        payload_kwargs = {"yandex_link": url}
        # Если ответственный ещё не назначен — назначаем текущего админа.
        if (not resp_id) and actor_id:
            payload_kwargs["responsible_telegram_id"] = actor_id
            payload_kwargs["responsible_username"] = actor_username
        updated = await update_order(order_id, **payload_kwargs)
    except Exception as e:
        logger.exception("Update order failed: %s", e)
        await message.answer("Ошибка при сохранении ссылки. Попробуйте позже.")
        return
    if not updated:
        await message.answer("Ошибка обновления заявки.")
        await state.clear()
        return

    new_status = updated.get("status") or "обновлён"
    status_phrase = f"Статус заявки: «{new_status}»." if new_status != "обновлён" else "Статус заявки обновлён."

    # Уведомление в рабочий чат
    settings = get_settings()
    if settings.work_chat_id_for_send:
        try:
            await message.bot.send_message(
                chat_id=settings.work_chat_id_for_send,
                text=f"Заявка №{order_number.split('-')[-1]} выполнена\n\nСсылка на файлы: {url}",
            )
        except Exception as e:
            logger.exception("Send to work chat failed: %s", e)
    # Уведомление всем админам в личку
    who = f"@{message.from_user.username}" if message.from_user and message.from_user.username else (message.from_user.full_name or "пользователь") if message.from_user else "пользователь"
    admin_text = (
        f"Ссылка добавлена к заявке №{order_number.split('-')[-1]}\n"
        f"Добавил: {who}\n\n"
        f"Ссылка: {url}\n\n"
        f"{status_phrase}"
    )
    for admin_id in settings.admin_ids_list:
        try:
            await message.bot.send_message(chat_id=admin_id, text=admin_text)
        except Exception as e:
            logger.warning("Send link notification to admin %s failed: %s", admin_id, e)

    # После добавления ссылки спрашиваем, отправлять ли заявку.
    # Важно: вопрос уместен только если ссылка реально есть.
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"ysend_yes:{order_id}"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"ysend_no:{order_id}"),
            ]
        ]
    )
    await state.clear()
    await message.answer(f"Ссылка добавлена. {status_phrase}")
    if updated.get("yandex_link"):
        await message.answer("Желаете отправить заявку?", reply_markup=kb)
    else:
        uid = message.from_user.id if message.from_user else 0
        await message.answer("Главное меню:", reply_markup=main_menu_kb(is_admin=is_admin(uid)))


@router.callback_query(F.data.startswith("ysend_no:"))
async def ysend_no(callback: CallbackQuery):
    """Не отправлять: оставляем статус 'готово'."""
    try:
        await callback.answer("Ок, оставляю «готово».")
    except Exception:
        pass
    uid = callback.from_user.id if callback.from_user else 0
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb(is_admin=is_admin(uid)))


@router.callback_query(F.data.startswith("ysend_yes:"))
async def ysend_yes(callback: CallbackQuery):
    """Отправить: меняем статус на 'отправлена' (только если ссылка есть)."""
    if not callback.from_user:
        await callback.answer("Ошибка.", show_alert=True)
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.answer("Ошибка.", show_alert=True)
        return
    try:
        order = await get_order(order_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Get order for ysend_yes failed: %s", e)
        await callback.answer("Ошибка загрузки заявки.", show_alert=True)
        return
    if not order or not order.get("yandex_link"):
        await callback.answer("Ссылка не прикреплена. Отправка невозможна.", show_alert=True)
        return

    try:
        updated = await update_order(order_id, status="отправлена")
    except Exception as e:  # noqa: BLE001
        logger.exception("Set status sent failed: %s", e)
        await callback.answer("Ошибка отправки. Попробуйте позже.", show_alert=True)
        return
    if not updated:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    # Уведомление автору: заявка готова, ссылка прикреплена
    try:
        author_id = order.get("author_telegram_id")
        if author_id:
            text_parts = [f"Ваша заявка №{updated.get('number') or order.get('number')} готова."]
            link = order.get("yandex_link") or updated.get("yandex_link")
            if link:
                text_parts.append(f"Ссылка на файлы: {link}")
            await callback.bot.send_message(chat_id=author_id, text="\n".join(text_parts))
    except Exception as e:  # noqa: BLE001
        # Не ломаем сценарий, но админа предупреждаем, если уведомление не дошло
        logger.warning("Notify order author failed (order_id=%s): %s", order_id, e)
        try:
            await callback.message.answer(
                "Статус поменял на «отправлена», но автору не смог отправить сообщение (возможно, пользователь не запускал бота)."
            )
        except Exception:
            pass

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Отправлено.")
    uid = callback.from_user.id
    await callback.message.answer(
        f"Заявка №{updated.get('number')} отправлена.",
        reply_markup=main_menu_kb(is_admin=is_admin(uid)),
    )
