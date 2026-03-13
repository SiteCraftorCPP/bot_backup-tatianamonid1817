"""One-off migration: add payment / status columns to products table.

Работает как с SQLite, так и с Postgres, использует существующий async engine.
"""

from sqlalchemy import create_engine, inspect, text

from config import get_settings


def migrate() -> None:
    settings = get_settings()
    url = settings.DATABASE_URL
    # Для async-драйверов (sqlite+aiosqlite / postgresql+asyncpg) используем sync-аналог.
    url = url.replace("+aiosqlite", "").replace("+asyncpg", "")

    engine = create_engine(url)

    with engine.begin() as conn:
        inspector = inspect(conn)
        columns = {c["name"] for c in inspector.get_columns("products")}

        statements: list[str] = []

        if "payment_method" not in columns:
            statements.append(
                "ALTER TABLE products ADD COLUMN payment_method VARCHAR(100)"
            )
        if "status" not in columns:
            statements.append("ALTER TABLE products ADD COLUMN status VARCHAR(100)")
        if "extended_status" not in columns:
            statements.append(
                "ALTER TABLE products ADD COLUMN extended_status VARCHAR(200)"
            )
        if "signed" not in columns:
            statements.append("ALTER TABLE products ADD COLUMN signed VARCHAR(50)")

        if not statements:
            print("Nothing to do: all columns already exist.")
            return

        for sql in statements:
            conn.execute(text(sql))

        print("Migration completed. Executed statements:")
        for sql in statements:
            print(" -", sql)


if __name__ == "__main__":
    migrate()

