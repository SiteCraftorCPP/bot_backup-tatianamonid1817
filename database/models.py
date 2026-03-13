"""Database models for Честный знак bot."""
from datetime import datetime, timedelta, timezone
from enum import Enum as PyEnum
from sqlalchemy import String, Integer, BigInteger, ForeignKey, Text, DateTime, Enum, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UserRole(str, PyEnum):
    USER = "user"
    ADMIN = "admin"


class OrderStatus(str, PyEnum):
    CREATED = "создана"
    IN_PROGRESS = "в работе"
    READY = "готово"
    SENT = "отправлена"


class ProductCategory(str, PyEnum):
    CLOTHING = "одежда"
    SHOES = "обувь"


TZ_UTC3 = timezone(timedelta(hours=3))


class User(Base):
    __tablename__ = "users"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default=UserRole.USER.value)
    department: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(TZ_UTC3))
    
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="author")


class Product(Base):
    __tablename__ = "products"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gtin: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    article: Mapped[str] = mapped_column(String(100), nullable=False, index=True)  # Тип КМ / артикул
    name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    color: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tnved_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    composition: Mapped[str | None] = mapped_column(String(500), nullable=True)
    country: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_gender: Mapped[str | None] = mapped_column(String(50), nullable=True)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)  # КУРТКА, ДЖИНСЫ, etc
    legal_entity: Mapped[str | None] = mapped_column(String(200), nullable=True)
    variant: Mapped[str | None] = mapped_column(String(100), nullable=True)  # Вид товара (артикул+цвет)
    size: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extended_status: Mapped[str | None] = mapped_column(String(200), nullable=True)
    signed: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=True, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(TZ_UTC3))
    
    order_items: Mapped[list["OrderItem"]] = relationship("OrderItem", back_populates="product")


class Order(Base):
    __tablename__ = "orders"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    author_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=OrderStatus.CREATED.value)
    order_type: Mapped[str | None] = mapped_column(String(50), nullable=True)  # Ламода, ОЗ/ВБ, Киргизия
    ms_order_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    yandex_link: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    responsible_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    responsible_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(TZ_UTC3))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(TZ_UTC3), onupdate=lambda: datetime.now(TZ_UTC3))
    
    author: Mapped["User"] = relationship("User", back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("products.id"), nullable=True)
    size: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Для нового товара — данные без product_id
    article: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    color: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tnved_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    legal_entity: Mapped[str | None] = mapped_column(String(200), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    composition: Mapped[str | None] = mapped_column(String(500), nullable=True)
    country: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_gender: Mapped[str | None] = mapped_column(String(50), nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    
    order: Mapped["Order"] = relationship("Order", back_populates="items")
    product: Mapped["Product | None"] = relationship("Product", back_populates="order_items")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(TZ_UTC3))
