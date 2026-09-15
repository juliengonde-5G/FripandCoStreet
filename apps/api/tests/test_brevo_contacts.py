# Nouveau test (PR3, §7 ARCHITECTURE_PR3.md) — synchro Brevo Contacts (E1) :
# `push_contact`/`remove_from_list` HTTP mocke (`httpx.MockTransport`
# injecte dans `brevo_contacts._transport`) et `apply_webhook_event`
# (base de donnees reelle, cf. `tests/test_triggers.py` pour le patron
# PostgreSQL-only du depot).
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.client import Client, Consent, ConsentSource
from app.services import brevo_contacts

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _configure_brevo(monkeypatch):
    monkeypatch.setattr(settings, "BREVO_API_KEY", "ak-test")
    monkeypatch.setattr(settings, "BREVO_LIST_ID", "42")
    monkeypatch.setattr(settings, "BREVO_WEBHOOK_TOKEN", "wh-token")
    monkeypatch.setattr(brevo_contacts, "_transport", None)
    yield
    monkeypatch.setattr(brevo_contacts, "_transport", None)


def _client_obj(**overrides) -> Client:
    defaults = dict(email="cliente@example.com", first_name="Alice", last_name="Martin")
    defaults.update(overrides)
    c = Client(**defaults)
    c.id = uuid.uuid4()
    return c


async def test_push_contact_calls_expected_endpoint_and_body():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 1})

    brevo_contacts._transport = httpx.MockTransport(handler)

    result = await brevo_contacts.push_contact(_client_obj())

    assert result.ok is True
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.brevo.com/v3/contacts"
    assert seen["headers"]["api-key"] == "ak-test"
    assert seen["body"]["email"] == "cliente@example.com"
    assert seen["body"]["attributes"] == {"PRENOM": "Alice", "NOM": "Martin"}
    assert seen["body"]["listIds"] == [42]
    assert seen["body"]["updateEnabled"] is True
    # E1 — jamais de blocklist globale sur ce compte partagé.
    assert "emailBlacklisted" not in seen["body"]


async def test_push_contact_204_update_is_ok():
    brevo_contacts._transport = httpx.MockTransport(lambda r: httpx.Response(204))
    result = await brevo_contacts.push_contact(_client_obj())
    assert result.ok is True
    assert result.status_code == 204


async def test_push_contact_error_status_surfaced():
    brevo_contacts._transport = httpx.MockTransport(
        lambda r: httpx.Response(400, json={"message": "invalid"})
    )
    result = await brevo_contacts.push_contact(_client_obj())
    assert result.ok is False
    assert result.status_code == 400


async def test_remove_from_list_calls_expected_endpoint_never_deletes_contact():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    brevo_contacts._transport = httpx.MockTransport(handler)

    result = await brevo_contacts.remove_from_list("cliente@example.com")

    assert result.ok is True
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.brevo.com/v3/contacts/lists/42/contacts/remove"
    assert seen["body"] == {"emails": ["cliente@example.com"]}


async def test_remove_from_list_404_is_treated_as_ok():
    brevo_contacts._transport = httpx.MockTransport(lambda r: httpx.Response(404))
    result = await brevo_contacts.remove_from_list("absent@example.com")
    assert result.ok is True


def test_describe_never_leaks_secrets():
    body = brevo_contacts.describe()
    assert body == {"configured": True, "list_id_set": True, "webhook_token_set": True}
    assert "ak-test" not in json.dumps(body)


# ---------------------------------------------------------------------------
# apply_webhook_event — base de donnees reelle (via `client`/`auth_headers`).
# ---------------------------------------------------------------------------


async def test_apply_webhook_event_revokes_consent_and_journals():
    from app.core.database import async_session

    async with async_session() as db:
        c = Client(email="opt-in@example.com", first_name="Alice", newsletter_optin=True)
        db.add(c)
        await db.commit()
        await db.refresh(c)
        client_id = c.id

    async with async_session() as db:
        result = await brevo_contacts.apply_webhook_event(db, {"event": "unsubscribed", "email": "opt-in@example.com"})
        await db.commit()
    assert result["applied"] is True

    async with async_session() as db:
        refreshed = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one()
        assert refreshed.newsletter_optin is False
        consents = (
            await db.execute(select(Consent).where(Consent.client_id == client_id))
        ).scalars().all()
        assert len(consents) == 1
        assert consents[0].granted is False
        assert consents[0].source == ConsentSource.webhook


async def test_apply_webhook_event_ignores_unknown_event_type():
    from app.core.database import async_session

    async with async_session() as db:
        c = Client(email="opt-in2@example.com", newsletter_optin=True)
        db.add(c)
        await db.commit()

    async with async_session() as db:
        result = await brevo_contacts.apply_webhook_event(db, {"event": "delivered", "email": "opt-in2@example.com"})
    assert result["applied"] is False


async def test_apply_webhook_event_unknown_client_is_noop():
    from app.core.database import async_session

    async with async_session() as db:
        result = await brevo_contacts.apply_webhook_event(
            db, {"event": "unsubscribed", "email": "nobody@example.com"}
        )
    assert result == {"applied": False, "reason": "client_not_found"}
