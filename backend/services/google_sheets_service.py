"""Google Sheets integration for registry."""
import os
import time
from typing import Optional, Any, Dict, Tuple

try:
    import gspread
    from google.oauth2.service_account import Credentials
    HAS_GS = True
except ImportError:
    HAS_GS = False


class GoogleSheetsService:
    """Service for writing to Google Sheets registry."""
    
    def __init__(self):
        self._client = None
        # Простой кэш в памяти для дорогих операций с таблицей
        # ключ -> (timestamp, value)
        self._cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}
        # TTL для кэша (в секундах). 60–120 сек достаточно, чтобы
        # разгрузить Google API и не мешать онлайновой работе.
        self._ttl_seconds: int = 90
    
    def _get_client(self):
        if not HAS_GS:
            return None
        if self._client is not None:
            return self._client
        from config import get_settings
        settings = get_settings()
        creds_path = settings.GOOGLE_CREDENTIALS_FILE
        if not os.path.exists(creds_path):
            return None
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        self._client = gspread.authorize(creds)
        return self._client

    # --- Cache helpers ---

    def _cache_get(self, key: Tuple[str, str]) -> Optional[Any]:
        """Получить значение из кэша, если не протухло."""
        now = time.time()
        item = self._cache.get(key)
        if not item:
            return None
        ts, value = item
        if now - ts > self._ttl_seconds:
            # TTL истёк
            self._cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: Tuple[str, str], value: Any) -> None:
        """Записать значение в кэш."""
        self._cache[key] = (time.time(), value)

    def _cache_invalidate_prefix(self, prefix: str) -> None:
        """Инвалидировать все ключи, начинающиеся с prefix."""
        keys_to_delete = [k for k in self._cache.keys() if k[0].startswith(prefix)]
        for k in keys_to_delete:
            self._cache.pop(k, None)
    
    def append_order_to_registry(
        self,
        order_number: str,
        created_at: str,
        author: str,
        items_summary: str,
        status: str,
        author_telegram_id: Optional[int] = None,
        author_username: Optional[str] = None,
        excel_link: Optional[str] = None,
        yandex_link: Optional[str] = None,
    ) -> bool:
        """Append order row to registry sheet."""
        client = self._get_client()
        if not client:
            return False
        try:
            from config import get_settings
            settings = get_settings()
            spreadsheet_id = settings.TEST_SPREADSHEET_ID or settings.SPREADSHEET_ID

            # Кэшируем получение листа и, при необходимости, создание заголовка.
            cache_key = ("registry_sheet", spreadsheet_id)
            sheet = self._cache_get(cache_key)
            if sheet is None:
                spreadsheet = client.open_by_key(spreadsheet_id)
                try:
                    sheet = spreadsheet.worksheet(settings.REGISTRY_SHEET_NAME)
                except gspread.WorksheetNotFound:
                    sheet = spreadsheet.add_worksheet(
                        title=settings.REGISTRY_SHEET_NAME,
                        rows=1000,
                        cols=20,
                    )
                    # Header row: Номер, Дата/время, ФИО, Telegram username, Telegram ID,
                    # Позиции, Статус, ссылка на файлы (Excel/Яндекс).
                    sheet.append_row(
                        [
                            "Номер",
                            "Дата/время",
                            "ФИО",
                            "Telegram username",
                            "Telegram ID",
                            "Артикул/количество",
                            "Статус",
                            "Ссылка на Excel",
                            "Ссылка на Яндекс.Диск",
                        ]
                    )
                self._cache_set(cache_key, sheet)
            
            tg_username = f"@{author_username}" if author_username else ""
            tg_id = str(author_telegram_id) if author_telegram_id is not None else ""

            # Определяем формат существующего листа по заголовкам.
            headers_key = ("registry_headers", spreadsheet_id)
            headers = self._cache_get(headers_key)
            if headers is None:
                headers = sheet.row_values(1)
                self._cache_set(headers_key, headers)

            has_extended_headers = any(
                h in headers
                for h in (
                    "Telegram username",
                    "Telegram ID",
                    "Ссылка на Excel",
                    "Ссылка на Яндекс.Диск",
                )
            )

            if has_extended_headers:
                # Новый формат: отдельные колонки для username / ID / ссылок.
                # Чтобы ссылка не оказывалась "через одну ячейку" от статуса,
                # пишем ссылку Яндекс.Диска и в колонку "Ссылка на Excel", и в
                # колонку "Ссылка на Яндекс.Диск".
                row = [
                    order_number,
                    created_at,
                    author,
                    tg_username,
                    tg_id,
                    items_summary,
                    status,
                    yandex_link or excel_link or "",
                    yandex_link or "",
                ]
            else:
                # Старый формат: Номер, Дата/время, Автор, Артикул/количество, Статус.
                # Ничего лишнего не добавляем, чтобы не "съезжали" колонки.
                row = [
                    order_number,
                    created_at,
                    author,
                    items_summary,
                    status,
                ]
            sheet.append_row(row)
            return True
        except Exception:
            return False
    
    # Default column indices (for new structure): Статус=7, Яндекс=9
    # Для старых листов: Статус=5, Яндекс=7
    STATUS_COL = 7
    YANDEX_COL = 9

    def update_order_in_registry(
        self,
        order_number: str,
        status: Optional[str] = None,
        yandex_link: Optional[str] = None,
    ) -> bool:
        """Update order row (status, yandex link) in registry."""
        client = self._get_client()
        if not client:
            return False
        try:
            from config import get_settings
            settings = get_settings()
            spreadsheet_id = settings.TEST_SPREADSHEET_ID or settings.SPREADSHEET_ID
            spreadsheet = client.open_by_key(spreadsheet_id)
            sheet = spreadsheet.worksheet(settings.REGISTRY_SHEET_NAME)

            cache_key = ("registry_find", f"{spreadsheet_id}:{order_number}")
            cells = self._cache_get(cache_key)
            if cells is None:
                cells = sheet.findall(str(order_number))
                self._cache_set(cache_key, cells)
            if not cells:
                return False
            headers_key = ("registry_headers", spreadsheet_id)
            headers = self._cache_get(headers_key)
            if headers is None:
                headers = sheet.row_values(1)
                self._cache_set(headers_key, headers)
            status_col = self._col_index(headers, "Статус", self.STATUS_COL)
            yandex_col = self._col_index(headers, "Ссылка на Яндекс.Диск", self.YANDEX_COL)
            excel_col = self._col_index(headers, "Ссылка на Excel", self.STATUS_COL + 1)
            for cell in cells:
                row_idx = cell.row
                if status is not None:
                    sheet.update_cell(row_idx, status_col, status)
                if yandex_link is not None:
                    # Синхронизируем ссылку и в колонке Excel, и в колонке Яндекс,
                    # чтобы после статуса не оставалась пустая ячейка.
                    sheet.update_cell(row_idx, excel_col, yandex_link)
                    sheet.update_cell(row_idx, yandex_col, yandex_link)
                break
            return True
        except Exception:
            return False

    def _col_index(self, headers: list, name: str, default: int) -> int:
        """Get 1-based column index by header name, fallback to default."""
        try:
            idx = headers.index(name)
            return idx + 1
        except (ValueError, AttributeError):
            return default


sheets_service = GoogleSheetsService()
