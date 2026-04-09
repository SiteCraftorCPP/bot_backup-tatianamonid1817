"""User flow: attach additional file to an existing order."""
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.types import Message, CallbackQuery

from bot.api_client import (
    get_orders,
    get_order,
    add_order_attachment,
    admin_telegram_ids_for_notify,
)
from bot.handlers.main_menu import is_admin as is_admin_user
from bot.keyboards import main_menu_kb, orders_list_inline

router = Router()
logger = logging.getLogger(__name__)

ORDERS_PER_PAGE = 8


async def _notify_admins_new_attachment(
    message: Message,
    *,
    order_id: int,
    order_number: str | int | None,
    file_id: str,
    file_name: str | None,
    as_photo: bool,
) -> None:
    """Уведомить ответственного; если его нет — всех админов из пула уведомлений."""
    recipients: list[int] = []
    try:
        order = await get_order(int(order_id))
    except Exception:
        order = None
    resp_id = order.get("responsible_telegram_id") if order else None
    if resp_id is not None:
        try:
            recipients = [int(resp_id)]
        except (TypeError, ValueError):
            recipients = []
    if not recipients:
        try:
            recipients = await admin_telegram_ids_for_notify()
        except Exception as e:
            logger.warning("admin_telegram_ids_for_notify failed: %s", e)
            recipients = []

    for admin_id in {int(x) for x in recipients}:
        try:
            if as_photo:
                await message.bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                )
            else:
                await message.bot.send_document(
                    chat_id=admin_id,
                    document=file_id,
                )
        except Exception as e:
            logger.warning(
                "Notify admin about user attachment failed admin_id=%s order_id=%s: %s",
                admin_id,
                order_id,
                e,
            )


@router.message(F.text == "📎 Добавить файл к заявке")
async def user_attach_start(message: Message, state: FSMContext):
    """Show user's orders so they can pick one and attach a file."""
    await state.clear()
    if not message.from_user:
        return
    uid = message.from_user.id
    try:
        orders = await get_orders(author_telegram_id=uid, limit=100)
    except Exception as e:
        logger.exception("Get orders for user attach failed: %s", e)
        await message.answer("Ошибка загрузки заявок. Попробуйте позже.")
        return
    if not orders:
        await message.answer("У вас пока нет заявок для прикрепления файла.")
        return

    items = [(o["id"], o["number"], o["status"]) for o in orders]
    has_next = len(orders) > ORDERS_PER_PAGE
    await message.answer(
        "Выберите заявку, к которой хотите прикрепить файл:",
        reply_markup=orders_list_inline(
            items,
            page=0,
            has_next=has_next,
            prefix="uatt",
            back_callback="uatt_back",
        ),
    )
    await state.update_data(orders=orders, page=0)
    await state.set_state("user_attach:select_order")


@router.callback_query(F.data == "uatt_back")
async def user_attach_back(callback: CallbackQuery, state: FSMContext):
    """Back to main menu from user attach flow."""
    await state.clear()
    uid = callback.from_user.id if callback.from_user else 0
    is_adm = await is_admin_user(uid)
    try:
        await callback.message.edit_text("Главное меню:")
    except Exception:
        pass
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb(is_admin=is_adm))
    await callback.answer()


@router.callback_query(F.data.startswith("uattpg:"))
async def user_attach_page(callback: CallbackQuery, state: FSMContext):
    """Paginate user orders in attach flow."""
    page = int(callback.data.split(":")[1])
    data = await state.get_data()
    orders = data.get("orders", [])
    items = [(o["id"], o["number"], o["status"]) for o in orders]
    has_next = len(orders) > (page + 1) * ORDERS_PER_PAGE
    await callback.message.edit_reply_markup(
        reply_markup=orders_list_inline(
            items,
            page=page,
            has_next=has_next,
            prefix="uatt",
            back_callback="uatt_back",
        ),
    )
    await state.update_data(page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("uatt:"))
async def user_attach_pick_order(callback: CallbackQuery, state: FSMContext):
    """User picked an order. Ask to send a file."""
    if not callback.from_user:
        await callback.answer("Ошибка.", show_alert=True)
        return
    uid = callback.from_user.id
    try:
        order_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    try:
        order = await get_order(order_id)
    except Exception as e:
        logger.exception("Get order for user attach failed: %s", e)
        await callback.answer("Ошибка загрузки заявки.", show_alert=True)
        return
    if not order:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if order.get("author_telegram_id") != uid:
        await callback.answer("Можно выбрать только свою заявку.", show_alert=True)
        return

    await state.update_data(
        selected_order_id=order_id,
        selected_order_number=order.get("number"),
    )
    await state.set_state("user_attach:await_file")
    await callback.message.edit_text(
        f"Заявка № {order.get('number')}\n\n"
        "Отправьте файл или фото, которые нужно прикрепить."
    )
    await callback.answer()


async def _finalize_user_attachment(
    message: Message,
    state: FSMContext,
    *,
    telegram_file_id: str,
    file_name: str | None,
    as_photo: bool,
) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    data = await state.get_data()
    order_id = data.get("selected_order_id")
    order_number = data.get("selected_order_number")
    if not order_id:
        await state.clear()
        await message.answer("Сессия истекла. Выберите заявку заново.")
        return

    try:
        await add_order_attachment(
            int(order_id),
            author_telegram_id=uid,
            telegram_file_id=telegram_file_id,
            file_name=file_name,
        )
    except Exception as e:
        logger.exception("add_order_attachment failed: %s", e)
        txt = str(e).lower()
        if "order_deleted" in txt or "удален" in txt:
            await message.answer("Эта заявка в корзине. Прикрепить файл нельзя.")
        else:
            await message.answer("Не удалось прикрепить файл. Попробуйте позже.")
        return

    await _notify_admins_new_attachment(
        message,
        order_id=int(order_id),
        order_number=order_number,
        file_id=telegram_file_id,
        file_name=file_name,
        as_photo=as_photo,
    )

    await state.clear()
    is_adm = await is_admin_user(uid)
    await message.answer(
        "Файл прикреплён к заявке.",
        reply_markup=main_menu_kb(is_admin=is_adm),
    )


@router.message(StateFilter("user_attach:await_file"), F.document)
async def user_attach_file(message: Message, state: FSMContext):
    """Attach document to selected order and notify responsible admin / all admins."""
    if not message.document:
        return
    doc = message.document
    await _finalize_user_attachment(
        message,
        state,
        telegram_file_id=doc.file_id,
        file_name=doc.file_name,
        as_photo=False,
    )


@router.message(StateFilter("user_attach:await_file"), F.photo)
async def user_attach_photo(message: Message, state: FSMContext):
    """Attach photo (as file_id) to order; admins receive as photo."""
    if not message.photo:
        return
    ph = message.photo[-1]
    name = f"photo_{ph.file_unique_id}.jpg"
    await _finalize_user_attachment(
        message,
        state,
        telegram_file_id=ph.file_id,
        file_name=name,
        as_photo=True,
    )


@router.message(StateFilter("user_attach:await_file"))
async def user_attach_wait_document(message: Message):
    await message.answer("Пожалуйста, отправьте файл документом или фото.")
