"""Main menu and start handler."""
import logging
import re
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.dispatcher.event.bases import SkipHandler

from config import get_settings
from bot.keyboards import main_menu_kb, stats_mode_inline_kb
from bot import api_client

router = Router()
logger = logging.getLogger(__name__)

_PERIOD_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{2})\b")

# Тексты кнопок/команд, которые должны переключать раздел даже во время ввода периода.
_NAV_BUTTON_TEXTS = {
    "📜 История заявок",
    "📦 Мои заявки",
    "📝 Заявка по шаблону",
    "📎 Прикрепить файл",
    "🔗 Добавить ссылку на файлы",
    "👤 Управление пользователями",
    "📊 Статистика",
    "❓ Помощь",
    "« Назад",
}

class StatsStates(StatesGroup):
    waiting_for_period = State()


async def is_admin(telegram_id: int) -> bool:
    """Проверка прав админа: сначала по ADMIN_IDS, затем по роли в БД."""
    settings = get_settings()
    if telegram_id in settings.admin_ids_list:
        return True
    try:
        user = await api_client.get_user(telegram_id)
    except Exception:  # noqa: BLE001
        return False
    return bool(user and str(user.get("role")) == "admin")


@router.message(CommandStart())
async def cmd_start(message: Message):
    """Handle /start."""
    user_id = message.from_user.id if message.from_user else 0
    is_adm = await is_admin(user_id)

    # Если это админ (по ENV или по БД) — синхронизируем его username/full_name
    # в реестре пользователей, чтобы затем использовать в статистике и списках.
    if is_adm and message.from_user:
        try:
            await api_client.upsert_user(
                telegram_id=user_id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
                role="admin",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to sync admin user via /start: %s", e)
    await message.answer(
        "Добро пожаловать в бот заявок «Честный знак».\n\n"
        "Выберите действие:",
        reply_markup=main_menu_kb(is_admin=is_adm),
    )


@router.message(F.text == "« Назад")
async def back_to_main(message: Message, state: FSMContext):
    """Return to main menu from any state."""
    await state.clear()
    user_id = message.from_user.id if message.from_user else 0
    is_adm = await is_admin(user_id)
    await message.answer("Главное меню:", reply_markup=main_menu_kb(is_admin=is_adm))


@router.message(F.text == "📊 Статистика")
async def stats_menu(message: Message):
    """Открыть меню статистики (только админам)."""
    user_id = message.from_user.id if message.from_user else 0
    if not await is_admin(user_id):
        await message.answer("Доступно только администраторам.")
        return
    await message.answer(
        "📊 Статистика — выберите режим:",
        reply_markup=stats_mode_inline_kb(),
    )


@router.callback_query(F.data.startswith("stats:"))
async def stats_choose_mode(callback: CallbackQuery, state: FSMContext):
    if not callback.from_user:
        await callback.answer("Ошибка.", show_alert=True)
        return
    user_id = callback.from_user.id
    if not await is_admin(user_id):
        await callback.answer("Доступно только администраторам.", show_alert=True)
        return

    mode = (callback.data or "").split(":", 1)[1]
    if mode == "cancel":
        await state.clear()
        await callback.message.edit_text("Ок, отменил.")
        await callback.answer()
        return

    if mode == "month":
        try:
            stats = await api_client.get_stats_summary()
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to fetch stats: %s", e)
            await callback.answer()
            await callback.message.answer("Ошибка получения статистики. Попробуйте позже.")
            return

        year = stats.get("year")
        month = stats.get("month")
        total = stats.get("total_orders", 0)
        by_user = stats.get("by_user") or []
        by_admin = stats.get("by_admin_completed") or []

        lines: list[str] = []
        lines.append(f"📊 Статистика за {month:02d}.{year}:")
        lines.append("")
        lines.append(f"Всего заявок создано: <b>{total}</b>")

        if by_user:
            lines.append("")
            lines.append("👤 По пользователям (создали заявок):")
            for row in by_user:
                uname = row.get("username") or ""
                tid = row.get("telegram_id")
                label = f"@{uname}" if uname else str(tid)
                lines.append(f"- {label}: {row.get('orders_count', 0)}")

        if by_admin:
            lines.append("")
            lines.append("👨‍💼 По администраторам (выполнили заявок):")
            for row in by_admin:
                uname = row.get("username") or ""
                tid = row.get("telegram_id")
                label = f"@{uname}" if uname else str(tid)
                lines.append(f"- {label}: {row.get('orders_count', 0)}")

        await callback.answer()
        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=stats_mode_inline_kb(),
        )
        return

    if mode == "period":
        await state.set_state(StatsStates.waiting_for_period)
        await callback.answer()
        await callback.message.edit_text(
            "Введите период одной строкой в формате:\n"
            "<code>с 01.03.26 по 16.03.26</code>\n"
            "или\n"
            "<code>01.03.26 16.03.26</code>\n\n"
            "Для отмены нажмите «Отмена».",
            parse_mode="HTML",
            reply_markup=stats_mode_inline_kb(),
        )
        return

    await callback.answer("Неизвестная команда.", show_alert=True)


@router.message(StatsStates.waiting_for_period)
async def stats_period_apply(message: Message, state: FSMContext):
    """Построить статистику по вручную введённому периоду."""
    user_id = message.from_user.id if message.from_user else 0
    if not await is_admin(user_id):
        await message.answer("Доступно только администраторам.")
        return
    text = (message.text or "").strip()

    # Не блокируем навигацию: при выборе другого раздела выходим из режима периода
    # и пропускаем апдейт в соответствующий хендлер.
    if text in _NAV_BUTTON_TEXTS or text.startswith("/"):
        await state.clear()
        raise SkipHandler()

    # Достаём 2 даты дд.мм.гг
    m = _PERIOD_DATE_RE.findall(text)
    if len(m) < 2:
        await message.answer("Не понял период. Пример: <code>с 01.03.26 по 16.03.26</code>", parse_mode="HTML")
        return
    date_from, date_to = m[0], m[1]
    try:
        stats = await api_client.get_stats_summary(date_from=date_from, date_to=date_to)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to fetch period stats: %s", e)
        await message.answer("Ошибка получения статистики за период. Попробуйте позже.")
        return

    total = stats.get("total_orders", 0)
    by_user = stats.get("by_user") or []
    by_admin = stats.get("by_admin_completed") or []

    lines: list[str] = []
    lines.append(f"📊 Статистика за период {date_from} — {date_to}:")
    lines.append("")
    lines.append(f"Всего заявок создано: <b>{total}</b>")
    if by_user:
        lines.append("")
        lines.append("👤 По пользователям (создали заявок):")
        for row in by_user:
            uname = row.get("username") or ""
            tid = row.get("telegram_id")
            label = f"@{uname}" if uname else str(tid)
            lines.append(f"- {label}: {row.get('orders_count', 0)}")
    if by_admin:
        lines.append("")
        lines.append("👨‍💼 По администраторам (выполнили заявок):")
        for row in by_admin:
            uname = row.get("username") or ""
            tid = row.get("telegram_id")
            label = f"@{uname}" if uname else str(tid)
            lines.append(f"- {label}: {row.get('orders_count', 0)}")

    # Остаёмся в режиме "Период", чтобы админ мог ввести новый диапазон без выхода в старт.
    await state.set_state(StatsStates.waiting_for_period)
    await state.update_data(last_date_from=date_from, last_date_to=date_to)
    # В сообщении со статистикой — кнопка "Изменить период", чтобы не слать второе сообщение-инструкцию.
    kb = stats_mode_inline_kb()
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


@router.message(F.text == "❓ Помощь")
async def help_message(message: Message):
    """Показать инструкцию по работе с ботом."""
    text = (
        "🟢 Инструкция по работе с ботом:\n"
        "\n"
        "1️⃣ Выберите категорию товара:\n"
        "- Одежда 👕\n"
        "- Обувь 👟\n"
        "\n"
        "2️⃣ Выберите тип заявки:\n"
        "- Повторный товар 🔁\n"
        "- Новый товар 🆕\n"
        "\n"
        "3️⃣ Скачайте шаблон:\n"
        "- Для повторного товара: выбираете артикул → бот формирует шаблон с нужными полями.\n"
        "- Для нового товара: выбираете юридическое лицо, бренд, страну производства и пол "
        "→ бот формирует пустой шаблон.\n"
        "\n"
        "4️⃣ Заполните файл:\n"
        "- Все поля являются обязательными к заполнению (кроме «Номер заказа МС»).\n"
        "- Не изменяйте названия колонок.\n"
        "\n"
        "5️⃣ Отправьте файл обратно боту:\n"
        "- Нажмите кнопку «📎 Прикрепить файл» и отправьте заполненный шаблон.\n"
        "- При необходимости после предложения бота вы можете прикрепить доп. файл для работы "
        "и оставить комментарий.\n"
        "- Бот проверит файл на ошибки и подтвердит отправку.\n"
        "\n"
        "6️⃣ После отправки:\n"
        "- Ваша заявка получит уникальный номер.\n"
        "- Администраторы получат файл для работы.\n"
        "- Заявке будет присвоен статус «создана», пока её не возьмут в работу администраторы.\n"
        "\n"
        "❗ Важно:\n"
        "- Заполняйте все обязательные поля.\n"
        "- Используйте Excel формат .xlsx.\n"
        "- Не удаляйте и не переименовывайте столбцы.\n"
    )
    await message.answer(text)

