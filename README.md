# Telegram-бот заявок «Честный знак»

Автоматизация создания заявок по маркировке «Честный знак».

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Заполните .env: TELEGRAM_BOT_TOKEN, WORK_CHAT_ID, ADMIN_IDS
```

## Инициализация БД

```bash
python scripts/init_db.py
```

## Импорт справочника товаров

Вариант 1 — из CSV (экспорт из Google Sheets):
```bash
python scripts/import_products.py data.csv
```

Вариант 2 — из Google Sheets API (нужен credentials.json):
```bash
python scripts/import_products.py
```

## Запуск

Backend (обязательно запустить первым):
```bash
./run_backend.sh
# или: uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

Bot (в отдельном терминале):
```bash
./run_bot.sh
# или: python -m bot.main
```

## Тесты

```bash
pytest tests/ -v
```
