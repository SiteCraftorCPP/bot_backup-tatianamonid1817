"""Reliable notifications about new orders to admins.

Goal: orders should not "get lost" even if Excel generation/sending fails.
We always try to send a document card; if it fails, we fall back to a text card with the same "take" button.
Also supports periodic retry for orders in status "создана" that have no telegram postings in DB.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
)

from bot.api_client import (
    admin_telegram_ids_for_notify,
    get_markznak_order_excel,
    get_order,
    register_order_telegram_posting,
)
from bot.notification_registry import notifications_registry
from backend.services.excel_service import get_markznak_download_filename

logger = logging.getLogger(__name__)

_PENDING_PATH = Path(__file__).resolve().parent / "pending_notifications.json"

_PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


def _is_photo_filename(file_name: str | None) -> bool:
    if not file_name:
        return False
    return str(file_name).strip().lower().endswith(_PHOTO_EXTS)


async def _send_extras_as_albums(bot, *, chat_id: int, extras: list[dict]) -> None:
    """Send extra attachments grouped: documents together, photos together (Telegram limitation)."""
    docs: list[dict] = []
    photos: list[dict] = []
    for att in extras or []:
        fid = att.get("telegram_file_id")
        if not fid:
            continue
        fn = att.get("file_name")
        if _is_photo_filename(fn):
            photos.append(att)
        else:
            docs.append(att)

    async def _send_docs_group(chunk: list[dict]) -> None:
        if not chunk:
            return
        if len(chunk) == 1:
            await bot.send_document(chat_id=chat_id, document=chunk[0]["telegram_file_id"])
            return
        media = [InputMediaDocument(media=a["telegram_file_id"]) for a in chunk]
        await bot.send_media_group(chat_id=chat_id, media=media)

    async def _send_photos_group(chunk: list[dict]) -> None:
        if not chunk:
            return
        if len(chunk) == 1:
            await bot.send_photo(chat_id=chat_id, photo=chunk[0]["telegram_file_id"])
            return
        media = [InputMediaPhoto(media=a["telegram_file_id"]) for a in chunk]
        await bot.send_media_group(chat_id=chat_id, media=media)

    # Telegram: media_group max 10 items
    i = 0
    while i < len(docs):
        await _send_docs_group(docs[i : i + 10])
        i += 10
    i = 0
    while i < len(photos):
        await _send_photos_group(photos[i : i + 10])
        i += 10


def _load_pending_ids() -> list[int]:
    try:
        if not _PENDING_PATH.exists():
            return []
        raw = _PENDING_PATH.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            ids = data.get("order_ids") or []
        else:
            ids = data or []
        out: list[int] = []
        for x in ids:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        # uniq keep order
        seen: set[int] = set()
        uniq: list[int] = []
        for oid in out:
            if oid in seen:
                continue
            seen.add(oid)
            uniq.append(oid)
        return uniq
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to load pending notifications: %s", e)
        return []


def _save_pending_ids(order_ids: list[int]) -> None:
    try:
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "order_ids": order_ids,
        }
        _PENDING_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to save pending notifications: %s", e)


def enqueue_pending(order_id: int) -> None:
    """Queue a confirmed order_id for retry delivery (so nothing is lost)."""
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return
    ids = _load_pending_ids()
    if oid in ids:
        return
    ids.append(oid)
    _save_pending_ids(ids)


def dequeue_pending(order_id: int) -> None:
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return
    ids = [x for x in _load_pending_ids() if x != oid]
    _save_pending_ids(ids)


def _take_markup(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Взять в работу",
                    callback_data=f"take:{order_id}",
                )
            ]
        ]
    )


def _caption_from_order(order: dict) -> str:
    number = str(order.get("number") or "")
    created_at = str(order.get("created_at") or "")
    author = order.get("author_username") or ""
    codes_total = sum(int(i.get("quantity") or 0) for i in (order.get("items") or []))
    lines = [f"Новая заявка № {number}"]
    if author:
        lines.append(f"Создал: @{author}")
    lines.append(f"Количество кодов: {codes_total}")
    if created_at:
        lines.append(f"Дата: {created_at[:19].replace('T', ' ')}")
    comment = order.get("comment")
    if comment:
        lines.append(f"Комментарий: {comment}")
    return "\n".join(lines)


@dataclass
class NotifyResult:
    delivered_any: bool
    delivered_to: int
    failed_to: int


async def notify_order_to_admins(bot, order: dict) -> NotifyResult:
    """Try to deliver order card to admins (document, fallback to text)."""
    order_id = int(order["id"])
    order_number = str(order.get("number") or "")
    caption = _caption_from_order(order)
    markup = _take_markup(order_id)
    extras = list(order.get("extra_attachments") or [])

    try:
        admin_ids = await admin_telegram_ids_for_notify()
    except Exception as e:  # noqa: BLE001
        logger.warning("admin_telegram_ids_for_notify failed: %s", e)
        admin_ids = []

    delivered_any = False
    delivered_to = 0
    failed_to = 0

    # Try to prepare the MarkZnak excel once (shared for all admins).
    excel_bytes: bytes | None = None
    filename: str | None = None
    try:
        excel_bytes = await get_markznak_order_excel(order_id)
        filename = get_markznak_download_filename(order_number) if order_number else "order_markznak.xlsx"
    except Exception as e:  # noqa: BLE001
        logger.exception("get_markznak_order_excel failed (order_id=%s): %s", order_id, e)
        excel_bytes = None

    for admin_id in admin_ids:
        try:
            if excel_bytes and filename:
                doc = BufferedInputFile(excel_bytes, filename=filename)
                sent = await bot.send_document(
                    chat_id=admin_id,
                    document=doc,
                    caption=caption,
                    reply_markup=markup,
                )
                msg_id = sent.message_id
            else:
                sent = await bot.send_message(
                    chat_id=admin_id,
                    text=caption,
                    reply_markup=markup,
                )
                msg_id = sent.message_id

            delivered_any = True
            delivered_to += 1
            notifications_registry.add(
                order_id=order_id,
                chat_id=sent.chat.id,
                message_id=msg_id,
                is_document=bool(getattr(sent, "document", None)),
                file_id=(sent.document.file_id if getattr(sent, "document", None) else None),
            )
            try:
                await register_order_telegram_posting(order_id, sent.chat.id, msg_id)
            except Exception as reg_err:  # noqa: BLE001
                logger.warning(
                    "register_order_telegram_posting order=%s admin_id=%s: %s",
                    order_id,
                    admin_id,
                    reg_err,
                )

            # Send extra attachments grouped (docs album, photos album).
            try:
                await _send_extras_as_albums(bot, chat_id=admin_id, extras=extras)
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "Send extras albums failed admin_id=%s order_id=%s: %s",
                    admin_id,
                    order_id,
                    e,
                )
        except Exception as e:  # noqa: BLE001
            failed_to += 1
            # Fallback: if document send failed, try text.
            try:
                sent2 = await bot.send_message(
                    chat_id=admin_id,
                    text=caption,
                    reply_markup=markup,
                )
                delivered_any = True
                delivered_to += 1
                notifications_registry.add(
                    order_id=order_id,
                    chat_id=sent2.chat.id,
                    message_id=sent2.message_id,
                    is_document=False,
                    file_id=None,
                )
                try:
                    await register_order_telegram_posting(order_id, sent2.chat.id, sent2.message_id)
                except Exception as reg_err:  # noqa: BLE001
                    logger.warning(
                        "register_order_telegram_posting (fallback) order=%s admin_id=%s: %s",
                        order_id,
                        admin_id,
                        reg_err,
                    )

                # Even in fallback mode, still try to send extras grouped.
                try:
                    await _send_extras_as_albums(bot, chat_id=admin_id, extras=extras)
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "Send extras albums failed (fallback) admin_id=%s order_id=%s: %s",
                        admin_id,
                        order_id,
                        e,
                    )
            except Exception as e2:  # noqa: BLE001
                logger.exception("Notify admin failed admin_id=%s order_id=%s: %s / %s", admin_id, order_id, e, e2)

    return NotifyResult(delivered_any=delivered_any, delivered_to=delivered_to, failed_to=failed_to)


async def retry_loop(bot, *, interval_sec: int = 60) -> None:
    """Periodically retry notifications for confirmed orders from local pending queue."""
    while True:
        try:
            pending = _load_pending_ids()
            if not pending:
                await asyncio.sleep(interval_sec)
                continue
            # Process a snapshot (avoid long lock if file changes)
            for oid in list(pending)[:50]:
                try:
                    order = await get_order(int(oid))
                except Exception as e:  # noqa: BLE001
                    logger.warning("retry_loop get_order(%s) failed: %s", oid, e)
                    continue
                if not order:
                    # Removed from backend => drop from queue
                    dequeue_pending(int(oid))
                    continue
                res = await notify_order_to_admins(bot, order)
                if res.delivered_any:
                    dequeue_pending(int(oid))
        except Exception as e:  # noqa: BLE001
            logger.exception("retry_loop cycle failed: %s", e)
        await asyncio.sleep(interval_sec)

