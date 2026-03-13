"""One-off migration: add category column to order_items table."""

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
        if "category" in columns:
            print("Nothing to do: category column already exists.")
            return
        conn.execute(text("ALTER TABLE order_items ADD COLUMN category VARCHAR(100)"))
        print("Migration completed: added category column to order_items.")


if __name__ == "__main__":
    migrate()

