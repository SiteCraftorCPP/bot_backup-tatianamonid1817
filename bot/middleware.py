"""Bot middleware."""
import logging
from typing import Callable, Dict, Any, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from config import get_settings
from bot import api_client

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseMiddleware):
    """Log all updates."""
    
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user:
            uname = f"@{user.username}" if getattr(user, "username", None) else ""
            if isinstance(event, CallbackQuery):
                cb_data = getattr(event, "data", None)
                btn_text = None
                try:
                    if event.message and event.message.reply_markup:
                        for row in event.message.reply_markup.inline_keyboard:
                            for btn in row:
                                if btn.callback_data == cb_data:
                                    btn_text = btn.text
                                    raise StopIteration
                except StopIteration:
                    pass
                msg_head = ""
                try:
                    msg_text = (event.message.text or event.message.caption or "") if event.message else ""
                    msg_head = msg_text.replace("\n", " ")[:120]
                except Exception:
                    msg_head = ""
                logger.info(
                    "Update from %s %s: CallbackQuery data=%r btn=%r msg=%r",
                    user.id,
                    uname,
                    cb_data,
                    btn_text,
                    msg_head,
                )
            elif isinstance(event, Message):
                txt = (event.text or event.caption or "").replace("\n", " ")[:200]
                logger.info("Update from %s %s: Message text=%r", user.id, uname, txt)
            else:
                logger.info("Update from %s %s: %s", user.id, uname, type(event).__name__)
        return await handler(event, data)


class AccessMiddleware(BaseMiddleware):
    """Deny-by-default access.

    Разрешено:
    - админы (ADMIN_IDS или role=admin в БД)
    - пользователи, заранее заведённые в БД (role=user)
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        telegram_id = int(getattr(from_user, "id", 0) or 0)

        # Системные события без пользователя не блокируем.
        if telegram_id <= 0:
            return await handler(event, data)

        settings = get_settings()
        if telegram_id in settings.admin_ids_list:
            return await handler(event, data)

        allowed = False
        is_admin = False
        try:
            user = await api_client.get_user(telegram_id)
            if user:
                role = str(user.get("role") or "")
                allowed = role in ("user", "admin")
                is_admin = role == "admin"
        except Exception as e:  # noqa: BLE001
            logger.warning("Access check failed for %s: %s", telegram_id, e)

        if allowed:
            return await handler(event, data)

        text = "Доступ к боту закрыт.\nОбратитесь к администратору, чтобы вас добавили."

        # Для сообщений — отвечаем сообщением, для колбэков — алертом.
        if isinstance(event, Message):
            await event.answer(text, parse_mode="HTML")
            return None
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
            return None

        # На всякий случай блокируем и прочие типы апдейтов с from_user.
        return None
