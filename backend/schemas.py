"""Pydantic schemas for API."""
from datetime import datetime
from typing import Optional, Literal

from pydantic import BaseModel, Field


class ProductResponse(BaseModel):
    id: int
    gtin: Optional[str] = None
    article: str
    name: str
    brand: Optional[str] = None
    color: Optional[str] = None
    tnved_code: Optional[str] = None
    composition: Optional[str] = None
    country: Optional[str] = None
    target_gender: Optional[str] = None
    category: Optional[str] = None
    legal_entity: Optional[str] = None
    variant: Optional[str] = None
    size: Optional[str] = None

    model_config = {"from_attributes": True}


class OrderItemCreate(BaseModel):
    product_id: Optional[int] = None
    size: str
    quantity: int = 1
    article: Optional[str] = None
    name: Optional[str] = None
    color: Optional[str] = None
    tnved_code: Optional[str] = None
    legal_entity: Optional[str] = None
    brand: Optional[str] = None
    composition: Optional[str] = None
    country: Optional[str] = None
    target_gender: Optional[str] = None
    category: Optional[str] = None


class OrderCreate(BaseModel):
    author_telegram_id: int
    author_username: Optional[str] = None
    author_full_name: Optional[str] = None
    order_type: Optional[str] = None  # Ламода, ОЗ/ВБ, Киргизия
    ms_order_number: Optional[str] = None
    comment: Optional[str] = None
    items: list[OrderItemCreate]


class OrderAttachmentCreate(BaseModel):
    telegram_file_id: str
    file_name: Optional[str] = None


class OrderAttachmentResponse(BaseModel):
    id: int
    telegram_file_id: str
    file_name: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class OrderTelegramPostingCreate(BaseModel):
    chat_id: int
    message_id: int


class OrderTelegramPostingResponse(BaseModel):
    chat_id: int
    message_id: int

    model_config = {"from_attributes": True}


class OrderItemResponse(BaseModel):
    id: int
    product_id: Optional[int] = None
    size: str
    quantity: int
    article: Optional[str] = None
    name: Optional[str] = None
    color: Optional[str] = None
    tnved_code: Optional[str] = None
    legal_entity: Optional[str] = None
    brand: Optional[str] = None
    composition: Optional[str] = None

    model_config = {"from_attributes": True}


class OrderResponse(BaseModel):
    id: int
    number: str
    author_id: int
    author_telegram_id: Optional[int] = None
    author_username: Optional[str] = None
    author_full_name: Optional[str] = None
    status: str
    order_type: Optional[str] = None
    ms_order_number: Optional[str] = None
    comment: Optional[str] = None
    yandex_link: Optional[str] = None
    responsible_telegram_id: Optional[int] = None
    responsible_username: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None
    items: list[OrderItemResponse] = []
    extra_attachments: list[OrderAttachmentResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class OrderUpdate(BaseModel):
    status: Optional[str] = None
    yandex_link: Optional[str] = None
    responsible_telegram_id: Optional[int] = None
    responsible_username: Optional[str] = None
    comment: Optional[str] = None


class OrderListResponse(BaseModel):
    id: int
    number: str
    status: str
    created_at: datetime
    author_username: Optional[str] = None
    responsible_username: Optional[str] = None
    items_count: int = 0
    deleted_at: Optional[datetime] = None


class PurgeTrashRequest(BaseModel):
    """Пустой список или None — удалить все заявки в корзине."""

    ids: Optional[list[int]] = None


class UserUpsert(BaseModel):
    telegram_id: int
    username: Optional[str] = None
    full_name: Optional[str] = None
    role: Literal["user", "admin"]


class UserResponse(BaseModel):
    id: int
    telegram_id: int
    username: Optional[str] = None
    full_name: Optional[str] = None
    role: str
    created_at: datetime

    model_config = {"from_attributes": True}
