from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class NotificationEntry:
    order_id: int
    chat_id: int
    message_id: int
    is_document: bool
    file_id: str | None = None


class NotificationRegistry:
    """Реестр уведомлений о заявках, отправленных администраторам.

    Хранится только в памяти процесса бота и используется,
    чтобы при взятии заявки в работу убрать кнопку у всех админов.
    """

    def __init__(self) -> None:
        self._by_order: Dict[int, List[NotificationEntry]] = {}

    def add(
        self,
        order_id: int,
        chat_id: int,
        message_id: int,
        *,
        is_document: bool,
        file_id: str | None = None,
    ) -> None:
        entries = self._by_order.setdefault(order_id, [])
        for e in entries:
            if e.chat_id == chat_id and e.message_id == message_id:
                return
        entries.append(
            NotificationEntry(
                order_id=order_id,
                chat_id=chat_id,
                message_id=message_id,
                is_document=is_document,
                file_id=file_id,
            )
        )

    def get_for_order(self, order_id: int) -> List[NotificationEntry]:
        return list(self._by_order.get(order_id, []))


# Глобальный инстанс сервиса-реестра
notifications_registry = NotificationRegistry()

