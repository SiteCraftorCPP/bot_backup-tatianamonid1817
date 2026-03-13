"""Обработка обновлений, не подошедших ни под один хендлер (чтобы не было «Update is not handled»)."""
from aiogram import Router
from aiogram.types import Message, CallbackQuery

router = Router()


@router.message()
async def fallback_message(message: Message):
    """Любое сообщение, не обработанное другими хендлерами (фото, документ не в том шаге и т.д.)."""
    # Команды (начинаются с "/") не трогаем: пусть ими занимаются другие хендлеры.
    if (message.text or "").startswith("/"):
        return
    await message.answer(
        "Используйте кнопки меню или введите текст по инструкции. "
        "Чтобы начать заново — нажмите /start."
    )


@router.callback_query()
async def fallback_callback(callback: CallbackQuery):
    """Callback, не обработанный другими хендлерами (устаревшая кнопка и т.д.)."""
    await callback.answer("Действие недоступно. Обновите меню или нажмите /start.", show_alert=False)
