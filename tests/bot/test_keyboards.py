"""Tests for bot keyboards and status buttons."""
from aiogram.types import InlineKeyboardMarkup

from bot.keyboards import order_detail_back_kb, skip_inline_kb


def _status_button_texts(kb: InlineKeyboardMarkup) -> list[str]:
    texts: list[str] = []
    for row in kb.inline_keyboard:
        for btn in row:
            if btn.callback_data and btn.callback_data.startswith("st:"):
                texts.append(btn.text)
    return texts


def test_skip_inline_kb_text():
    kb = skip_inline_kb("skip_test")
    assert len(kb.inline_keyboard) == 1
    btn = kb.inline_keyboard[0][0]
    assert "Пропустить" in btn.text
    assert btn.callback_data == "skip_test"


def test_order_detail_back_kb_hides_current_status_in_progress():
    kb = order_detail_back_kb(is_admin=True, order_id=123, current_status="в работе")
    texts = _status_button_texts(kb)
    # Only \"Готово\" and \"Отправлена\" should be present
    assert "В работе" not in texts
    assert "Готово" in texts
    assert "Отправлена" in texts


def test_order_detail_back_kb_shows_only_subsequent_when_ready():
    """При статусе «готово» показываем только последующий статус «Отправлена», без возврата назад."""
    kb = order_detail_back_kb(is_admin=True, order_id=123, current_status="готово")
    texts = _status_button_texts(kb)
    assert "Готово" not in texts
    assert "В работе" not in texts  # возврат назад запрещён
    assert "Отправлена" in texts


def test_order_detail_back_kb_shows_all_when_created():
    kb = order_detail_back_kb(is_admin=True, order_id=123, current_status="создана")
    texts = _status_button_texts(kb)
    # All three admin status buttons should be available
    assert set(texts) == {"В работе", "Готово", "Отправлена"}
