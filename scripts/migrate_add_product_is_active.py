"""One-off migration: add is_active flag to products."""

from sqlalchemy import create_engine, inspect, text

from config import get_settings


def migrate() -> None:
    settings = get_settings()
    url = settings.DATABASE_URL
    url = url.replace("+aiosqlite", "").replace("+asyncpg", "")

    engine = create_engine(url)

    with engine.begin() as conn:
        inspector = inspect(conn)
        columns = {c["name"] for c in inspector.get_columns("products")}
        if "is_active" in columns:
            print("Nothing to do: is_active already exists.")
            return

        conn.execute(
            text("ALTER TABLE products ADD COLUMN is_active BOOLEAN DEFAULT 1")
        )
        print("Migration completed: added is_active to products.")


if __name__ == "__main__":
    migrate()

