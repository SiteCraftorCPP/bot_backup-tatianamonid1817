"""One-off migration: add responsible fields to orders."""

from sqlalchemy import create_engine, inspect, text

from config import get_settings


def migrate() -> None:
    settings = get_settings()
    url = settings.DATABASE_URL
    url = url.replace("+aiosqlite", "").replace("+asyncpg", "")

    engine = create_engine(url)

    with engine.begin() as conn:
        inspector = inspect(conn)
        columns = {c["name"] for c in inspector.get_columns("orders")}
        statements: list[str] = []
        if "responsible_telegram_id" not in columns:
            statements.append(
                "ALTER TABLE orders ADD COLUMN responsible_telegram_id BIGINT"
            )
        if "responsible_username" not in columns:
            statements.append(
                "ALTER TABLE orders ADD COLUMN responsible_username VARCHAR(255)"
            )
        for sql in statements:
            conn.execute(text(sql))

    if statements:
        print("Migration completed. Executed:")
        for s in statements:
            print(" -", s)
    else:
        print("Nothing to do: columns already exist.")


if __name__ == "__main__":
    migrate()

