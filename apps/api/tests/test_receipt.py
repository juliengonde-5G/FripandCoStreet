# Extrait de l'application source (tests/test_receipt_refund.py), adapte au ticket PR2
# (§4.4 du contrat) — mention D14, pas de "conforme NF525", duplicate_count.
import uuid

import pytest

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


async def test_receipt_text_returned_on_sale_response(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    text = sale["receipt_text"]
    assert "Ticket #1" in text
    assert "Total TTC:" in text
    assert "25.00 EUR" in text
    assert "conforme NF525" not in text.lower()
    assert "auto-attestation" in text.lower()


async def test_receipt_endpoint_returns_same_text_and_increments_duplicate(
    client, auth_headers, open_drawer
):
    sale = await _sell(client, auth_headers)
    r1 = await client.get(f"/api/pos/transactions/{sale['id']}/receipt", headers=auth_headers)
    assert r1.status_code == 200
    assert r1.json()["text"] == sale["receipt_text"]
    assert r1.json()["duplicate_count"] == 1

    r2 = await client.get(f"/api/pos/transactions/{sale['id']}/receipt", headers=auth_headers)
    assert r2.json()["duplicate_count"] == 2


async def test_receipt_unknown_transaction_404(client, auth_headers):
    r = await client.get(f"/api/pos/transactions/{uuid.uuid4()}/receipt", headers=auth_headers)
    assert r.status_code == 404


async def test_refund_receipt_mentions_original_ticket(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel", json={"reason": "essai"}, headers=auth_headers
    )
    refund_text = r.json()["receipt_text"]
    assert "TICKET D'ANNULATION" in refund_text
    assert f"Annule le ticket n° {sale['transaction_number']}" in refund_text
    assert "essai" in refund_text


async def test_receipt_shows_vat_number_when_set(client, auth_headers, open_drawer):
    r = await client.put(
        "/api/admin/settings/shop",
        json={"name": "Frip & Co Street", "siret": "12345678901234", "vat_number": "FR12345678901"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    sale = await _sell(client, auth_headers)
    assert "N° TVA : FR12345678901" in sale["receipt_text"]


async def test_receipt_omits_vat_number_line_when_not_set(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    assert "N° TVA" not in sale["receipt_text"]


async def test_discount_appears_on_receipt(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Robe", "unit_price": "50.00", "quantity": 1}],
            "discount": {"type": "amount", "value": "5.00"},
            "payments": [{"method": "cash", "amount": "45.00", "tendered_amount": "45.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    text = r.json()["receipt_text"]
    assert "-5.00 EUR" in text
