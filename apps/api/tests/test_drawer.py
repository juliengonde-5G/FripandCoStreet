# Extrait de Vintiz (tests/test_drawer_open_guard.py), adapte a la caisse
# PR2 (§4.3 du contrat : mouvements especes, verrou partage vente/cloture D10).
import uuid

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.pos import Transaction

pytestmark = pytest.mark.anyio


async def test_open_drawer_then_reopen_is_rejected(client, auth_headers):
    r1 = await client.post("/api/pos/drawer/open", json={"opening_amount": "50.00"}, headers=auth_headers)
    assert r1.status_code == 200
    r2 = await client.post("/api/pos/drawer/open", json={"opening_amount": "50.00"}, headers=auth_headers)
    assert r2.status_code == 409
    assert r2.json()["code"] == "drawer_already_open"


async def test_close_drawer_without_open_one_is_rejected(client, auth_headers):
    r = await client.post("/api/pos/drawer/close", json={"closing_amount": "0.00"}, headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "no_open_drawer"


async def test_drawer_current_reports_closed_state(client, auth_headers):
    r = await client.get("/api/pos/drawer/current", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"open": False}


async def test_drawer_current_reports_open_totals(client, auth_headers, open_drawer):
    r = await client.get("/api/pos/drawer/current", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["open"] is True
    assert body["today"]["sales_count"] == 0
    assert body["today"]["cash_expected"] == 100.0


async def test_cash_movement_requires_open_drawer(client, auth_headers):
    r = await client.post(
        "/api/pos/cash-movements",
        json={"direction": "out", "amount": "20.00", "reason": "bank_deposit"},
        headers=auth_headers,
    )
    assert r.status_code == 409
    assert r.json()["code"] == "drawer_closed"


async def test_cash_movement_other_reason_requires_note(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/cash-movements",
        json={"direction": "in", "amount": "10.00", "reason": "other"},
        headers=auth_headers,
    )
    assert r.status_code == 422


async def test_cash_movement_in_and_out_affect_expected(client, auth_headers, open_drawer):
    r_in = await client.post(
        "/api/pos/cash-movements",
        json={"direction": "in", "amount": "20.00", "reason": "float_top_up"},
        headers=auth_headers,
    )
    assert r_in.status_code == 200
    r_out = await client.post(
        "/api/pos/cash-movements",
        json={"direction": "out", "amount": "5.00", "reason": "bank_deposit"},
        headers=auth_headers,
    )
    assert r_out.status_code == 200

    r = await client.get("/api/pos/drawer/current", headers=auth_headers)
    today = r.json()["today"]
    assert today["cash_in"] == 20.0
    assert today["cash_out"] == 5.0
    # opening 100 + in 20 - out 5 = 115 attendu (aucune vente).
    assert today["cash_expected"] == 115.0


async def test_list_cash_movements(client, auth_headers, open_drawer):
    await client.post(
        "/api/pos/cash-movements",
        json={"direction": "in", "amount": "20.00", "reason": "float_top_up"},
        headers=auth_headers,
    )
    r = await client.get(
        "/api/pos/cash-movements", params={"drawer_id": open_drawer["id"]}, headers=auth_headers
    )
    assert r.status_code == 200
    assert len(r.json()["movements"]) == 1


async def test_sales_and_refunds_change_cash_expected_d11(client, auth_headers, open_drawer):
    """D11 : attendu = ouverture + ventes especes - remboursements especes
    + entrees - sorties."""
    client_uuid = str(uuid.uuid4())
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": client_uuid,
            "items": [{"label": "Robe", "unit_price": "40.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "40.00", "tendered_amount": "40.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201
    tx_id = r.json()["id"]

    snapshot = await client.get("/api/pos/drawer/current", headers=auth_headers)
    assert snapshot.json()["today"]["cash_expected"] == 140.0  # 100 + 40

    cancel = await client.post(
        f"/api/pos/transactions/{tx_id}/cancel", json={"reason": "essai retour"}, headers=auth_headers
    )
    assert cancel.status_code == 201

    snapshot2 = await client.get("/api/pos/drawer/current", headers=auth_headers)
    # 100 + 40 (vente) - 40 (remboursement) = 100 : Vintiz neutralisait ce
    # remboursement (C-2) ; PR2 le prend en compte (D11).
    assert snapshot2.json()["today"]["cash_expected"] == 100.0


async def test_close_drawer_computes_discrepancy(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/drawer/close", json={"closing_amount": "90.00", "note": "manque 10"}, headers=auth_headers
    )
    assert r.status_code == 200
    z = r.json()
    assert z["expected_amount"] == 100.0
    assert z["closing_amount"] == 90.0
    assert z["discrepancy"] == -10.0

    async with async_session() as db:
        rows = (await db.execute(select(Transaction))).scalars().all()
    assert rows == []
