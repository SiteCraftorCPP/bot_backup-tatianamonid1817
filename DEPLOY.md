# Деплой на VPS через Git

После клонирования репозитория на сервер нужно один раз настроить окружение. Дальше обновление — `git pull` и перезапуск сервисов.

## 1. Очистка VPS и клонирование

```bash
# Удалить старую папку (если есть)
rm -rf /root/bot

# Клонировать репо (подставь свой репо)
git clone https://github.com/SiteCraftorCPP/bot_backup-tatianamonid1817.git /root/bot
cd /root/bot
```

## 2. Окружение и зависимости

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Файлы, которых нет в репо (создать вручную)

- **`.env`** — скопировать с текущего сервера или создать из `.env.example`, заполнить:
  - `TELEGRAM_BOT_TOKEN`
  - `WORK_CHAT_ID` (или оставить плейсхолдер `-1001234567890`, если не нужна отправка в чат)
  - `ADMIN_IDS`
  - `DATABASE_URL` (по умолчанию `sqlite+aiosqlite:///./chestny_znak.db`)
  - `GOOGLE_CREDENTIALS_FILE=credentials.json`
  - `SPREADSHEET_ID`
  - `BACKEND_URL=http://localhost:8000`

- **`credentials.json`** — ключи Google Service Account для таблиц. Положить в корень проекта (`/root/bot/credentials.json`).

- **БД** — либо пустая (см. ниже), либо восстановить бэкап `chestny_znak.db` в `/root/bot/`.

## 4. Инициализация БД (если с нуля)

```bash
source .venv/bin/activate
python scripts/init_db.py
# При необходимости миграции:
python scripts/migrate_add_product_type.py
python scripts/migrate_add_product_is_active.py
python scripts/migrate_add_product_payment_status.py
python scripts/migrate_add_orderitem_category.py
python scripts/migrate_add_order_responsible.py
```

## 5. Запуск

Скрипты из репо (исполняемые):

```bash
chmod +x run_backend.sh run_bot.sh
```

**Вариант А — вручную в двух терминалах**

```bash
./run_backend.sh   # порт 8000
./run_bot.sh       # в другом терминале
```

**Вариант Б — systemd (рекомендуется)**

Создать юниты, например:

- `/etc/systemd/system/chestny-backend.service`
- `/etc/systemd/system/chestny-bot.service`

(WorkingDirectory=/root/bot, ExecStart=/root/bot/.venv/bin/uvicorn … и /root/bot/.venv/bin/python -m bot.main), затем:

```bash
systemctl daemon-reload
systemctl enable chestny-backend chestny-bot
systemctl start chestny-backend chestny-bot
```

## 6. Обновление после git push

На VPS:

```bash
cd /root/bot
git pull
source .venv/bin/activate
pip install -r requirements.txt   # если менялся requirements.txt
# При появлении новых миграций — выполнить их (см. п. 4)
systemctl restart chestny-backend chestny-bot   # если используешь systemd
# или перезапустить вручную run_backend.sh и run_bot.sh
```

---

**Итого чего нет в Git и нужно на сервере:** `.env`, `credentials.json`, при необходимости бэкап `chestny_znak.db`. Всё остальное — из репозитория.
