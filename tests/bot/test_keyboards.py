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


def test_order_detail_back_kb_admin_has_all_status_buttons():
    kb = order_detail_back_kb(is_admin=True, order_id=123, current_status="в работе")
    texts = _status_button_texts(kb)
    assert set(texts) == {"В работе", "Готово", "Отправлена"}


def test_order_detail_back_kb_has_more_button_under_delete():
    kb = order_detail_back_kb(is_admin=True, order_id=123, current_status="готово")
    flat = [btn for row in kb.inline_keyboard for btn in row]
    more = [b for b in flat if b.callback_data and b.callback_data.startswith("ordmore:")]
    assert len(more) == 1
    assert more[0].text == "Подробнее"
    delete_idx = next(i for i, b in enumerate(flat) if b.callback_data and "adel_confirm" in b.callback_data)
    more_idx = next(i for i, b in enumerate(flat) if b.callback_data and b.callback_data.startswith("ordmore:"))
    back_idx = next(i for i, b in enumerate(flat) if b.text == "« Назад")
    assert delete_idx < more_idx < back_idx


def test_order_detail_back_kb_user_sees_more_without_admin_row():
    kb = order_detail_back_kb(is_admin=False, order_id=55)
    flat = [btn for row in kb.inline_keyboard for btn in row]
    assert not any(b.callback_data and b.callback_data.startswith("st:") for b in flat)
    assert any(b.callback_data == "ordmore:55" for b in flat)
    assert not any(b.callback_data and b.callback_data.startswith("del_confirm:") for b in flat)


def test_order_detail_back_kb_user_delete_when_created_status():
    kb = order_detail_back_kb(is_admin=False, order_id=77, show_user_delete=True)
    flat = [btn for row in kb.inline_keyboard for btn in row]
    assert any(b.callback_data == "del_confirm:77" for b in flat)
    delete_idx = next(i for i, b in enumerate(flat) if b.callback_data == "del_confirm:77")
    more_idx = next(i for i, b in enumerate(flat) if b.callback_data == "ordmore:77")
    back_idx = next(i for i, b in enumerate(flat) if b.text == "« Назад")
    assert delete_idx < more_idx < back_idx


def test_order_detail_back_kb_trash_shows_purge_only():
    kb = order_detail_back_kb(is_admin=True, order_id=99, in_trash=True)
    flat = [btn for row in kb.inline_keyboard for btn in row]
    assert not any(b.callback_data and b.callback_data.startswith("st:") for b in flat)
    assert any(b.callback_data == "purge1:99" for b in flat)
    assert any(b.callback_data == "ordmore:99" for b in flat)
