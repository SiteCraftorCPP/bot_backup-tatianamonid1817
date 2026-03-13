"""Main menu and start handler."""
import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.filters import CommandStart

from config import get_settings
from bot.keyboards import main_menu_kb
from bot import api_client

router = Router()
logger = logging.getLogger(__name__)


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
    """Показать сводную статистику за текущий месяц (только админам)."""
    user_id = message.from_user.id if message.from_user else 0
    if not await is_admin(user_id):
        await message.answer("Доступно только администраторам.")
        return
    try:
        stats = await api_client.get_stats_summary()
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to fetch stats: %s", e)
        await message.answer("Ошибка получения статистики. Попробуйте позже.")
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

    await message.answer("\n".join(lines))


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

