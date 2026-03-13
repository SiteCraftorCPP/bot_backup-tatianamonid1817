"""User management handlers (add/update users and roles)."""
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from config import get_settings
from bot import api_client


logger = logging.getLogger(__name__)
router = Router()


async def _is_admin(telegram_id: int) -> bool:
    settings = get_settings()
    if telegram_id in settings.admin_ids_list:
        return True
    try:
        user = await api_client.get_user(telegram_id)
    except Exception:  # noqa: BLE001
        return False
    return bool(user and str(user.get("role")) == "admin")


class UserStates(StatesGroup):
    waiting_for_user_data = State()
    waiting_for_delete_id = State()


@router.message(Command("add_user"))
async def cmd_add_user(message: Message, state: FSMContext) -> None:
    """Entry point: only admins can add/update admins.

    Поддерживаются два варианта:
    1) /add_user        -> бот просит ID отдельным сообщением;
    2) /add_user <id>   -> ID передаётся сразу.
    """
    user_id = message.from_user.id if message.from_user else 0
    if not await _is_admin(user_id):
        await message.answer("Эта команда доступна только администраторам.")
        return

    text = (message.text or "").strip()
    parts = text.split()
    # Вариант 2: /add_user <id> [@username]
    if len(parts) >= 2 and parts[1].isdigit():
        telegram_id = int(parts[1])
        username_arg = parts[2] if len(parts) >= 3 else None
        if username_arg and username_arg.startswith("@"):
            username_arg = username_arg[1:]
        try:
            # Пытаемся сначала получить текущие username/full_name из БД.
            try:
                existing = await api_client.get_user(telegram_id)
            except Exception:
                existing = None
            username = username_arg or (existing.get("username") if existing else None)
            full_name = existing.get("full_name") if existing else None

            data = await api_client.upsert_user(
                telegram_id=telegram_id,
                username=username,
                full_name=full_name,
                role="admin",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to upsert user via API")
            await message.answer(f"Ошибка при сохранении пользователя: {e}")
            return

        await message.answer(
            "Пользователь сохранён как администратор.\n"
            f"telegram_id: <code>{data.get('telegram_id')}</code>\n"
            "role: <b>admin</b>",
            parse_mode="HTML",
        )
        return

    # Вариант 1: только /add_user — просим ID (и опционально username) следующим сообщением.
    await message.answer(
        "Отправьте данные пользователя одной строкой.\n"
        "Пример: <code>7600749840</code> или <code>7600749840 @username</code>",
        parse_mode="HTML",
    )
    await state.set_state(UserStates.waiting_for_user_data)


@router.message(UserStates.waiting_for_user_data)
async def handle_user_data(message: Message, state: FSMContext) -> None:
    """Parse admin input and call backend /users/ upsert endpoint."""
    admin_id = message.from_user.id if message.from_user else 0
    if not await _is_admin(admin_id):
        await message.answer("Эта команда доступна только администраторам.")
        await state.clear()
        return

    text = (message.text or "").strip()
    parts = text.split()
    if not parts or not parts[0].isdigit():
        await message.answer(
            "ID должен быть числом. Пример: <code>7600749840</code> или "
            "<code>7600749840 @username</code>",
            parse_mode="HTML",
        )
        return

    telegram_id = int(parts[0])
    username_arg = parts[1] if len(parts) >= 2 else None
    if username_arg and username_arg.startswith("@"):
        username_arg = username_arg[1:]

    try:
        try:
            existing = await api_client.get_user(telegram_id)
        except Exception:
            existing = None
        username = username_arg or (existing.get("username") if existing else None)
        full_name = existing.get("full_name") if existing else None

        data = await api_client.upsert_user(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
            role="admin",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to upsert user via API")
        await message.answer(f"Ошибка при сохранении пользователя: {e}")
        await state.clear()
        return

    await message.answer(
        "Пользователь сохранён как администратор.\n"
        f"telegram_id: <code>{data.get('telegram_id')}</code>\n"
        "role: <b>admin</b>",
        parse_mode="HTML",
    )
    await state.clear()


@router.message(Command("del_user"))
async def cmd_del_user(message: Message, state: FSMContext) -> None:
    """Начало снятия прав администратора (только для админов).

    Поддерживаются два варианта:
    1) /del_user        -> бот просит ID отдельным сообщением;
    2) /del_user <id>   -> ID передаётся сразу.
    """
    user_id = message.from_user.id if message.from_user else 0
    if not await _is_admin(user_id):
        await message.answer("Эта команда доступна только администраторам.")
        return

    text = (message.text or "").strip()
    parts = text.split()
    # Вариант 2: /del_user <id> [@username] — username игнорируем, нам важен только id.
    if len(parts) >= 2 and parts[1].isdigit():
        telegram_id = int(parts[1])
        try:
            data = await api_client.upsert_user(
                telegram_id=telegram_id,
                username=None,
                full_name=None,
                role="user",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to downgrade user via API")
            await message.answer(f"Ошибка при изменении роли пользователя: {e}")
            return

        if not data:
            await message.answer(
                "Пользователь с таким <code>telegram_id</code> не найден.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "Права администратора сняты.\n"
                f"telegram_id: <code>{data.get('telegram_id')}</code>\n"
                "role: <b>user</b>",
                parse_mode="HTML",
            )
        return

    # Вариант 1: только /del_user — просим ID следующим сообщением.
    await message.answer(
        "Отправьте данные пользователя одной строкой.\n"
        "Пример: <code>7600749840</code>",
        parse_mode="HTML",
    )
    await state.set_state(UserStates.waiting_for_delete_id)


@router.message(UserStates.waiting_for_delete_id)
async def handle_delete_user(message: Message, state: FSMContext) -> None:
    """Удаление прав администратора: перевод в роль user."""
    admin_id = message.from_user.id if message.from_user else 0
    if not await _is_admin(admin_id):
        await message.answer("Эта команда доступна только администраторам.")
        await state.clear()
        return

    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer(
            "ID должен быть числом. Пример: <code>7600749840</code>",
            parse_mode="HTML",
        )
        return

    telegram_id = int(text)

    try:
        data = await api_client.upsert_user(
            telegram_id=telegram_id,
            username=None,
            full_name=None,
            role="user",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to downgrade user via API")
        await message.answer(f"Ошибка при изменении роли пользователя: {e}")
        await state.clear()
        return

    if not data:
        await message.answer(
            "Пользователь с таким <code>telegram_id</code> не найден.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "Права администратора сняты.\n"
            f"telegram_id: <code>{data.get('telegram_id')}</code>\n"
            "role: <b>user</b>",
            parse_mode="HTML",
        )
    await state.clear()


@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    """Показать информацию о текущем пользователе и его роли."""
    user_id = message.from_user.id if message.from_user else 0
    username = message.from_user.username if message.from_user else None
    full_name = message.from_user.full_name if message.from_user else None

    # Роль в конфиге (ADMIN_IDS)
    is_cfg_admin = await _is_admin(user_id)

    # Роль в БД (если есть)
    db_role: str | None = None
    try:
        user_data = await api_client.get_user(user_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to fetch user via API")
        user_data = None

    if user_data:
        db_role = str(user_data.get("role") or "")

    text_lines = [
        f"Ваш telegram_id: <code>{user_id}</code>",
        f"username: <code>@{username}</code>" if username else "username: <i>нет</i>",
        f"ФИО: <code>{full_name}</code>" if full_name else "ФИО: <i>нет</i>",
        "",
        f"Роль в БД: <b>{db_role or 'не заведён'}</b>",
        f"Админ по конфигу (ADMIN_IDS): <b>{'да' if is_cfg_admin else 'нет'}</b>",
    ]

    await message.answer("\n".join(text_lines), parse_mode="HTML")

    # Синхронизируем username/full_name в БД для админов.
    role_is_admin = (db_role == "admin") or is_cfg_admin
    if role_is_admin and message.from_user:
        try:
            await api_client.upsert_user(
                telegram_id=user_id,
                username=username,
                full_name=full_name,
                role="admin",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to sync admin user via whoami: %s", e)

