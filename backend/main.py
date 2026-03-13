"""FastAPI Backend for Честный знак bot."""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database.session import init_db
from backend.routes import products, orders, users, stats
from scripts.import_products import import_from_sheets


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    async def products_sync_worker() -> None:
        """Background task: periodically import products from Google Sheets."""
        while True:
            try:
                imported = await import_from_sheets()
                logger.info("Google Sheets sync: imported %s new products", imported)
            except Exception:
                logger.exception("Google Sheets sync failed")
            await asyncio.sleep(600)  # 10 минут

    sync_task = asyncio.create_task(products_sync_worker())
    try:
        yield
    finally:
        sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await sync_task


app = FastAPI(title="Честный знак API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

app.include_router(products.router, prefix="/products", tags=["products"])
app.include_router(orders.router, prefix="/orders", tags=["orders"])
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(stats.router, prefix="/stats", tags=["stats"])


@app.get("/health")
async def health():
    return {"status": "ok"}
