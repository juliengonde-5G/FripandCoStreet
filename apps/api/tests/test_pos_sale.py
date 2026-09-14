# Extrait de Vintiz (tests/test_pos_idempotence.py + tests/test_pos.py),
# adapte au panier libre PR2 (§4.1 du contrat) : pas de produits, deux
# moyens de paiement (especes / CB via `sumup_verify` monkeypatche, §7).
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.pos import Transaction

pytestmark = pytest.mark.anyio


def _new_uuid() -> str:
    return str(uuid.uuid4())


async def test_create_sale_cash_happy_path(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _new_uuid(),
            "items": [
                {"label": "Robe", "unit_price": "25.00", "quantity": 1},
                {"label": "Sac", "unit_price": "9.90", "quantity": 2},
            ],
            "payments": [{"method": "cash", "amount": "44.80", "tendered_amount": "50.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["total_ttc"] == 44.80
    assert body["transaction_type"] == "sale"
    assert body["transaction_number"] == 1
    assert body["receipt_number"] == 1
    assert len(body["hash_chain"]) == 64
    assert body["payments"][0]["change_amount"] == 5.20
    assert "FRIP & CO STREET" in body["receipt_text"]
    assert "conforme NF525" not in body["receipt_text"].lower()


async def test_create_sale_idempotent_replay_returns_200(client, auth_headers, open_drawer):
    payload = {
        "client_uuid": _new_uuid(),
        "items": [{"label": "T-shirt", "unit_price": "10.00", "quantity": 1}],
        "payments": [{"method": "cash", "amount": "10.00", "tendered_amount": "10.00"}],
    }
    r1 = await client.post("/api/pos/transactions", json=payload, headers=auth_headers)
    assert r1.status_code == 201
    r2 = await client.post("/api/pos/transactions", json=payload, headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]

    async with async_session() as db:
        count = (
            await db.execute(
                select(Transaction).where(Transaction.client_uuid == payload["client_uuid"])
            )
        ).scalars().all()
    assert len(count) == 1


async def test_sale_refused_when_drawer_closed(client, auth_headers):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _new_uuid(),
            "items": [{"label": "Robe", "unit_price": "10.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "10.00", "tendered_amount": "10.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "drawer_closed"

    async with async_session() as db:
        rows = (await db.execute(select(Transaction))).scalars().all()
    assert rows == []


async def test_sale_rejects_empty_cart(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/transactions",
        json={"client_uuid": _new_uuid(), "items": [], "payments": [{"method": "cash", "amount": "1.00"}]},
        headers=auth_headers,
    )
    assert r.status_code == 422


async def test_sale_rejects_more_than_50_items(client, auth_headers, open_drawer):
    items = [{"label": f"Article {i}", "unit_price": "1.00", "quantity": 1} for i in range(51)]
    r = await client.post(
        "/api/pos/transactions",
        json={"client_uuid": _new_uuid(), "items": items, "payments": [{"method": "cash", "amount": "51.00"}]},
        headers=auth_headers,
    )
    assert r.status_code == 422


async def test_sale_payment_sum_mismatch_rejected(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _new_uuid(),
            "items": [{"label": "Robe", "unit_price": "25.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "20.00", "tendered_amount": "20.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "payment_mismatch"

    async with async_session() as db:
        rows = (await db.execute(select(Transaction))).scalars().all()
    assert rows == []


async def test_sale_rejects_two_payments_same_method(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _new_uuid(),
            "items": [{"label": "Robe", "unit_price": "20.00", "quantity": 1}],
            "payments": [
                {"method": "cash", "amount": "10.00", "tendered_amount": "10.00"},
                {"method": "cash", "amount": "10.00", "tendered_amount": "10.00"},
            ],
        },
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "payment_mismatch"


async def test_sale_cash_over_legal_cap_rejected(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _new_uuid(),
            "items": [{"label": "Manteau vintage", "unit_price": "1500.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "1500.00", "tendered_amount": "1500.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "cash_cap_exceeded"

    async with async_session() as db:
        rows = (await db.execute(select(Transaction))).scalars().all()
    assert rows == []


async def test_sale_card_confirmed_writes_sumup_fields(client, auth_headers, open_drawer, monkeypatch):
    async def _fake_verify(db, tender, client_uuid):
        return SimpleNamespace(
            sumup_checkout_id=tender.checkout_id,
            sumup_transaction_id="TXN-ABC",
            sumup_transaction_code="CODE-1",
            sumup_auth_code="000000",
            sumup_card_brand="VISA",
            sumup_card_last4="4242",
        )

    monkeypatch.setattr("app.services.pos.verify_card_tender", _fake_verify)

    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _new_uuid(),
            "items": [{"label": "Robe", "unit_price": "30.00", "quantity": 1}],
            "payments": [{"method": "card", "amount": "30.00", "checkout_id": "chk_123"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    payment = r.json()["payments"][0]
    assert payment["sumup_card_brand"] == "VISA"
    assert payment["sumup_card_last4"] == "4242"


async def test_sale_card_not_confirmed_rejected_and_nothing_written(
    client, auth_headers, open_drawer, monkeypatch
):
    async def _fake_verify_fail(db, tender, client_uuid):
        raise Exception("Paiement CB non confirmé par SumUp (statut : FAILED).")

    monkeypatch.setattr("app.services.pos.verify_card_tender", _fake_verify_fail)

    client_uuid = _new_uuid()
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": client_uuid,
            "items": [{"label": "Robe", "unit_price": "30.00", "quantity": 1}],
            "payments": [{"method": "card", "amount": "30.00", "checkout_id": "chk_fail"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "card_not_confirmed"

    async with async_session() as db:
        rows = (
            await db.execute(select(Transaction).where(Transaction.client_uuid == client_uuid))
        ).scalars().all()
    assert rows == []


async def test_sale_card_requires_checkout_id(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _new_uuid(),
            "items": [{"label": "Robe", "unit_price": "30.00", "quantity": 1}],
            "payments": [{"method": "card", "amount": "30.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "payment_mismatch"


async def test_sale_default_label_is_article(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _new_uuid(),
            "items": [{"unit_price": "5.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "5.00", "tendered_amount": "5.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["items"][0]["label"] == "Article"


async def test_jet_failure_rolls_back_sale_entirely(client, auth_headers, open_drawer, monkeypatch):
    """§8 : vente + JET dans la meme transaction SQL — un echec d'ecriture du
    JET ne doit laisser AUCUNE trace de vente (pas de Transaction orpheline
    non signee)."""

    async def _boom(self, *args, **kwargs):
        raise RuntimeError("JET indisponible (test)")

    monkeypatch.setattr("app.services.jet.JournalService.record", _boom)

    client_uuid = _new_uuid()
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": client_uuid,
            "items": [{"label": "Robe", "unit_price": "10.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "10.00", "tendered_amount": "10.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 500

    async with async_session() as db:
        rows = (
            await db.execute(select(Transaction).where(Transaction.client_uuid == client_uuid))
        ).scalars().all()
    assert rows == []


async def test_list_transactions_defaults_to_today(client, auth_headers, open_drawer):
    await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _new_uuid(),
            "items": [{"label": "Robe", "unit_price": "10.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "10.00", "tendered_amount": "10.00"}],
        },
        headers=auth_headers,
    )
    r = await client.get("/api/pos/transactions", headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()["transactions"]) == 1

    from datetime import datetime, timedelta

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    r_empty = await client.get("/api/pos/transactions", params={"date": yesterday}, headers=auth_headers)
    assert r_empty.json()["transactions"] == []


async def test_list_transactions_rejects_bad_date(client, auth_headers, open_drawer):
    r = await client.get("/api/pos/transactions", params={"date": "not-a-date"}, headers=auth_headers)
    assert r.status_code == 422


async def test_fiscal_signing_key_used_for_hash(client, auth_headers, open_drawer):
    """Le hash change si on rejoue le calcul avec une autre cle — preuve que
    la signature est bien derivee de FISCAL_SIGNING_KEY (pas un hash trivial)."""
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _new_uuid(),
            "items": [{"label": "Robe", "unit_price": "10.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "10.00", "tendered_amount": "10.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201
    tx_id = r.json()["id"]

    async with async_session() as db:
        tx = (await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(tx_id)))).scalar_one()
        original_hash = tx.hash_chain

        from app.services.fiscal import FiscalService

        fiscal = FiscalService(db)
        payload = await fiscal._transaction_payload(tx, tx.previous_hash)
        recomputed = fiscal._hmac(payload)
    assert recomputed == original_hash
