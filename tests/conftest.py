"""Test infrastructure: FastAPI app, test DB, HTTP client, and fake Google Sheets."""
from collections import namedtuple
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.main import app
from database.models import Base
from database.session import get_db
from backend.services import google_sheets_service


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """Async engine bound to in-memory SQLite for tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def test_db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Provide a database session bound to the test engine."""
    session_maker = async_sessionmaker(
        test_engine,
        expire_on_commit=False,
        class_=AsyncSession,
        autoflush=False,
        autocommit=False,
    )
    async with session_maker() as session:
        try:
            yield session
        finally:
            await session.rollback()


class FakeSheetsService:
    """In-memory fake for GoogleSheetsService used in tests."""

    def __init__(self) -> None:
        Call = namedtuple("Call", ["args", "kwargs"])
        self.append_calls: list[Call] = []
        self.update_calls: list[Call] = []

    # Signature mirrors GoogleSheetsService.append_order_to_registry
    def append_order_to_registry(
        self,
        order_number: str,
        created_at: str,
        author: str,
        items_summary: str,
        status: str,
        excel_link: str | None = None,
        yandex_link: str | None = None,
        author_telegram_id: int | None = None,
        author_username: str | None = None,
    ) -> bool:
        self.append_calls.append(
            (order_number, created_at, author, items_summary, status, excel_link, yandex_link, author_telegram_id, author_username)
        )
        return True

    # Signature mirrors GoogleSheetsService.update_order_in_registry
    def update_order_in_registry(
        self,
        order_number: str,
        status: str | None = None,
        yandex_link: str | None = None,
    ) -> bool:
        self.update_calls.append((order_number, status, yandex_link))
        return True


@pytest.fixture(autouse=True)
def fake_sheets(monkeypatch) -> FakeSheetsService:
    """Automatically replace real Google Sheets service with in-memory fake."""
    fake = FakeSheetsService()
    # Patch instance in google_sheets_service module
    monkeypatch.setattr(google_sheets_service, "sheets_service", fake)
    # Patch reference imported in orders routes
    from backend.routes import orders
    monkeypatch.setattr(orders, "sheets_service", fake)
    return fake


@pytest_asyncio.fixture
async def client(test_db_session) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client bound to FastAPI app with overridden DB dependency."""

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield test_db_session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c

    app.dependency_overrides.clear()
