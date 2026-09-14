# Nouveau test (PR3b, migration 0004_receipts_printed) — complète
# `test_triggers.py` (qui couvre déjà `content`/`duplicate_count`, migration
# 0002, non modifié ici) en prouvant que la fonction réécrite du trigger
# `receipts` continue de refuser `content`, et autorise désormais aussi
# `printed_count`/`printed_at` en plus de `duplicate_count`.
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import async_session, engine
from app.models.receipt import Receipt

pytestmark = pytest.mark.anyio


async def _sell(client, auth_headers, amount: str = "10.00"):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Article", "unit_price": amount, "quantity": 1}],
            "payments": [{"method": "cash", "amount": amount, "tendered_amount": amount}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_receipt_update_content_still_rejected_after_0004(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE receipts SET content = 'falsifie' WHERE transaction_id = :id"),
                {"id": sale["id"]},
            )


async def test_receipt_update_printed_count_is_allowed(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE receipts SET printed_count = 3 WHERE transaction_id = :id"),
            {"id": sale["id"]},
        )
    async with async_session() as db:
        receipt = (
            await db.execute(select(Receipt).where(Receipt.transaction_id == uuid.UUID(sale["id"])))
        ).scalar_one()
        assert receipt.printed_count == 3


async def test_receipt_update_printed_at_is_allowed(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE receipts SET printed_at = :printed_at WHERE transaction_id = :id"),
            {"printed_at": now, "id": sale["id"]},
        )
    async with async_session() as db:
        receipt = (
            await db.execute(select(Receipt).where(Receipt.transaction_id == uuid.UUID(sale["id"])))
        ).scalar_one()
        assert receipt.printed_at is not None


async def test_receipt_default_printed_count_is_zero(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    async with async_session() as db:
        receipt = (
            await db.execute(select(Receipt).where(Receipt.transaction_id == uuid.UUID(sale["id"])))
        ).scalar_one()
        assert receipt.printed_count == 0
        assert receipt.printed_at is None
