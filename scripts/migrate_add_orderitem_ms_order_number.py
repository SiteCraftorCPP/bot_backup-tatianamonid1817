"""One-off migration: add ms_order_number column to order_items table."""

from sqlalchemy import create_engine, inspect, text

from config import get_settings


def migrate() -> None:
    settings = get_settings()
    url = settings.DATABASE_URL
    url = url.replace("+aiosqlite", "").replace("+asyncpg", "")

    engine = create_engine(url)

    with engine.begin() as conn:
        inspector = inspect(conn)
        columns = {c["name"] for c in inspector.get_columns("order_items")}
        if "ms_order_number" in columns:
            print("Nothing to do: order_items.ms_order_number already exists.")
            return
        conn.execute(
            text("ALTER TABLE order_items ADD COLUMN ms_order_number VARCHAR(100)")
        )
        print("Migration completed: added ms_order_number to order_items.")


if __name__ == "__main__":
    migrate()
