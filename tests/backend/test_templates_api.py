"""Tests for template-related API endpoints."""
import io

import pytest
from httpx import AsyncClient
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Product


@pytest.mark.asyncio
async def test_get_template_by_article_filters_products(client: AsyncClient, test_db_session: AsyncSession):
    # Prepare products for two different articles
    products = [
        Product(
            article="33708white",
            name="Джемпер женский, арт. 33708white, размер S",
            brand="Brand",
            color="white",
            tnved_code="6202400009",
            composition="100% хлопок",
            country="Россия",
            target_gender="ЖЕНСКИЙ",
            category="ДЖЕМПЕР",
            legal_entity="Акс Кэпитал",
            size="S",
        ),
        Product(
            article="33708black",
            name="Джемпер женский, арт. 33708black, размер M",
            brand="Brand",
            color="black",
            tnved_code="6202400009",
            composition="100% хлопок",
            country="Россия",
            target_gender="ЖЕНСКИЙ",
            category="ДЖЕМПЕР",
            legal_entity="Акс Кэпитал",
            size="M",
        ),
        Product(
            article="042purple",
            name="Куртка женская, арт. 042purple, размер L",
            brand="Brand",
            color="purple",
            tnved_code="6202400009",
            composition="100% полиэстер",
            country="Киргизия",
            target_gender="ЖЕНСКИЙ",
            category="КУРТКА",
            legal_entity="Акс Кэпитал",
            size="L",
        ),
    ]
    test_db_session.add_all(products)
    await test_db_session.commit()

    resp = await client.get("/products/template", params={"article": "33708"})
    assert resp.status_code == 200

    wb = load_workbook(io.BytesIO(resp.content))
    ws = wb.active

    # Header row of user template
    headers = [ws.cell(row=1, column=col).value for col in range(1, 10)]
    assert headers[:3] == ["Количество", "Артикул", "Размер"]

    # Data rows should only contain 33708* articles in user template
    articles = [ws.cell(row=row, column=2).value for row in range(2, ws.max_row + 1)]
    assert set(articles) == {"33708white", "33708black"}

    # Quantity column should be empty for all rows
    qty_values = [ws.cell(row=row, column=1).value for row in range(2, ws.max_row + 1)]
    assert all(v in (None, "", 0) for v in qty_values)


@pytest.mark.asyncio
async def test_create_order_from_template_happy_path(client: AsyncClient, test_db_session: AsyncSession):
    # Prepare products for one article with several sizes
    base_kwargs = dict(
        name="Джинсы женские, арт. 8069white",
        brand="Bronks",
        color="white",
        tnved_code="6204623100",
        composition="75% хлопок, 15% вискоза, 10% полиэстер",
        country="Киргизия",
        target_gender="ЖЕНСКИЙ",
        category="ДЖИНСЫ",
        legal_entity="Акс Кэпитал",
    )
    products = [
        Product(article="8069white", size="S", **base_kwargs),
        Product(article="8069white", size="M", **base_kwargs),
        Product(article="8069white", size="L", **base_kwargs),
    ]
    test_db_session.add_all(products)
    await test_db_session.commit()

    # Get user template from API
    resp = await client.get("/products/template", params={"article": "8069white"})
    assert resp.status_code == 200

    wb = load_workbook(io.BytesIO(resp.content))
    ws = wb.active

    # Fill quantities for first two rows in user template
    ws.cell(row=2, column=1, value=2)  # Количество for size S
    ws.cell(row=3, column=1, value=3)  # Количество for size M

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    files = {"file": ("template.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    data = {
        "author_telegram_id": "222",
        "author_username": "tester2",
        "author_full_name": "Tester Two",
        "order_type": "Ламода",
        "ms_order_number": "MS-TEMPLATE-1",
        "comment": "Заявка по шаблону",
    }

    resp2 = await client.post("/orders/from_template", data=data, files=files)
    assert resp2.status_code == 200
    order = resp2.json()

    assert len(order["items"]) == 2
    quantities = sorted(i["quantity"] for i in order["items"])
    assert quantities == [2, 3]
