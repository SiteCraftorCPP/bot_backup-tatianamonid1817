"""User management handlers (add/update users and roles)."""
import logging
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from config import get_settings
from bot import api_client
from bot.handlers.main_menu import is_admin as _is_admin


logger = logging.getLogger(__name__)
router = Router()
BOT_BUILD = "2026-04-10_demote_unassign_orders"


async def _clear_responsible_on_orders_after_staff_change(
    *,
    target_telegram_id: int,
    requester_telegram_id: int,
) -> str:
    """После демоута/блокировки — снять ответственного со всех заявок этого telegram_id."""
    try:
        n = await api_client.unassign_responsible_orders_by_telegram(
            target_telegram_id=target_telegram_id,
            requester_telegram_id=requester_telegram_id,
        )
    except Exception:
        logger.exception(
            "unassign_responsible_orders_by_telegram failed (target=%s)",
            target_telegram_id,
        )
        return (
            "\n\n<i>Не удалось автоматически снять ответственность с заявок "
            "(проверьте бэкенд и логи).</i>"
        )
    if n <= 0:
        return ""
    return (
        f"\n\nОтветственный снят с <b>{n}</b> заявок в базе. "
        "Откройте «Историю заявок» заново, чтобы увидеть обновление."
    )


def _normalize_username(username: str | None) -> str | None:
    """Нормализация username из /add_admin: убираем @ и пробелы."""
    if username is None:
        return None
    value = username.strip()
    if value.startswith("@"):
        value = value[1:].strip()
    return value or None


def _remove_admin_id_from_env(telegram_id: int) -> bool:
    """Удалить telegram_id из ADMIN_IDS в .env (если присутствует).

    Возвращает True, если файл был изменён.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except Exception:
        return False

    lines = text.splitlines(keepends=True)
    changed = False
    new_lines: list[str] = []

    for line in lines:
        # Игнорируем комментарии и пустые строки
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not stripped.startswith("ADMIN_IDS="):
            new_lines.append(line)
            continue

        # ADMIN_IDS=1,2,3
        prefix, value = line.split("=", 1)
        raw = value.strip()
        # сохраняем перенос строки (если был)
        newline = "\n" if line.endswith("\n") else ""
        if raw.endswith("\n"):
            raw = raw[:-1]
        ids = [x.strip() for x in raw.split(",") if x.strip()]
        ids2 = [x for x in ids if x != str(telegram_id)]
        if ids2 != ids:
            changed = True
        new_value = ",".join(ids2)
        new_lines.append(f"{prefix}={new_value}{newline}")

    if not changed:
        return False

    try:
        env_path.write_text("".join(new_lines), encoding="utf-8")
    except Exception:
        return False
    return True


class UserStates(StatesGroup):
    waiting_for_user_data = State()
    waiting_for_admin_data = State()
    waiting_for_delete_id = State()
    waiting_for_demote_id = State()


@router.message(Command("add_user"))
async def cmd_add_user(message: Message, state: FSMContext) -> None:
    """Entry point: only admins can add allowed users (role=user).

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
        username_arg = _normalize_username(parts[2] if len(parts) >= 3 else None)
        try:
            # Пытаемся сначала получить текущие username/full_name из БД.
            try:
                existing = await api_client.get_user(telegram_id)
            except Exception:
                existing = None
            username = username_arg or _normalize_username(existing.get("username") if existing else None)
            full_name = existing.get("full_name") if existing else None

            data = await api_client.upsert_user(
                telegram_id=telegram_id,
                username=username,
                full_name=full_name,
                role="user",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to upsert user via API")
            await message.answer(f"Ошибка при сохранении пользователя: {e}")
            return

        await message.answer(
            "Пользователь добавлен и может пользоваться ботом.\n"
            f"telegram_id: <code>{data.get('telegram_id')}</code>\n"
            "role: <b>user</b>",
            parse_mode="HTML",
        )
        return

    # Вариант 1: только /add_user — просим ID (и опционально username) следующим сообщением.
    await message.answer(
        "Отправьте данные пользователя одной строкой.\n"
        "Пример: <code>7600749840</code>",
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
    username_arg = _normalize_username(parts[1] if len(parts) >= 2 else None)

    try:
        try:
            existing = await api_client.get_user(telegram_id)
        except Exception:
            existing = None
        username = username_arg or _normalize_username(existing.get("username") if existing else None)
        full_name = existing.get("full_name") if existing else None

        data = await api_client.upsert_user(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
            role="user",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to upsert user via API")
        await message.answer(f"Ошибка при сохранении пользователя: {e}")
        await state.clear()
        return

    await message.answer(
        "Пользователь добавлен и может пользоваться ботом.\n"
        f"telegram_id: <code>{data.get('telegram_id')}</code>\n"
        "role: <b>user</b>",
        parse_mode="HTML",
    )
    await state.clear()


@router.message(Command("add_admin"))
async def cmd_add_admin(message: Message, state: FSMContext) -> None:
    """Entry point: only admins can add/update admins (role=admin).

    Поддерживаются два варианта:
    1) /add_admin        -> бот просит ID отдельным сообщением;
    2) /add_admin <id>   -> ID передаётся сразу.
    """
    user_id = message.from_user.id if message.from_user else 0
    if not await _is_admin(user_id):
        await message.answer("Эта команда доступна только администраторам.")
        return

    text = (message.text or "").strip()
    parts = text.split()
    # Вариант 2: /add_admin <id> [@username]
    if len(parts) >= 2 and parts[1].isdigit():
        telegram_id = int(parts[1])
        username_arg = _normalize_username(parts[2] if len(parts) >= 3 else None)
        try:
            try:
                existing = await api_client.get_user(telegram_id)
            except Exception:
                existing = None
            username = username_arg or _normalize_username(existing.get("username") if existing else None)
            if not username:
                await message.answer(
                    "Для /add_admin укажите username: "
                    "<code>/add_admin 6933111964 @AmbassadorSoft1</code>",
                    parse_mode="HTML",
                )
                return
            full_name = existing.get("full_name") if existing else None

            data = await api_client.upsert_user(
                telegram_id=telegram_id,
                username=username,
                full_name=full_name,
                role="admin",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to upsert admin via API")
            await message.answer(f"Ошибка при сохранении администратора: {e}")
            return

        await message.answer(
            "Пользователь сохранён как администратор.\n"
            f"telegram_id: <code>{data.get('telegram_id')}</code>\n"
            "role: <b>admin</b>",
            parse_mode="HTML",
        )
        return

    # Вариант 1: только /add_admin — просим ID (и опционально username) следующим сообщением.
    await message.answer(
        "Отправьте данные пользователя одной строкой.\n"
        "Пример: <code>7600749840</code> или <code>7600749840 @username</code>",
        parse_mode="HTML",
    )
    await state.set_state(UserStates.waiting_for_admin_data)


@router.message(UserStates.waiting_for_admin_data)
async def handle_admin_data(message: Message, state: FSMContext) -> None:
    """Parse admin input and call backend /users/ upsert endpoint (role=admin)."""
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
    username_arg = _normalize_username(parts[1] if len(parts) >= 2 else None)

    try:
        try:
            existing = await api_client.get_user(telegram_id)
        except Exception:
            existing = None
        username = username_arg or _normalize_username(existing.get("username") if existing else None)
        if not username:
            await message.answer(
                "Для добавления админа укажите username: "
                "<code>6933111964 @AmbassadorSoft1</code>",
                parse_mode="HTML",
            )
            return
        full_name = existing.get("full_name") if existing else None

        data = await api_client.upsert_user(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
            role="admin",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to upsert admin via API")
        await message.answer(f"Ошибка при сохранении администратора: {e}")
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
    """Отключить пользователю доступ к боту (только для админов).

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
    # Вариант 2: /del_user <id> — всё остальное игнорируем.
    if len(parts) >= 2 and parts[1].isdigit():
        telegram_id = int(parts[1])
        try:
            ok = await api_client.delete_user(telegram_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to delete user via API")
            await message.answer(f"Ошибка при отключении доступа: {e}")
            return

        if not ok:
            await message.answer("Пользователь не найден.")
            return

        umsg = await _clear_responsible_on_orders_after_staff_change(
            target_telegram_id=telegram_id,
            requester_telegram_id=user_id,
        )

        # Если id был в ADMIN_IDS — убираем его оттуда автоматически.
        settings = get_settings()
        if telegram_id in settings.admin_ids_list:
            if _remove_admin_id_from_env(telegram_id):
                get_settings.cache_clear()
            else:
                await message.answer(
                    "Пользователь заблокирован в БД."
                    + umsg
                    + "\n\nНе удалось автоматически обновить <b>.env</b> (ADMIN_IDS).\n"
                    "Уберите telegram_id из ADMIN_IDS вручную и перезапустите бота.",
                    parse_mode="HTML",
                )
                return

        await message.answer("Доступ к боту отключён." + umsg, parse_mode="HTML")
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
    """Отключение доступа к боту: удаление пользователя из БД."""
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
        ok = await api_client.delete_user(telegram_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to delete user via API")
        await message.answer(f"Ошибка при отключении доступа: {e}")
        await state.clear()
        return

    if not ok:
        await message.answer("Пользователь не найден.")
        await state.clear()
        return

    umsg = await _clear_responsible_on_orders_after_staff_change(
        target_telegram_id=telegram_id,
        requester_telegram_id=admin_id,
    )

    settings = get_settings()
    if telegram_id in settings.admin_ids_list:
        if _remove_admin_id_from_env(telegram_id):
            get_settings.cache_clear()
        else:
            await message.answer(
                "Пользователь заблокирован в БД."
                + umsg
                + "\n\nНе удалось автоматически обновить <b>.env</b> (ADMIN_IDS).\n"
                "Уберите telegram_id из ADMIN_IDS вручную и перезапустите бота.",
                parse_mode="HTML",
            )
            await state.clear()
            return

    await message.answer("Доступ к боту отключён." + umsg, parse_mode="HTML")
    await state.clear()


@router.message(Command("demote_admin"))
async def cmd_demote_admin(message: Message, state: FSMContext) -> None:
    """Снять права администратора, но оставить доступ к боту (role=user)."""
    user_id = message.from_user.id if message.from_user else 0
    if not await _is_admin(user_id):
        await message.answer("Эта команда доступна только администраторам.")
        return

    text = (message.text or "").strip()
    parts = text.split()
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
            logger.exception("Failed to demote admin via API")
            await message.answer(f"Ошибка при изменении роли: {e}")
            return

        if not data:
            await message.answer("Пользователь не найден.")
            return

        settings = get_settings()
        note = ""
        if telegram_id in settings.admin_ids_list:
            note = (
                "\n\nЭтот telegram_id всё ещё в <b>ADMIN_IDS</b> — для порядка уберите его из конфига "
                "и перезапустите бота."
            )
        umsg = await _clear_responsible_on_orders_after_staff_change(
            target_telegram_id=telegram_id,
            requester_telegram_id=user_id,
        )
        await message.answer(
            "Права администратора сняты (доступ к боту сохранён)." + note + umsg,
            parse_mode="HTML",
        )
        return

    await message.answer(
        "Отправьте telegram_id одной строкой.\nПример: <code>7600749840</code>",
        parse_mode="HTML",
    )
    await state.set_state(UserStates.waiting_for_demote_id)


@router.message(UserStates.waiting_for_demote_id)
async def handle_demote_admin(message: Message, state: FSMContext) -> None:
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
        logger.exception("Failed to demote admin via API")
        await message.answer(f"Ошибка при изменении роли: {e}")
        await state.clear()
        return

    if not data:
        await message.answer("Пользователь не найден.")
        await state.clear()
        return

    settings = get_settings()
    note = ""
    if telegram_id in settings.admin_ids_list:
        note = (
            "\n\nЭтот telegram_id всё ещё в <b>ADMIN_IDS</b> — для порядка уберите его из конфига "
            "и перезапустите бота."
        )
    umsg = await _clear_responsible_on_orders_after_staff_change(
        target_telegram_id=telegram_id,
        requester_telegram_id=admin_id,
    )
    await message.answer(
        "Права администратора сняты (доступ к боту сохранён)." + note + umsg,
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


@router.message(Command("version"))
async def cmd_version(message: Message) -> None:
    """Диагностика: какой код сейчас отвечает."""
    await message.answer(f"build: <code>{BOT_BUILD}</code>", parse_mode="HTML")

