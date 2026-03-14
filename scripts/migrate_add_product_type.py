"""Миграция: добавить колонку product_type в таблицу products."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text
from config import get_settings


def migrate() -> None:
    settings = get_settings()
    url = settings.DATABASE_URL.replace("+aiosqlite", "").replace("+asyncpg", "")
    engine = create_engine(url)

    with engine.begin() as conn:
        inspector = inspect(conn)
        columns = {c["name"] for c in inspector.get_columns("products")}
        if "product_type" in columns:
            print("Nothing to do: product_type already exists.")
            return
        conn.execute(
            text("ALTER TABLE products ADD COLUMN product_type VARCHAR(20)")
        )
        print("Migration completed: added product_type to products.")


if __name__ == "__main__":
    migrate()
