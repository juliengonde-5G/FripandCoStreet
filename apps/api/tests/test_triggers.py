# Extrait de l'application source (structure de tests inspiree de la suite NF525), adapte
# aux 6 groupes de triggers de la migration 0002 (§2/§8 du contrat) :
# transactions ; transaction_items+payments ; z_reports ; cash_drawers ;
# cash_movements ; receipts. Chaque UPDATE/DELETE interdit doit lever une
# `DBAPIError` dont le message contient "NF525".
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import async_session, engine
from app.models.pos import CashDrawer, Transaction
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


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------


async def test_transactions_update_after_signature_rejected(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE transactions SET total_ttc = 999.99 WHERE id = :id"),
                {"id": sale["id"]},
            )


async def test_transactions_delete_after_signature_rejected(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM transactions WHERE id = :id"), {"id": sale["id"]})

    async with async_session() as db:
        assert (await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))).scalar_one()


# ---------------------------------------------------------------------------
# transaction_items / payments — immuables des que la transaction parente
# est signee (elle l'est toujours a ce stade : la vente est ecrite en une
# seule fois deja signee, cf. services/pos.py).
# ---------------------------------------------------------------------------


async def test_transaction_items_update_rejected(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    item_id = sale["items"][0]["id"]
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE transaction_items SET quantity = 99 WHERE id = :id"), {"id": item_id}
            )


async def test_transaction_items_delete_rejected(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    item_id = sale["items"][0]["id"]
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM transaction_items WHERE id = :id"), {"id": item_id})


async def test_transaction_items_insert_on_signed_transaction_rejected(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO transaction_items "
                    "(id, created_at, updated_at, transaction_id, label, quantity, "
                    "unit_price, discount_amount, line_total, tva_rate, line_ht, line_tva, position) "
                    "VALUES (gen_random_uuid(), now(), now(), :tx_id, 'Ajout frauduleux', 1, "
                    "5.00, 0, 5.00, 20.00, 4.17, 0.83, 99)"
                ),
                {"tx_id": sale["id"]},
            )


async def test_payments_update_rejected(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    payment_id = sale["payments"][0]["id"]
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE payments SET amount = 0.01 WHERE id = :id"), {"id": payment_id}
            )


async def test_payments_delete_rejected(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    payment_id = sale["payments"][0]["id"]
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM payments WHERE id = :id"), {"id": payment_id})


# ---------------------------------------------------------------------------
# z_reports — aucune exception
# ---------------------------------------------------------------------------


async def test_z_reports_update_rejected(client, auth_headers, open_drawer):
    await _sell(client, auth_headers)
    z = (
        await client.post("/api/pos/drawer/close", json={"closing_amount": "110.00"}, headers=auth_headers)
    ).json()
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE z_reports SET total_sales = 0 WHERE id = :id"), {"id": z["id"]}
            )


async def test_z_reports_delete_rejected(client, auth_headers, open_drawer):
    await _sell(client, auth_headers)
    z = (
        await client.post("/api/pos/drawer/close", json={"closing_amount": "110.00"}, headers=auth_headers)
    ).json()
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM z_reports WHERE id = :id"), {"id": z["id"]})


# ---------------------------------------------------------------------------
# cash_drawers — figes apres cloture (closed_at + z_report_id)
# ---------------------------------------------------------------------------


async def test_cash_drawer_update_before_close_is_allowed(client, auth_headers, open_drawer):
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE cash_drawers SET closing_note = 'note libre' WHERE id = :id"),
            {"id": open_drawer["id"]},
        )
    async with async_session() as db:
        drawer = (
            await db.execute(select(CashDrawer).where(CashDrawer.id == uuid.UUID(open_drawer["id"])))
        ).scalar_one()
        assert drawer.closing_note == "note libre"


async def test_cash_drawer_update_after_close_rejected(client, auth_headers, open_drawer):
    await client.post("/api/pos/drawer/close", json={"closing_amount": "100.00"}, headers=auth_headers)
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE cash_drawers SET closing_amount = 0 WHERE id = :id"),
                {"id": open_drawer["id"]},
            )


async def test_cash_drawer_delete_always_rejected(client, auth_headers, open_drawer):
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM cash_drawers WHERE id = :id"), {"id": open_drawer["id"]})


# ---------------------------------------------------------------------------
# cash_movements — append-only
# ---------------------------------------------------------------------------


async def test_cash_movement_update_rejected(client, auth_headers, open_drawer):
    movement = (
        await client.post(
            "/api/pos/cash-movements",
            json={"direction": "in", "amount": "10.00", "reason": "float_top_up"},
            headers=auth_headers,
        )
    ).json()
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE cash_movements SET amount = 0.01 WHERE id = :id"), {"id": movement["id"]}
            )


async def test_cash_movement_delete_rejected(client, auth_headers, open_drawer):
    movement = (
        await client.post(
            "/api/pos/cash-movements",
            json={"direction": "in", "amount": "10.00", "reason": "float_top_up"},
            headers=auth_headers,
        )
    ).json()
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM cash_movements WHERE id = :id"), {"id": movement["id"]})


# ---------------------------------------------------------------------------
# receipts — DELETE interdit, UPDATE limite a duplicate_count
# ---------------------------------------------------------------------------


async def test_receipt_delete_rejected(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM receipts WHERE transaction_id = :id"), {"id": sale["id"]}
            )


async def test_receipt_update_content_rejected(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE receipts SET content = 'falsifie' WHERE transaction_id = :id"),
                {"id": sale["id"]},
            )


async def test_receipt_update_duplicate_count_is_allowed(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE receipts SET duplicate_count = 5 WHERE transaction_id = :id"),
            {"id": sale["id"]},
        )
    async with async_session() as db:
        receipt = (
            await db.execute(select(Receipt).where(Receipt.transaction_id == uuid.UUID(sale["id"])))
        ).scalar_one()
        assert receipt.duplicate_count == 5
