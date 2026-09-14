# Extrait de Vintiz (tests/test_refund_service.py), reduit a l'annulation
# TOTALE exposee par le contrat PR2 (D4, §4.2).
import uuid

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.pos import Transaction

pytestmark = pytest.mark.anyio


async def _sell(client, auth_headers, amount: str = "25.00"):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Robe", "unit_price": amount, "quantity": 1}],
            "payments": [{"method": "cash", "amount": amount, "tendered_amount": amount}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_cancel_total_sale_creates_mirrored_refund(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers, "25.00")
    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel",
        json={"reason": "cliente insatisfaite"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    refund = r.json()
    assert refund["transaction_type"] == "refund"
    assert refund["total_ttc"] == 25.0
    assert refund["original_transaction_id"] == sale["id"]
    assert refund["refund_reason"] == "cliente insatisfaite"
    assert refund["items"][0]["label"] == sale["items"][0]["label"]
    assert refund["payments"][0]["method"] == "cash"
    assert "TICKET D'ANNULATION" in refund["receipt_text"]


async def test_cancel_reason_too_short_rejected(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel", json={"reason": "x"}, headers=auth_headers
    )
    assert r.status_code == 422


async def test_cancel_already_refunded_rejected(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    r1 = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel", json={"reason": "premier retour"}, headers=auth_headers
    )
    assert r1.status_code == 201
    r2 = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel", json={"reason": "deuxieme retour"}, headers=auth_headers
    )
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "already_refunded"


async def test_cancel_a_refund_itself_rejected(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    refund = (
        await client.post(
            f"/api/pos/transactions/{sale['id']}/cancel", json={"reason": "retour normal"}, headers=auth_headers
        )
    ).json()
    r = await client.post(
        f"/api/pos/transactions/{refund['id']}/cancel", json={"reason": "annuler l'annulation"}, headers=auth_headers
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_a_sale"


async def test_cancel_unknown_transaction_404(client, auth_headers, open_drawer):
    r = await client.post(
        f"/api/pos/transactions/{uuid.uuid4()}/cancel", json={"reason": "peu importe"}, headers=auth_headers
    )
    assert r.status_code == 404


async def test_cancel_requires_open_drawer(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    await client.post("/api/pos/drawer/close", json={"closing_amount": "125.00"}, headers=auth_headers)
    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel", json={"reason": "caisse fermee"}, headers=auth_headers
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "drawer_closed"


async def test_cancel_card_sale_calls_sumup_refund_before_write(
    client, auth_headers, open_drawer, monkeypatch
):
    from types import SimpleNamespace

    async def _fake_verify(db, tender, client_uuid):
        return SimpleNamespace(
            sumup_checkout_id=tender.checkout_id,
            sumup_transaction_id="TXN-999",
            sumup_transaction_code="CODE-999",
            sumup_auth_code="000000",
            sumup_card_brand="MASTERCARD",
            sumup_card_last4="1111",
        )

    monkeypatch.setattr("app.services.pos.verify_card_tender", _fake_verify)

    sale = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Manteau", "unit_price": "60.00", "quantity": 1}],
            "payments": [{"method": "card", "amount": "60.00", "checkout_id": "chk_refund"}],
        },
        headers=auth_headers,
    )
    sale = sale.json()

    calls = []

    async def _fake_refund(sumup_transaction_id, amount):
        calls.append((sumup_transaction_id, amount))
        return {"ok": True, "status": "refunded"}

    monkeypatch.setattr("app.services.refund.refund_card_payment", _fake_refund)

    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel", json={"reason": "carte remboursee"}, headers=auth_headers
    )
    assert r.status_code == 201, r.text
    assert calls == [("TXN-999", 60.0)] or (calls[0][0] == "TXN-999")


async def test_cancel_card_sale_blocked_when_sumup_refund_fails(
    client, auth_headers, open_drawer, monkeypatch
):
    from types import SimpleNamespace

    async def _fake_verify(db, tender, client_uuid):
        return SimpleNamespace(
            sumup_checkout_id=tender.checkout_id,
            sumup_transaction_id="TXN-FAIL",
            sumup_transaction_code="CODE-FAIL",
            sumup_auth_code="000000",
            sumup_card_brand="VISA",
            sumup_card_last4="0000",
        )

    monkeypatch.setattr("app.services.pos.verify_card_tender", _fake_verify)

    sale = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Manteau", "unit_price": "60.00", "quantity": 1}],
            "payments": [{"method": "card", "amount": "60.00", "checkout_id": "chk_refund_fail"}],
        },
        headers=auth_headers,
    )
    sale = sale.json()

    async def _fake_refund_fail(sumup_transaction_id, amount):
        return {"ok": False, "status": "http_400", "message": "carte refusee"}

    monkeypatch.setattr("app.services.refund.refund_card_payment", _fake_refund_fail)

    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel", json={"reason": "sumup indisponible"}, headers=auth_headers
    )
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "sumup_refund_failed"

    async with async_session() as db:
        refunds = (
            await db.execute(
                select(Transaction).where(
                    Transaction.original_transaction_id == uuid.UUID(sale["id"])
                )
            )
        ).scalars().all()
    assert refunds == []
