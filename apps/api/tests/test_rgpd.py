# Nouveau test (PR3, §7 ARCHITECTURE_PR3.md, E4) — anonymisation RGPD :
# PII effacées, AUCUNE ligne supprimée, AUCUNE vente modifiée (hors
# `client_id`, deja inchange ici), `verify_chain_integrity` toujours
# valide, contact retiré de la liste Brevo dédiée (appel vérifié par mock).
from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import async_session
from app.models.client import Client, Consent, ConsentSource
from app.services import brevo_contacts
from app.services.client_service import ClientService
from app.services.fiscal import FiscalService

pytestmark = pytest.mark.anyio


async def _sell(client, auth_headers, amount: str = "10.00") -> dict:
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


async def test_anonymize_service_erases_pii_but_keeps_the_row():
    async with async_session() as db:
        c = Client(email="a-effacer@example.com", first_name="Alice", last_name="Martin", newsletter_optin=True)
        db.add(c)
        await db.commit()
        await db.refresh(c)
        client_id = c.id

    async with async_session() as db:
        client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one()
        await ClientService(db).anonymize(client=client, user_id=None, reason="Demande cliente")
        await db.commit()

    async with async_session() as db:
        refreshed = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one()
        assert refreshed.first_name is None
        assert refreshed.last_name is None
        assert refreshed.newsletter_optin is False
        assert refreshed.anonymized_at is not None
        assert refreshed.email.endswith("@anonyme.invalid")
        assert refreshed.email != "a-effacer@example.com"

        consents = (
            await db.execute(
                select(Consent).where(Consent.client_id == client_id, Consent.source == ConsentSource.rgpd)
            )
        ).scalars().all()
        assert len(consents) == 1
        assert consents[0].granted is False


async def test_anonymize_is_idempotent():
    async with async_session() as db:
        c = Client(email="deja-anonyme@example.com")
        db.add(c)
        await db.commit()
        await db.refresh(c)
        client_id = c.id

    async with async_session() as db:
        client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one()
        await ClientService(db).anonymize(client=client, user_id=None, reason="r1")
        await db.commit()

    async with async_session() as db:
        client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one()
        first_email = client.email
        await ClientService(db).anonymize(client=client, user_id=None, reason="r2 — deja fait")
        await db.commit()

    async with async_session() as db:
        refreshed = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one()
        assert refreshed.email == first_email  # pas re-anonymise une seconde fois


async def test_anonymize_keeps_transactions_intact_and_chain_valid(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    attach = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "vente-conservee@example.com", "send_receipt": False},
        headers=auth_headers,
    )
    client_id = attach.json()["client"]["id"]

    r = await client.post(
        f"/api/admin/clients/{client_id}/anonymize",
        json={"reason": "Demande RGPD art. 17"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    from app.models.pos import Transaction

    async with async_session() as db:
        tx = (
            await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))
        ).scalar_one()
        # La vente n'est ni supprimée ni modifiée hors client_id (déjà posé
        # avant l'anonymisation, inchangé par elle) — même total, même hash.
        assert tx.client_id == uuid.UUID(client_id)
        assert float(tx.total_ttc) == 10.0
        assert tx.hash_chain

        result = await FiscalService(db).verify_chain_integrity()
        assert result["valid"] is True


async def test_admin_anonymize_endpoint_removes_contact_from_brevo_list(
    client, auth_headers, open_drawer, monkeypatch
):
    monkeypatch.setattr(settings, "BREVO_API_KEY", "ak-test")
    monkeypatch.setattr(settings, "BREVO_LIST_ID", "77")

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": request.content})
        return httpx.Response(204)

    monkeypatch.setattr(brevo_contacts, "_transport", httpx.MockTransport(handler))

    sale = await _sell(client, auth_headers)
    attach = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "a-retirer@example.com", "send_receipt": False},
        headers=auth_headers,
    )
    client_id = attach.json()["client"]["id"]

    r = await client.post(
        f"/api/admin/clients/{client_id}/anonymize",
        json={"reason": "Demande RGPD"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.brevo.com/v3/contacts/lists/77/contacts/remove"
    import json as _json

    assert _json.loads(calls[0]["body"]) == {"emails": ["a-retirer@example.com"]}

    # Jamais de DELETE /v3/contacts ni de blocklist — un seul appel, celui
    # du retrait de liste.
    async with async_session() as db:
        refreshed = (await db.execute(select(Client).where(Client.id == uuid.UUID(client_id)))).scalar_one()
        assert refreshed.anonymized_at is not None


async def test_export_client_data_contains_tickets_and_consents(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    attach = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "export-rgpd@example.com", "newsletter_optin": True, "send_receipt": False},
        headers=auth_headers,
    )
    client_id = attach.json()["client"]["id"]

    r = await client.get(f"/api/admin/clients/{client_id}/export", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client"]["email"] == "export-rgpd@example.com"
    assert len(body["consents"]) == 1
    assert len(body["tickets"]) == 1
    assert "Ticket #" in body["tickets"][0]["content"] or str(sale["transaction_number"]) in body["tickets"][0]["content"]
