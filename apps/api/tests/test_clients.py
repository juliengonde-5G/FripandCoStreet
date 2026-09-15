# Nouveau test (PR3, §7 ARCHITECTURE_PR3.md) — rattachement client + ticket
# par e-mail au POS (§4.1), fiche admin (§4). Environnement de test sans
# BREVO_API_KEY/SMTP_* (cf. `.env`) : la passerelle e-mail retombe sur la
# simulation — assez pour verifier le cablage bout-en-bout (communication
# tracee, JET) sans dependre du reseau. `test_email_gateway.py` /
# `test_brevo_contacts.py` couvrent deja les appels HTTP mockes en detail.
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import async_session
from app.models.client import Client, Consent
from app.models.communication import Communication
from app.models.jet import JournalEvent
from app.services import brevo_contacts

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


# ---------------------------------------------------------------------------
# POST /pos/transactions/{id}/client
# ---------------------------------------------------------------------------


async def test_attach_client_creates_client_links_and_sends_receipt(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)

    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={
            "email": "  Cliente@Example.COM  ",
            "first_name": "Alice",
            "last_name": "Martin",
            "newsletter_optin": True,
            "send_receipt": True,
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Normalisation e-mail (minuscules, trim).
    assert body["client"]["email"] == "cliente@example.com"
    assert body["client"]["newsletter_optin"] is True
    assert body["receipt_email"]["status"] in {"sent", "simulated", "failed"}
    assert body["brevo"] is not None  # opt-in => tentative de synchro

    async with async_session() as db:
        from app.models.pos import Transaction

        tx = (
            await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))
        ).scalar_one()
        assert tx.client_id is not None

        comms = (
            await db.execute(
                select(Communication).where(Communication.transaction_id == tx.id)
            )
        ).scalars().all()
        assert len(comms) == 1
        assert comms[0].recipient == "cliente@example.com"

        jet_types = {
            e.event_type
            for e in (await db.execute(select(JournalEvent))).scalars().all()
        }
        assert "client.created" in jet_types
        assert "consent.granted" in jet_types
        assert "client.linked" in jet_types
        assert {"receipt.emailed", "receipt.email_failed"} & jet_types


async def test_attach_client_without_newsletter_optin_still_syncs_brevo_removal(
    client, auth_headers, open_drawer
):
    """Revue RGPD : un opt-out (ou un client jamais opt-in) déclenche quand
    même une tentative de retrait de la liste Brevo (best-effort, jamais
    bloquant) — `ClientService.sync_brevo` retire si `newsletter_optin` est
    faux, plutôt que de sauter la synchro. Sans `BREVO_API_KEY` en test, le
    résultat est `status: failed`, jamais `None`."""
    sale = await _sell(client, auth_headers)
    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "no-optin@example.com", "newsletter_optin": False},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["brevo"] == {"status": "failed"}


async def test_attach_client_send_receipt_false_skips_email(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "sans-ticket@example.com", "send_receipt": False},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["receipt_email"] is None


async def test_attach_client_invalid_email_returns_422(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "pas-un-email"},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_email"


async def test_attach_client_unknown_transaction_404(client, auth_headers):
    r = await client.post(
        f"/api/pos/transactions/{uuid.uuid4()}/client",
        json={"email": "x@y.fr"},
        headers=auth_headers,
    )
    assert r.status_code == 404


async def test_attach_client_already_linked_to_another_client_409(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    r1 = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "premiere@example.com"},
        headers=auth_headers,
    )
    assert r1.status_code == 200

    r2 = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "seconde@example.com"},
        headers=auth_headers,
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "client_already_linked"


async def test_attach_client_same_client_again_is_idempotent(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    r1 = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "revient@example.com", "newsletter_optin": True},
        headers=auth_headers,
    )
    assert r1.status_code == 200

    r2 = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "revient@example.com", "newsletter_optin": True, "send_receipt": False},
        headers=auth_headers,
    )
    assert r2.status_code == 200

    async with async_session() as db:
        consents = (
            await db.execute(
                select(Consent).join(Client).where(Client.email == "revient@example.com")
            )
        ).scalars().all()
        # Idempotence POS (§3) : un seul consentement newsletter=True écrit,
        # pas un par appel.
        assert len(consents) == 1


async def test_attach_client_sequential_repeat_is_fully_idempotent(client, auth_headers, open_drawer):
    """Revue robustesse (double-tap séquentiel) — un second
    `POST /pos/transactions/{id}/client` pour la MÊME vente et le MÊME
    client (email inchangé) ne doit ni renvoyer, ni journaliser, ni
    resynchroniser Brevo une seconde fois : `receipt_email` de la réponse
    reflète la communication déjà existante, sans en créer une nouvelle."""
    sale = await _sell(client, auth_headers)
    r1 = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "sequentiel@example.com", "newsletter_optin": True, "send_receipt": True},
        headers=auth_headers,
    )
    assert r1.status_code == 200, r1.text
    client_id = r1.json()["client"]["id"]
    first_receipt_email = r1.json()["receipt_email"]
    assert first_receipt_email is not None

    r2 = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "sequentiel@example.com", "newsletter_optin": True, "send_receipt": True},
        headers=auth_headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["client"]["id"] == client_id
    # Idempotent : la réponse reflète l'envoi déjà fait, elle n'en déclenche
    # pas un second — et `brevo` n'est pas re-tenté (revue robustesse §2).
    assert r2.json()["receipt_email"] == first_receipt_email
    assert r2.json()["brevo"] is None

    async with async_session() as db:
        comms = (
            await db.execute(
                select(Communication).where(Communication.transaction_id == uuid.UUID(sale["id"]))
            )
        ).scalars().all()
        assert len(comms) == 1  # un seul envoi malgré les deux appels

        linked_events = (
            await db.execute(select(JournalEvent).where(JournalEvent.event_type == "client.linked"))
        ).scalars().all()
        assert len(linked_events) == 1  # pas de second `client.linked`

        emailed_events = (
            await db.execute(select(JournalEvent).where(JournalEvent.event_type == "receipt.emailed"))
        ).scalars().all()
        assert len(emailed_events) == 1  # pas de second `receipt.emailed`


async def test_concurrent_attach_client_same_transaction_same_email_no_duplicate_no_500(
    client, auth_headers, open_drawer
):
    """Revue robustesse — reproduit le double-tap sur « Envoyer le ticket » :
    deux `POST /pos/transactions/{id}/client` CONCURRENTS (même vente, même
    e-mail, deux requêtes HTTP donc deux sessions DB distinctes via
    `asyncio.gather`) ne doivent jamais se solder par un 500 (course sur la
    contrainte unique `clients.email`) ; un seul client est créé, un seul
    e-mail est effectivement envoyé."""
    sale = await _sell(client, auth_headers)
    email = "double-tap@example.com"

    async def _attach():
        return await client.post(
            f"/api/pos/transactions/{sale['id']}/client",
            json={"email": email, "newsletter_optin": True, "send_receipt": True},
            headers=auth_headers,
        )

    r1, r2 = await asyncio.gather(_attach(), _attach())

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["client"]["id"] == r2.json()["client"]["id"]

    async with async_session() as db:
        client_rows = (
            await db.execute(select(Client).where(Client.email == email))
        ).scalars().all()
        assert len(client_rows) == 1  # un seul client — pas de course sur l'INSERT

        comms = (
            await db.execute(
                select(Communication).where(Communication.transaction_id == uuid.UUID(sale["id"]))
            )
        ).scalars().all()
        assert len(comms) == 1  # un seul e-mail effectivement envoyé


async def test_concurrent_attach_client_same_email_different_transactions_no_500(
    client, auth_headers, open_drawer
):
    """Variante : deux ventes DIFFÉRENTES rattachées concurremment à la
    MÊME adresse (ex. deux caissières servent la même cliente sur deux
    tickets en même temps) — toujours un seul client, jamais de 500."""
    sale1 = await _sell(client, auth_headers)
    sale2 = await _sell(client, auth_headers)
    email = "meme-cliente-deux-ventes@example.com"

    async def _attach(sale_id: str):
        return await client.post(
            f"/api/pos/transactions/{sale_id}/client",
            json={"email": email, "send_receipt": False},
            headers=auth_headers,
        )

    r1, r2 = await asyncio.gather(_attach(sale1["id"]), _attach(sale2["id"]))

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["client"]["id"] == r2.json()["client"]["id"]

    async with async_session() as db:
        client_rows = (
            await db.execute(select(Client).where(Client.email == email))
        ).scalars().all()
        assert len(client_rows) == 1


async def test_pos_opt_out_after_opt_in_removes_contact_from_brevo_list(
    client, auth_headers, open_drawer, monkeypatch
):
    """Revue RGPD (3) — un consentement newsletter qui passe à `False`
    (source `pos`) doit retirer le contact de la liste Brevo dédiée, pas
    seulement le pousser à l'inscription. Point unique :
    `ClientService.sync_brevo`, appelé après chaque `record_consent` de
    source `pos`/`admin` (jamais `webhook` — voir `test_brevo_webhook.py`).
    """
    monkeypatch.setattr(settings, "BREVO_API_KEY", "ak-test")
    monkeypatch.setattr(settings, "BREVO_LIST_ID", "55")

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "url": str(request.url)})
        if request.url.path.endswith("/remove"):
            return httpx.Response(204)
        return httpx.Response(201, json={"id": 1})

    monkeypatch.setattr(brevo_contacts, "_transport", httpx.MockTransport(handler))

    sale1 = await _sell(client, auth_headers)
    r1 = await client.post(
        f"/api/pos/transactions/{sale1['id']}/client",
        json={"email": "opt-in-then-out@example.com", "newsletter_optin": True, "send_receipt": False},
        headers=auth_headers,
    )
    assert r1.status_code == 200, r1.text
    assert calls[-1]["url"] == "https://api.brevo.com/v3/contacts"

    sale2 = await _sell(client, auth_headers)
    r2 = await client.post(
        f"/api/pos/transactions/{sale2['id']}/client",
        json={"email": "opt-in-then-out@example.com", "newsletter_optin": False, "send_receipt": False},
        headers=auth_headers,
    )
    assert r2.status_code == 200, r2.text
    assert calls[-1]["url"] == "https://api.brevo.com/v3/contacts/lists/55/contacts/remove"


# ---------------------------------------------------------------------------
# POST /pos/transactions/{id}/receipt/email
# ---------------------------------------------------------------------------


async def test_resend_receipt_email_requires_email_when_no_client_linked(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/receipt/email", json={}, headers=auth_headers
    )
    assert r.status_code == 422
    assert r.json()["code"] == "email_required"


async def test_resend_receipt_email_uses_linked_client_email_by_default(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "lien@example.com", "send_receipt": False},
        headers=auth_headers,
    )

    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/receipt/email", json={}, headers=auth_headers
    )
    assert r.status_code == 200, r.text

    async with async_session() as db:
        from app.models.pos import Transaction
        from app.models.receipt import Receipt

        tx = (
            await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))
        ).scalar_one()
        receipt = (
            await db.execute(select(Receipt).where(Receipt.transaction_id == tx.id))
        ).scalar_one()
        assert receipt.duplicate_count == 1

        comms = (
            await db.execute(select(Communication).where(Communication.transaction_id == tx.id))
        ).scalars().all()
        assert comms[0].recipient == "lien@example.com"


async def test_resend_receipt_email_with_explicit_email_increments_duplicate(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    r1 = await client.post(
        f"/api/pos/transactions/{sale['id']}/receipt/email",
        json={"email": "walk-in@example.com"},
        headers=auth_headers,
    )
    assert r1.status_code == 200
    r2 = await client.post(
        f"/api/pos/transactions/{sale['id']}/receipt/email",
        json={"email": "walk-in@example.com"},
        headers=auth_headers,
    )
    assert r2.status_code == 200

    async with async_session() as db:
        from app.models.pos import Transaction
        from app.models.receipt import Receipt

        tx = (
            await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))
        ).scalar_one()
        receipt = (
            await db.execute(select(Receipt).where(Receipt.transaction_id == tx.id))
        ).scalar_one()
        assert receipt.duplicate_count == 2


# ---------------------------------------------------------------------------
# Admin — fiche client (§4)
# ---------------------------------------------------------------------------


async def test_admin_list_and_search_clients(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "recherche@example.com", "first_name": "Bea", "send_receipt": False},
        headers=auth_headers,
    )

    r = await client.get("/api/admin/clients", params={"q": "recherche"}, headers=auth_headers)
    assert r.status_code == 200
    emails = [c["email"] for c in r.json()["clients"]]
    assert "recherche@example.com" in emails

    r2 = await client.get("/api/admin/clients", params={"q": "ne-matche-rien-xyz"}, headers=auth_headers)
    assert r2.json()["clients"] == []


async def test_admin_get_client_full_includes_consents_communications_transactions(
    client, auth_headers, open_drawer
):
    sale = await _sell(client, auth_headers)
    attach = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "fiche@example.com", "newsletter_optin": True},
        headers=auth_headers,
    )
    client_id = attach.json()["client"]["id"]

    r = await client.get(f"/api/admin/clients/{client_id}", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client"]["email"] == "fiche@example.com"
    assert len(body["consents"]) == 1
    assert len(body["communications"]) == 1
    assert len(body["transactions"]) == 1


async def test_admin_get_unknown_client_404(client, auth_headers):
    r = await client.get(f"/api/admin/clients/{uuid.uuid4()}", headers=auth_headers)
    assert r.status_code == 404


async def test_admin_add_consent_source_is_admin(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    attach = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "admin-consent@example.com", "newsletter_optin": False},
        headers=auth_headers,
    )
    client_id = attach.json()["client"]["id"]

    r = await client.post(
        f"/api/admin/clients/{client_id}/consents",
        json={"purpose": "newsletter", "granted": True, "note": "Demande orale en boutique"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client"]["newsletter_optin"] is True
    latest = body["consents"][0]
    assert latest["source"] == "admin"
    assert latest["granted"] is True


async def test_admin_consent_granted_false_removes_contact_from_brevo_list(
    client, auth_headers, open_drawer, monkeypatch
):
    """Revue RGPD (3) — bouton admin « Retirer de la newsletter »
    (`POST /admin/clients/{id}/consents` source `admin`, `granted:false`)
    retire aussi le contact de la liste Brevo dédiée."""
    monkeypatch.setattr(settings, "BREVO_API_KEY", "ak-test")
    monkeypatch.setattr(settings, "BREVO_LIST_ID", "66")

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url)})
        if request.url.path.endswith("/remove"):
            return httpx.Response(204)
        return httpx.Response(201, json={"id": 1})

    monkeypatch.setattr(brevo_contacts, "_transport", httpx.MockTransport(handler))

    sale = await _sell(client, auth_headers)
    attach = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "admin-remove@example.com", "newsletter_optin": True, "send_receipt": False},
        headers=auth_headers,
    )
    client_id = attach.json()["client"]["id"]
    assert calls  # push effectué à l'attache (opt-in)
    calls.clear()

    r = await client.post(
        f"/api/admin/clients/{client_id}/consents",
        json={"purpose": "newsletter", "granted": False},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["client"]["newsletter_optin"] is False

    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.brevo.com/v3/contacts/lists/66/contacts/remove"


async def test_admin_export_client_returns_full_json(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    attach = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={"email": "export@example.com", "send_receipt": False},
        headers=auth_headers,
    )
    client_id = attach.json()["client"]["id"]

    r = await client.get(f"/api/admin/clients/{client_id}/export", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client"]["email"] == "export@example.com"
    assert "tickets" in body
    assert len(body["tickets"]) == 1
    assert "exported_at" in body

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "client.exported")
            )
        ).scalars().all()
        assert len(events) == 1


# ---------------------------------------------------------------------------
# Messagerie — aucun secret
# ---------------------------------------------------------------------------


async def test_messaging_status_never_leaks_secrets(client, auth_headers, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "BREVO_API_KEY", "super-secret-brevo-key")
    monkeypatch.setattr(settings, "BREVO_LIST_ID", "99")
    monkeypatch.setattr(settings, "BREVO_WEBHOOK_TOKEN", "super-secret-webhook-token")

    r = await client.get("/api/admin/messaging/status", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["email"]["provider"] == "brevo"
    assert body["brevo_contacts"] == {
        "configured": True,
        "list_id_set": True,
        "webhook_token_set": True,
    }
    raw = r.text
    assert "super-secret-brevo-key" not in raw
    assert "super-secret-webhook-token" not in raw
