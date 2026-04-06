"""Database session and initialization."""
import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import StaticPool

from config import get_settings
from database.models import Base

settings = get_settings()
logger = logging.getLogger(__name__)


def _sqlite_ensure_orders_deleted_at(connection: Connection) -> None:
    """У старых SQLite-файлов нет колонки deleted_at; create_all её не добавляет."""
    try:
        exists = connection.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders' LIMIT 1"
            )
        ).first()
        if not exists:
            return
        info = connection.execute(text("PRAGMA table_info(orders)"))
        col_names = {row[1] for row in info}
        if "deleted_at" in col_names:
            return
        connection.execute(text("ALTER TABLE orders ADD COLUMN deleted_at DATETIME"))
        logger.info("SQLite: добавлена колонка orders.deleted_at")
    except Exception:
        logger.exception("SQLite: не удалось добавить orders.deleted_at")
        raise
    try:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_orders_deleted_at ON orders (deleted_at)"
            )
        )
    except Exception:
        logger.warning("SQLite: не удалось создать индекс ix_orders_deleted_at", exc_info=True)

db_url = (
    settings.TEST_DATABASE_URL
    if getattr(settings, "TEST_MODE", False) and settings.TEST_DATABASE_URL
    else settings.DATABASE_URL
)

if "sqlite" in db_url:
    engine = create_async_engine(
        db_url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
else:
    engine = create_async_engine(
        db_url,
        echo=False,
    )

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db():
    """Dependency for getting async database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """Создать недостающие таблицы и применить лёгкие миграции для старых SQLite-БД."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if "sqlite" in db_url:
            await conn.run_sync(_sqlite_ensure_orders_deleted_at)
