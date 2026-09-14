# Nouveau test (§4.3/§9 du contrat) : Z scelle a la creation (D9), cumuls
# perpetuels, completude (count == transaction_count), montants de caisse
# figes dans le payload signe.
import uuid

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.pos import ZReport
from app.services.fiscal import FiscalService

pytestmark = pytest.mark.anyio


async def _sell(client, auth_headers, amount: str):
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


async def test_close_drawer_creates_sealed_z_report(client, auth_headers, open_drawer):
    await _sell(client, auth_headers, "20.00")
    await _sell(client, auth_headers, "15.00")

    r = await client.post("/api/pos/drawer/close", json={"closing_amount": "135.00"}, headers=auth_headers)
    assert r.status_code == 200
    z = r.json()
    assert z["report_number"] == 1
    assert z["transaction_count"] == 2
    assert z["total_sales"] == 35.0
    assert z["total_refunds"] == 0.0
    assert z["counted"] is True
    assert len(z["hash"]) == 64
    assert z["previous_hash"] == "0"
    assert z["cumulative_sales"] == 35.0
    assert z["cumulative_transaction_count"] == 2


async def test_z_chain_links_previous_hash_and_cumulates(client, auth_headers):
    # Premiere session
    await client.post("/api/pos/drawer/open", json={"opening_amount": "0"}, headers=auth_headers)
    await _sell(client, auth_headers, "10.00")
    z1 = (
        await client.post("/api/pos/drawer/close", json={"closing_amount": "10.00"}, headers=auth_headers)
    ).json()

    # Deuxieme session
    await client.post("/api/pos/drawer/open", json={"opening_amount": "0"}, headers=auth_headers)
    await _sell(client, auth_headers, "5.00")
    z2 = (
        await client.post("/api/pos/drawer/close", json={"closing_amount": "5.00"}, headers=auth_headers)
    ).json()

    assert z2["previous_hash"] == z1["hash"]
    assert z2["cumulative_sales"] == 15.0
    assert z2["cumulative_transaction_count"] == 2


async def test_z_report_completeness_matches_transaction_count(client, auth_headers, open_drawer):
    await _sell(client, auth_headers, "10.00")
    await client.post("/api/pos/drawer/close", json={"closing_amount": "110.00"}, headers=auth_headers)

    async with async_session() as db:
        result = await FiscalService(db).verify_z_chain_integrity()
    assert result["valid"] is True


async def test_z_report_hash_commits_to_cash_amounts(client, auth_headers, open_drawer):
    """Le hash engage les montants de caisse (D9) : recalculer le payload
    avec un `closing_amount` different produit un hash different de celui
    stocke — la falsification serait detectee par `verify_z_chain_integrity`
    meme si le trigger ne bloquait pas l'UPDATE (defense en profondeur)."""
    await _sell(client, auth_headers, "10.00")
    z = (
        await client.post("/api/pos/drawer/close", json={"closing_amount": "110.00"}, headers=auth_headers)
    ).json()

    async with async_session() as db:
        report = (await db.execute(select(ZReport).where(ZReport.id == uuid.UUID(z["id"])))).scalar_one()
        assert float(report.closing_amount) == 110.0
        report.closing_amount = 999999.0  # mutation en memoire seulement, jamais flushee
        tampered_payload_hash = FiscalService._hmac(
            {
                "signature_version": report.fiscal_signature_version,
                "previous_hash": report.previous_hash,
                "report_number": report.report_number,
                "closing_amount": FiscalService._money(report.closing_amount),
            }
        )
    assert tampered_payload_hash != z["hash"]


async def test_regularization_preview_and_create(client, auth_headers, open_drawer):
    from datetime import datetime, timedelta, timezone

    await _sell(client, auth_headers, "10.00")
    await client.post("/api/pos/drawer/close", json={"closing_amount": "110.00"}, headers=auth_headers)

    # Aucune orpheline attendue : tout est deja couvert par la session close.
    now = datetime.now(timezone.utc)
    r = await client.get(
        "/api/pos/z-reports/regularization/preview",
        params={
            "period_from": (now - timedelta(hours=1)).isoformat(),
            "period_to": (now + timedelta(hours=1)).isoformat(),
        },
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["orphan_count"] == 0
    assert r.json()["can_regularize"] is False
