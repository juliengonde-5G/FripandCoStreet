# Nouveau test (PR3, §7 ARCHITECTURE_PR3.md, E9) — `POST /api/brevo/webhook`
# : SANS JWT, authentifie par token partage (query ou header
# `X-Brevo-Token`). Sans token configure -> 403 ; mauvais token -> 403 ; bon
# token -> consentement revoque + evenement JET.
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import async_session
from app.models.client import Client
from app.models.jet import JournalEvent

pytestmark = pytest.mark.anyio


async def _create_client(email: str, *, newsletter_optin: bool = True) -> None:
    async with async_session() as db:
        db.add(Client(email=email, newsletter_optin=newsletter_optin))
        await db.commit()


async def test_webhook_refuses_without_configured_token(client, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_WEBHOOK_TOKEN", "")
    r = await client.post(
        "/api/brevo/webhook",
        params={"token": "anything"},
        json={"event": "unsubscribed", "email": "x@y.fr"},
    )
    assert r.status_code == 403
    # Corps plat {detail, code} (comme le reste de l'API, cf. CLAUDE.md) —
    # `_check_token` leve `PosServiceError`, pas `HTTPException`.
    assert r.json()["code"] == "webhook_forbidden"


async def test_webhook_refuses_wrong_token(client, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_WEBHOOK_TOKEN", "expected-token")
    r = await client.post(
        "/api/brevo/webhook",
        params={"token": "wrong-token"},
        json={"event": "unsubscribed", "email": "x@y.fr"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "webhook_forbidden"


async def test_webhook_accepts_query_token_and_revokes_consent(client, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_WEBHOOK_TOKEN", "expected-token")
    await _create_client("desabonnee@example.com")

    r = await client.post(
        "/api/brevo/webhook",
        params={"token": "expected-token"},
        json={"event": "unsubscribed", "email": "desabonnee@example.com"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"applied": 1, "skipped": 0}

    async with async_session() as db:
        refreshed = (
            await db.execute(select(Client).where(Client.email == "desabonnee@example.com"))
        ).scalar_one()
        assert refreshed.newsletter_optin is False

        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "consent.revoked")
            )
        ).scalars().all()
        assert len(events) == 1
        webhook_events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "brevo.webhook_received")
            )
        ).scalars().all()
        assert len(webhook_events) == 1


async def test_webhook_header_token_accepted(client, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_WEBHOOK_TOKEN", "expected-token")
    await _create_client("via-header@example.com")

    r = await client.post(
        "/api/brevo/webhook",
        headers={"X-Brevo-Token": "expected-token"},
        json={"event": "hard_bounce", "email": "via-header@example.com"},
    )
    assert r.status_code == 200
    assert r.json() == {"applied": 1, "skipped": 0}


async def test_webhook_accepts_list_payload_and_skips_unrelated_events(client, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_WEBHOOK_TOKEN", "expected-token")
    await _create_client("liste@example.com")

    r = await client.post(
        "/api/brevo/webhook",
        params={"token": "expected-token"},
        json=[
            {"event": "delivered", "email": "liste@example.com"},
            {"event": "unsubscribed", "email": "liste@example.com"},
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == 1
    assert body["skipped"] == 1


async def test_webhook_unknown_client_replayed_is_skipped_not_an_error(client, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_WEBHOOK_TOKEN", "expected-token")
    r = await client.post(
        "/api/brevo/webhook",
        params={"token": "expected-token"},
        json={"event": "unsubscribed", "email": "personne@example.com"},
    )
    assert r.status_code == 200
    assert r.json() == {"applied": 0, "skipped": 1}
