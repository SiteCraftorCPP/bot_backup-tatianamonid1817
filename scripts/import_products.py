"""Import products from Google Sheets or CSV into database."""
import asyncio
import csv
import logging
import os
import sys

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import AsyncSessionLocal, init_db
from database.models import Product


logger = logging.getLogger(__name__)


# Fallback column mapping для текущей структуры Google Sheets
# См. лист «Одежда по всем ЮЛ»:
# A – пусто, B – GTIN, C – Количество, D – Тип КМ, E – Способ выпуска,
# F – Артикул, G – Размер, H – Дата создания, I – Наименование товара,
# J – Набор, K – Бренд, L – Вид товара, M – Код ТН ВЭД, N – Артикул (дубль),
# O – Страна производства, P – Цвет, Q – Размер (дубль), R – Тип текстиля,
# S – Возраст потребителя, T – Состав, U – Целевой пол, V – Статус, W – Расширенный статус,
# X – Подписан, Y – Группа ТН ВЭД, Z – Тип продукта, AA–AC сертификаты/декларации,
# AD – Код в системе, AE – Юр. лицо, AF – Номер заказа МС.
SHEETS_COLUMNS = {
    "gtin": 1,          # B: GTIN
    "article": 5,       # F: Артикул
    "name": 8,          # I: Наименование товара
    "brand": 10,        # K: Бренд
    "variant": 11,      # L: Вид товара
    "tnved_code": 12,   # M: Код ТН ВЭД
    "country": 14,         # O: Страна производства
    "color": 15,           # P: Цвет
    "size": 6,             # G: Размер
    "composition": 19,     # T: Состав
    "target_gender": 20,   # U: Целевой пол
    "category": 11,        # L: Вид товара
    "legal_entity": 30,    # AE: Юр. лицо
    "status": 21,          # V: Статус
    "extended_status": 22, # W: Расширенный статус
    "signed": 23,          # X: Подписан
}

# Для обувного листа структура отличается: размер и способ оплаты находятся
# в других столбцах. Вводим отдельный маппинг.
SHOES_SHEETS_COLUMNS = dict(SHEETS_COLUMNS)
# Столбец R (0-based индекс 17) — размер обуви в текущей таблице.
SHOES_SHEETS_COLUMNS["size"] = 17
# Столбец G (0-based индекс 6) — Способ оплаты.
SHOES_SHEETS_COLUMNS["payment_method"] = 6


def find_column(header: list[str], *names: str) -> int | None:
    """Find column index by header names (case-insensitive, partial match)."""
    for name in names:
        n = name.lower()
        for i, h in enumerate(header):
            h = str(h).strip().lower()
            if not h:
                continue
            if n in h or h in n:
                return i
    return None


def build_csv_mapping(header: list[str]) -> dict[str, int | None]:
    """Build column mapping from CSV header for Одежда/Обувь formats."""
    return {
        "gtin": find_column(header, "GTIN"),
        # Артикул берём только из колонок с явным названием «Артикул»,
        # не путая с «Тип КМ» и дублями.
        "article": find_column(header, "Артикул"),
        "name": find_column(header, "Наименование товара"),
        "brand": find_column(header, "Бренд"),
        "variant": find_column(header, "Вид товара", "Вид обуви"),
        "tnved_code": find_column(header, "Код ТН ВЭД"),
        "country": find_column(header, "Страна производства"),
        "color": find_column(header, "Цвет"),
        "size": find_column(header, "Размер"),
        "composition": find_column(header, "Состав"),
        "target_gender": find_column(header, "Целевой пол"),
        "category": find_column(header, "Вид товара", "Вид обуви", "Набор"),
        "legal_entity": find_column(header, "Юр. лицо"),
        "payment_method": find_column(header, "Способ оплаты"),
        "status": find_column(header, "Статус"),
        "extended_status": find_column(header, "Расширенный статус"),
        "signed": find_column(header, "Подписан"),
    }


def parse_row(row: list[str], mapping: dict[str, int | None] | None = None) -> dict:
    """Parse a row into product dict. Uses mapping if provided, else SHEETS_COLUMNS."""
    cols = mapping if mapping is not None else SHEETS_COLUMNS

    def get(key: str) -> str | None:
        idx = cols.get(key)
        if idx is not None and idx is not False and 0 <= idx < len(row) and row[idx]:
            return str(row[idx]).strip()
        return None

    return {
        "gtin": get("gtin"),
        "article": get("article") or get("variant") or "",
        "name": get("name") or "",
        "brand": get("brand"),
        "variant": get("variant"),
        "tnved_code": get("tnved_code"),
        "country": get("country"),
        "color": get("color"),
        "size": get("size"),
        "composition": get("composition"),
        "target_gender": get("target_gender"),
        "category": get("category"),
        "legal_entity": get("legal_entity"),
        "payment_method": get("payment_method"),
        "status": get("status"),
        "extended_status": get("extended_status"),
        "signed": get("signed"),
    }


async def import_from_csv(csv_path: str, mapping: dict[str, int | None] | None = None) -> int:
    """Import products from CSV (export from Google Sheets)."""
    await init_db()
    count = 0
    async with AsyncSessionLocal() as session:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)

        if not rows:
            return 0

        header = rows[0]
        col_map = mapping if mapping is not None else build_csv_mapping(header)
        if col_map.get("name") is None and mapping is None:
            col_map = SHEETS_COLUMNS  # fallback to legacy mapping

        # Если в CSV есть несколько колонок «Размер», берём последнюю (как в обувных таблицах,
        # где первый «Размер» может быть пустым, а второй — фактический размер пары).
        if mapping is None:
            size_indices = [
                i
                for i, h in enumerate(header)
                if "размер" in str(h).strip().lower()
            ]
            if size_indices:
                col_map["size"] = max(size_indices)

        # Колонки материалов для обуви (материал верха / подкладки / низа).
        material_upper_idx = find_column(header, "Материал верха")
        material_lining_idx = find_column(header, "Материал подкладки")
        material_sole_idx = find_column(header, "Материал низа", "Материал низа / подошвы")

        for row in rows[1:]:
            if len(row) < 8:
                continue
            data = parse_row(row, col_map)
            if not data["name"]:
                continue

            # Если это обувь (ТН ВЭД 64...) и в таблице есть отдельные колонки материалов,
            # собираем их в composition в формате, который дальше разбирает шаблон для админа.
            tnved = data.get("tnved_code") or ""
            if isinstance(tnved, str) and tnved.startswith("64"):
                parts: list[str] = []
                if (
                    material_upper_idx is not None
                    and 0 <= material_upper_idx < len(row)
                    and row[material_upper_idx]
                ):
                    parts.append(f"Материал верха: {str(row[material_upper_idx]).strip()}")
                if (
                    material_lining_idx is not None
                    and 0 <= material_lining_idx < len(row)
                    and row[material_lining_idx]
                ):
                    parts.append(
                        f"Материал подкладки: {str(row[material_lining_idx]).strip()}"
                    )
                if (
                    material_sole_idx is not None
                    and 0 <= material_sole_idx < len(row)
                    and row[material_sole_idx]
                ):
                    parts.append(
                        f"Материал низа / подошвы: {str(row[material_sole_idx]).strip()}"
                    )
                if parts:
                    data["composition"] = "; ".join(parts)

            # Идентификация товара:
            # 1) по GTIN, если он есть;
            # 2) иначе по паре (article, size), чтобы не склеивать разные товары
            #    с общим артикулом, но разными размерами;
            # 3) в крайнем случае по name.
            if data["gtin"]:
                stmt = select(Product).where(Product.gtin == data["gtin"])
            elif data["article"] and data["size"]:
                stmt = select(Product).where(
                    Product.article == data["article"],
                    Product.size == data["size"],
                )
            else:
                stmt = select(Product).where(Product.name == data["name"])
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                # Обновляем недостающие поля (включая размер) для уже существующих товаров.
                updated = False
                for field in (
                    "size",
                    "tnved_code",
                    "country",
                    "color",
                    "composition",
                    "target_gender",
                    "category",
                    "brand",
                    "legal_entity",
                    "payment_method",
                    "status",
                    "extended_status",
                    "signed",
                ):
                    old_val = getattr(existing, field, None)
                    new_val = data.get(field)
                    # Для composition обуви (ТН ВЭД 64...) разрешаем перезапись,
                    # чтобы материалы и статусы попали в шаблон админа.
                    if field == "composition":
                        tnved = data.get("tnved_code") or ""
                        if (
                            isinstance(tnved, str)
                            and tnved.startswith("64")
                            and new_val
                            and "Материал верха" in str(new_val)
                        ):
                            setattr(existing, field, new_val)
                            updated = True
                        continue

                    if (old_val is None or old_val == "") and new_val:
                        setattr(existing, field, new_val)
                        updated = True
                if updated:
                    existing.is_active = True
                    session.add(existing)
                continue

            data["is_active"] = True
            product = Product(**data)
            session.add(product)
            count += 1

        await session.commit()
    return count


async def import_from_sheets() -> int:
    """Import products from Google Sheets via API."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("Install gspread and google-auth: pip install gspread google-auth")
        return 0
    
    from config import get_settings
    settings = get_settings()
    creds_path = settings.GOOGLE_CREDENTIALS_FILE
    if not os.path.exists(creds_path):
        print(f"Credentials file not found: {creds_path}")
        return 0
    
    await init_db()
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(settings.SPREADSHEET_ID)

    # Импортируем сразу два листа: «Одежда по всем ЮЛ» и «Обувь по всем ЮЛ».
    sheet_titles = ["Одежда по всем ЮЛ", "Обувь по всем ЮЛ"]
    total_count = 0

    async with AsyncSessionLocal() as session:
        # Перед полной синхронизацией считаем все товары неактивными;
        # те, что пришли из актуальных листов, пометим is_active=True.
        logger.info("Google Sheets sync: deactivating all products before import")
        await session.execute(update(Product).values(is_active=False))

        for title in sheet_titles:
            try:
                sheet = spreadsheet.worksheet(title)
            except gspread.WorksheetNotFound:
                continue
            rows = sheet.get_all_values()
            if not rows or len(rows) < 2:
                logger.info(
                    "Google Sheets sync: worksheet '%s' is empty or has no data rows",
                    title,
                )
                continue

            header = rows[0]
            # Строим маппинг по заголовкам и для одежды, и для обуви.
            # Для старых форматов листов используем fallback-карты.
            col_map = build_csv_mapping(header)
            if col_map.get("name") is None:
                if title.lower().startswith("обувь"):
                    col_map = SHOES_SHEETS_COLUMNS
                else:
                    col_map = SHEETS_COLUMNS

            # Колонки материалов для обуви (материал верха / подкладки / низа)
            # при импорте напрямую из Google Sheets.
            material_upper_idx = find_column(header, "Материал верха")
            material_lining_idx = find_column(header, "Материал подкладки")
            material_sole_idx = find_column(
                header, "Материал низа", "Материал низа / подошвы"
            )

            sheet_created = 0
            sheet_updated = 0
            sheet_total = 0
            sheet_skipped_short = 0
            sheet_skipped_no_name = 0
            sheet_existing = 0
            sheet_debug_logged = 0

            for idx, row in enumerate(rows[1:], start=2):  # Skip header, track row index
                sheet_total += 1
                if len(row) < 15:
                    sheet_skipped_short += 1
                    continue
                data = parse_row(row, col_map)
                if not data["name"]:
                    sheet_skipped_no_name += 1
                    # Для первых нескольких строк без наименования логируем диагностическую информацию.
                    if sheet_skipped_no_name <= 5:
                        logger.info(
                            "Google Sheets sync: row without name in '%s' (row=%s): first_cells=%s",
                            title,
                            idx,
                            row[:10],
                        )
                    continue

                # Диагностический лог для первых ~20 строк обувного листа и всех строк,
                # где встречаются артикулы вроде F1150 (по артикулу, имени или GTIN).
                is_f1150_like = any(
                    v and "f1150" in str(v).lower()
                    for v in (data.get("article"), data.get("name"), data.get("gtin"))
                )
                if title.lower().startswith("обувь") and (
                    sheet_debug_logged < 20 or is_f1150_like
                ):
                    logger.info(
                        "Google Sheets sync: row '%s' (row=%s): article=%s, name=%s, tnved=%s, gtin=%s",
                        title,
                        idx,
                        data.get("article"),
                        data.get("name"),
                        data.get("tnved_code"),
                        data.get("gtin"),
                    )
                    sheet_debug_logged += 1

                # Проверяем, есть ли уже такой товар в БД.
                # Возможны дубликаты по GTIN или article+name, поэтому берём просто первый найденный.
                if data["gtin"]:
                    stmt = select(Product).where(Product.gtin == data["gtin"])
                else:
                    stmt = select(Product).where(Product.name == data["name"])
                result = await session.execute(stmt)
                existing = result.scalars().first()
                if existing:
                    sheet_existing += 1


                # Если это обувь (ТН ВЭД 64...) и в таблице есть отдельные колонки материалов,
                # собираем их в composition в формате, который дальше разбирает шаблон для админа.
                tnved = data.get("tnved_code") or ""
                if (
                    isinstance(tnved, str)
                    and tnved.startswith("64")
                    and any(
                        idx is not None
                        for idx in (
                            material_upper_idx,
                            material_lining_idx,
                            material_sole_idx,
                        )
                    )
                ):
                    parts: list[str] = []
                    if (
                        material_upper_idx is not None
                        and 0 <= material_upper_idx < len(row)
                        and row[material_upper_idx]
                    ):
                        parts.append(
                            f"Материал верха: {str(row[material_upper_idx]).strip()}"
                        )
                    if (
                        material_lining_idx is not None
                        and 0 <= material_lining_idx < len(row)
                        and row[material_lining_idx]
                    ):
                        parts.append(
                            f"Материал подкладки: {str(row[material_lining_idx]).strip()}"
                        )
                    if (
                        material_sole_idx is not None
                        and 0 <= material_sole_idx < len(row)
                        and row[material_sole_idx]
                    ):
                        parts.append(
                            f"Материал низа / подошвы: {str(row[material_sole_idx]).strip()}"
                        )
                    if parts:
                        data["composition"] = "; ".join(parts)

                if existing:
                    # Обновляем недостающие поля (включая размер) для уже существующих товаров.
                    updated = False
                    for field in (
                        "size",
                        "tnved_code",
                        "country",
                        "color",
                        "composition",
                        "target_gender",
                        "category",
                        "brand",
                        "legal_entity",
                        "payment_method",
                        "status",
                        "extended_status",
                        "signed",
                    ):
                        old_val = getattr(existing, field, None)
                        new_val = data.get(field)
                        # Для composition обуви (ТН ВЭД 64...) разрешаем перезапись,
                        # чтобы материалы попали в шаблон админа и пользовательский обувной шаблон.
                        if field == "composition":
                            tnved_local = data.get("tnved_code") or ""
                            if (
                                isinstance(tnved_local, str)
                                and tnved_local.startswith("64")
                                and new_val
                                and "Материал верха" in str(new_val)
                            ):
                                setattr(existing, field, new_val)
                                updated = True
                            continue

                        if (old_val is None or old_val == "") and new_val:
                            setattr(existing, field, new_val)
                            updated = True

                    # В любом случае, раз товар есть в актуальном листе — считаем его активным.
                    existing.is_active = True
                    session.add(existing)
                    if updated:
                        sheet_updated += 1
                    continue  # уже есть хотя бы одна запись — только обновили недостающие поля

                data["is_active"] = True
                product = Product(**data)
                session.add(product)
                sheet_created += 1
                total_count += 1

            logger.info(
                "Google Sheets sync: worksheet '%s' processed: total_rows=%s, "
                "skipped_short=%s, skipped_no_name=%s, matched_existing=%s, "
                "created=%s, updated=%s",
                title,
                sheet_total,
                sheet_skipped_short,
                sheet_skipped_no_name,
                sheet_existing,
                sheet_created,
                sheet_updated,
            )

        await session.commit()
    logger.info("Google Sheets sync: finished, total new products=%s", total_count)
    return total_count


if __name__ == "__main__":
    # Если скрипт запускается напрямую, настраиваем базовое логирование,
    # чтобы INFO-сообщения синхронизации были видны в консоли.
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        )

    if len(sys.argv) > 1:
        total = 0
        for path in sys.argv[1:]:
            if os.path.exists(path):
                n = asyncio.run(import_from_csv(path))
                total += n
                print(f"Imported {n} products from {path}")
            else:
                print(f"File not found: {path}")
        if len(sys.argv) > 2:
            print(f"Total imported: {total}")
    else:
        n = asyncio.run(import_from_sheets())
        print(f"Imported {n} products from Google Sheets")
