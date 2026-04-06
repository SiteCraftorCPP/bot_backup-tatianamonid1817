-- Мягкое удаление заявок (корзина). Выполнить один раз на существующей БД.
-- SQLite:
ALTER TABLE orders ADD COLUMN deleted_at DATETIME;
CREATE INDEX IF NOT EXISTS ix_orders_deleted_at ON orders (deleted_at);

-- PostgreSQL (пример):
-- ALTER TABLE orders ADD COLUMN deleted_at TIMESTAMP WITH TIME ZONE;
-- CREATE INDEX IF NOT EXISTS ix_orders_deleted_at ON orders (deleted_at);
