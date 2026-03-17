"""Configuration from environment variables."""
from typing import ClassVar
from pydantic_settings import BaseSettings
from functools import lru_cache
from urllib.parse import quote


class Settings(BaseSettings):
    """Application settings."""
    
    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    WORK_CHAT_ID: str = ""  # Рабочий чат для заявок
    ADMIN_IDS: str = ""  # Comma-separated telegram IDs
    TELEGRAM_PROXY: str | None = None  # socks5://user:pass@host:port или host:port:user:pass
    
    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./chestny_znak.db"
    TEST_DATABASE_URL: str | None = None
    
    # Google Sheets
    GOOGLE_CREDENTIALS_FILE: str = "credentials.json"
    SPREADSHEET_ID: str = "13VpJ_vysyFBVTz6HiUCnWdAMKm1MQzdIRmqOjFCcUEM"
    REGISTRY_SHEET_NAME: str = "Реестр заявок"
    TEST_SPREADSHEET_ID: str | None = None
    
    # Backend
    BACKEND_URL: str = "http://localhost:8000"
    
    # Test mode
    TEST_MODE: bool = False
    TEST_TELEGRAM_BOT_TOKEN: str | None = None
    
    model_config = {"env_file": ".env", "extra": "ignore"}
    
    # Плейсхолдер в .env — не отправлять в рабочий чат (chat not found)
    WORK_CHAT_ID_PLACEHOLDER: ClassVar[str] = "-1001234567890"

    @property
    def admin_ids_list(self) -> list[int]:
        if not self.ADMIN_IDS:
            return []
        return [int(x.strip()) for x in self.ADMIN_IDS.split(",") if x.strip()]

    @property
    def work_chat_id_for_send(self) -> str | None:
        """Реальный ID рабочего чата или None (не отправлять)."""
        raw = (self.WORK_CHAT_ID or "").strip()
        if not raw or raw == self.WORK_CHAT_ID_PLACEHOLDER:
            return None
        return raw

    @property
    def telegram_proxy_url(self) -> str | None:
        """Нормализовать прокси для Telegram API.

        Поддерживаем:
        - socks5://user:pass@host:port (или http://...)
        - host:port:user:pass (удобный формат из панелей)
        """
        raw = (self.TELEGRAM_PROXY or "").strip()
        if not raw:
            return None
        if "://" in raw:
            return raw
        # host:port:user:pass
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            user_q = quote(user, safe="")
            pass_q = quote(password, safe="")
            return f"socks5://{user_q}:{pass_q}@{host}:{port}"
        return raw


@lru_cache
def get_settings() -> Settings:
    return Settings()
