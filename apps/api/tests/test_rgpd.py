# Nouveau test (PR3, §7 ARCHITECTURE_PR3.md, E4) — anonymisation RGPD :
# PII effacées, AUCUNE ligne supprimée, AUCUNE vente modifiée (hors
# `client_id`, deja inchange ici), `verify_chain_integrity` toujours
# valide, contact retiré de la liste Brevo dédiée (appel vérifié par mock),
# communications déjà tracées masquées, AUCUNE donnée personnelle dans le
# JET (immuable) — revue RGPD.
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import async_session
from app.models.client import Client, Consent, ConsentSource
from app.models.communication import Communication
from app.models.jet import JournalEvent
from app.services import brevo_contacts
from app.services.client_service import ClientService, mask_email
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
    # L'attache (newsletter_optin=False par défaut) a déjà déclenché une
    # tentative de retrait best-effort côté `ClientService.sync_brevo` —
    # on isole ci-dessous l'appel propre à l'anonymisation elle-même.
    calls.clear()

    r = await client.post(
        f"/api/admin/clients/{client_id}/anonymize",
        json={"reason": "Demande RGPD"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.brevo.com/v3/contacts/lists/77/contacts/remove"
    assert json.loads(calls[0]["body"]) == {"emails": ["a-retirer@example.com"]}

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


# ---------------------------------------------------------------------------
# Revue RGPD (2) — anonymisation complète : `communications.recipient` est
# une table MUTABLE (contrairement au JET) mais garde l'e-mail en clair tant
# qu'elle n'est pas explicitement masquée par `anonymize`.
# ---------------------------------------------------------------------------


def test_mask_email_helper():
    assert mask_email("alice@example.com") == "a***@example.com"
    assert mask_email("") == "***"
    assert mask_email("pas-un-email") == "***"


async def test_anonymize_masks_communication_recipients(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    attach = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "masque-moi@example.com", "send_receipt": True},
        headers=auth_headers,
    )
    assert attach.status_code == 200, attach.text
    client_id = attach.json()["client"]["id"]

    async with async_session() as db:
        before = (
            await db.execute(
                select(Communication).where(Communication.client_id == uuid.UUID(client_id))
            )
        ).scalars().all()
        assert len(before) == 1
        assert before[0].recipient == "masque-moi@example.com"

    r = await client.post(
        f"/api/admin/clients/{client_id}/anonymize",
        json={"reason": "Demande RGPD art. 17"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    async with async_session() as db:
        after = (
            await db.execute(
                select(Communication).where(Communication.client_id == uuid.UUID(client_id))
            )
        ).scalars().all()
        assert len(after) == 1  # la ligne (preuve d'envoi) reste, seul le destinataire est masqué
        assert after[0].recipient == "m***@example.com"
        assert after[0].recipient != "masque-moi@example.com"


# ---------------------------------------------------------------------------
# Revue RGPD (1) — le JET est un journal IMMUABLE : aucune ligne ne doit
# porter d'e-mail ni de nom en clair, sur tout le cycle de vie d'un client
# (création, lien, envoi, webhook, anonymisation).
# ---------------------------------------------------------------------------


async def test_jet_payloads_never_contain_personal_data_across_full_scenario(
    client, auth_headers, open_drawer, monkeypatch
):
    email = "jet-doit-rester-propre@example.com"
    first_name = "PrenomSecretissime"
    last_name = "NomSecretissime"
    monkeypatch.setattr(settings, "BREVO_WEBHOOK_TOKEN", "wh-token-jeu-essai")

    # 1. création + 2. lien + 3. envoi du ticket (une seule route).
    sale = await _sell(client, auth_headers)
    attach = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "newsletter_optin": True,
            "send_receipt": True,
        },
        headers=auth_headers,
    )
    assert attach.status_code == 200, attach.text
    client_id = attach.json()["client"]["id"]

    # 4. webhook Brevo désabonne ce même e-mail (source `webhook`).
    webhook = await client.post(
        "/api/brevo/webhook",
        params={"token": "wh-token-jeu-essai"},
        json={"event": "unsubscribed", "email": email},
    )
    assert webhook.status_code == 200, webhook.text

    # 5. anonymisation.
    anonymize = await client.post(
        f"/api/admin/clients/{client_id}/anonymize",
        json={"reason": "Jeu d'essai RGPD — vérification JET"},
        headers=auth_headers,
    )
    assert anonymize.status_code == 200, anonymize.text

    async with async_session() as db:
        events = (await db.execute(select(JournalEvent))).scalars().all()
    assert len(events) >= 5  # au moins un évènement par étape ci-dessus

    offenders: list[tuple[str, dict]] = []
    for event in events:
        blob = json.dumps(event.payload or {}, ensure_ascii=False).lower()
        if email.lower() in blob or first_name.lower() in blob or last_name.lower() in blob:
            offenders.append((event.event_type, event.payload))
    assert not offenders, (
        "Donnée personnelle trouvée dans un payload JET (journal immuable) : "
        + repr(offenders)
    )
