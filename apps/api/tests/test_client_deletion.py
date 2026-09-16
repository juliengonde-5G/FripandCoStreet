# Nouveau test (PR10, docs/ARCHITECTURE_PR10.md, contrat L5) — suppression
# RGPD DIFFEREE : la demande pose une date d'effet (reglage
# `rgpd.deletion_delay_days`, 30 jours par defaut), la cliente en recoit un
# accuse de reception trace dans `communications`, l'annulation est possible
# tant que l'echeance n'est pas atteinte, et un cron quotidien solde les
# demandes echues par l'anonymisation ordinaire. Le JET ne porte jamais de
# donnee personnelle.
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.jobs import run_daily_client_deletions
from app.models.client import Client
from app.models.communication import Communication, CommunicationKind
from app.models.jet import JournalEvent
from app.services.client_service import ClientService
from app.services.rgpd_email import SUBJECT, build_deletion_request_email
from app.services.settings_service import (
    DEFAULT_VALUES,
    SettingsService,
    clamp_deletion_delay_days,
)

pytestmark = pytest.mark.anyio


async def _create_client(client, auth_headers, **payload) -> dict:
    r = await client.post("/api/pos/clients", json=payload, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()["client"]


async def _row(client_id: str) -> Client:
    async with async_session() as db:
        return (
            await db.execute(select(Client).where(Client.id == uuid.UUID(client_id)))
        ).scalar_one()


# ---------------------------------------------------------------------------
# Reglage
# ---------------------------------------------------------------------------


def test_default_delay_is_thirty_days_and_clamped():
    assert DEFAULT_VALUES["rgpd"]["deletion_delay_days"] == 30
    assert clamp_deletion_delay_days(45) == 45
    # Bornes 1-90, valeurs aberrantes ramenees dans la fenetre.
    assert clamp_deletion_delay_days(0) == 1
    assert clamp_deletion_delay_days(-10) == 1
    assert clamp_deletion_delay_days(365) == 90
    # JSONB ecrit a la main : jamais d'exception, on retombe sur le defaut.
    assert clamp_deletion_delay_days("trente") == 30
    assert clamp_deletion_delay_days(None) == 30


async def test_rgpd_setting_is_readable_and_bounded_by_the_api(client, auth_headers):
    r = await client.get("/api/admin/settings/rgpd", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"deletion_delay_days": 30}

    ok = await client.put(
        "/api/admin/settings/rgpd", json={"deletion_delay_days": 7}, headers=auth_headers
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["deletion_delay_days"] == 7

    for invalid in (0, 91):
        ko = await client.put(
            "/api/admin/settings/rgpd",
            json={"deletion_delay_days": invalid},
            headers=auth_headers,
        )
        assert ko.status_code == 422, ko.text
        assert ko.json()["code"] == "invalid_setting"


# ---------------------------------------------------------------------------
# Demande
# ---------------------------------------------------------------------------


async def test_request_deletion_sets_the_dates_and_notifies(client, auth_headers):
    fiche = await _create_client(
        client, auth_headers, email="alice@example.com", first_name="Alice"
    )
    before = datetime.now(timezone.utc)

    r = await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-request", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()["client"]
    assert body["deletion_requested_at"] is not None
    assert body["deletion_scheduled_for"] is not None

    row = await _row(fiche["id"])
    assert row.deletion_requested_by_user_id is not None
    # 30 jours par defaut (tolerance : le temps d'execution du test).
    delta = row.deletion_scheduled_for - before
    assert timedelta(days=29, hours=23) < delta < timedelta(days=30, minutes=5)
    # La fiche reste PLEINEMENT utilisable : rien n'est efface aujourd'hui.
    assert row.anonymized_at is None
    assert row.first_name == "Alice"

    async with async_session() as db:
        comms = (
            await db.execute(
                select(Communication).where(
                    Communication.client_id == uuid.UUID(fiche["id"])
                )
            )
        ).scalars().all()
    assert len(comms) == 1
    assert comms[0].kind == CommunicationKind.rgpd
    assert comms[0].subject == SUBJECT
    assert comms[0].recipient == "alice@example.com"
    # Sans fournisseur configure, la passerelle simule l'envoi (E6).
    assert comms[0].provider.value == "simulated"
    assert comms[0].status.value == "simulated"


async def test_request_deletion_honours_the_configured_delay(client, auth_headers):
    await client.put(
        "/api/admin/settings/rgpd", json={"deletion_delay_days": 3}, headers=auth_headers
    )
    fiche = await _create_client(client, auth_headers, email="pressee@example.com")
    before = datetime.now(timezone.utc)
    r = await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-request", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    row = await _row(fiche["id"])
    assert timedelta(days=2, hours=23) < row.deletion_scheduled_for - before < timedelta(days=3, minutes=5)


async def test_request_deletion_without_email_writes_no_communication(
    client, auth_headers
):
    """Une fiche « telephone seul » (PR7/I3) : la demande est enregistree,
    il n'y a simplement personne a prevenir par e-mail — pas de SMS."""
    fiche = await _create_client(client, auth_headers, phone="06 11 22 33 44")
    r = await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-request", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    async with async_session() as db:
        comms = (
            await db.execute(
                select(Communication).where(
                    Communication.client_id == uuid.UUID(fiche["id"])
                )
            )
        ).scalars().all()
    assert comms == []


async def test_request_deletion_refuses_a_second_request(client, auth_headers):
    fiche = await _create_client(client, auth_headers, email="deux-fois@example.com")
    first = await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-request", headers=auth_headers
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-request", headers=auth_headers
    )
    assert second.status_code == 409
    assert second.json()["code"] == "already_requested"


async def test_request_deletion_refuses_an_inactive_client(client, auth_headers):
    anonymized = await _create_client(client, auth_headers, email="deja@example.com")
    await client.post(
        f"/api/admin/clients/{anonymized['id']}/anonymize",
        json={"reason": "Demande cliente"},
        headers=auth_headers,
    )
    r = await client.post(
        f"/api/admin/clients/{anonymized['id']}/deletion-request", headers=auth_headers
    )
    assert r.status_code == 409
    assert r.json()["code"] == "client_inactive"

    keeper = await _create_client(client, auth_headers, email="gardee2@example.com")
    absorbed = await _create_client(client, auth_headers, email="absorbee2@example.com")
    async with async_session() as db:
        row = (
            await db.execute(
                select(Client).where(Client.id == uuid.UUID(absorbed["id"]))
            )
        ).scalar_one()
        row.merged_into_client_id = uuid.UUID(keeper["id"])
        row.merged_at = datetime.now(timezone.utc)
        await db.commit()
    merged = await client.post(
        f"/api/admin/clients/{absorbed['id']}/deletion-request", headers=auth_headers
    )
    assert merged.status_code == 409
    assert merged.json()["code"] == "client_inactive"


async def test_request_deletion_404_on_unknown_client(client, auth_headers):
    r = await client.post(
        f"/api/admin/clients/{uuid.uuid4()}/deletion-request", headers=auth_headers
    )
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# Annulation
# ---------------------------------------------------------------------------


async def test_cancel_deletion_clears_the_three_fields(client, auth_headers):
    fiche = await _create_client(client, auth_headers, email="ravisee@example.com")
    await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-request", headers=auth_headers
    )
    r = await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-cancel", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()["client"]
    assert body["deletion_requested_at"] is None
    assert body["deletion_scheduled_for"] is None

    row = await _row(fiche["id"])
    assert row.deletion_requested_by_user_id is None
    assert row.anonymized_at is None


async def test_cancel_deletion_refuses_when_nothing_is_pending(client, auth_headers):
    fiche = await _create_client(client, auth_headers, email="rien@example.com")
    r = await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-cancel", headers=auth_headers
    )
    assert r.status_code == 409
    assert r.json()["code"] == "not_requested"


async def test_a_client_awaiting_deletion_stays_usable_at_the_till(
    client, auth_headers, open_drawer
):
    """La personne est toujours cliente jusqu'a la date d'effet : la fiche
    se retrouve en caisse, s'attache a une vente et garde son historique."""
    fiche = await _create_client(
        client, auth_headers, email="encore-la@example.com", first_name="Manon"
    )
    await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-request", headers=auth_headers
    )

    found = await client.get(
        "/api/pos/clients/search?q=encore-la", headers=auth_headers
    )
    assert found.status_code == 200, found.text
    assert [c["id"] for c in found.json()["clients"]] == [fiche["id"]]

    sale = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "client_id": fiche["id"],
            "items": [{"label": "Robe", "unit_price": "18.00", "quantity": 1}],
            "payments": [
                {"method": "cash", "amount": "18.00", "tendered_amount": "18.00"}
            ],
        },
        headers=auth_headers,
    )
    assert sale.status_code == 201, sale.text

    history = await client.get(
        f"/api/pos/clients/{fiche['id']}/history", headers=auth_headers
    )
    assert history.status_code == 200, history.text
    assert history.json()["total_spent"] == "18.00"


# ---------------------------------------------------------------------------
# Suppression immediate
# ---------------------------------------------------------------------------


async def test_immediate_anonymize_clears_a_pending_request(client, auth_headers):
    fiche = await _create_client(client, auth_headers, email="tout-de-suite@example.com")
    await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-request", headers=auth_headers
    )
    r = await client.post(
        f"/api/admin/clients/{fiche['id']}/anonymize",
        json={"reason": "La cliente insiste"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    row = await _row(fiche["id"])
    assert row.anonymized_at is not None
    assert row.deletion_requested_at is None
    assert row.deletion_scheduled_for is None
    assert row.deletion_requested_by_user_id is None


# ---------------------------------------------------------------------------
# Cron quotidien 04:00
# ---------------------------------------------------------------------------


async def test_cron_anonymizes_only_the_due_clients(client, auth_headers):
    due = await _create_client(client, auth_headers, email="echue@example.com")
    pending = await _create_client(client, auth_headers, email="pas-encore@example.com")
    untouched = await _create_client(client, auth_headers, email="tranquille@example.com")
    for fiche in (due, pending):
        r = await client.post(
            f"/api/admin/clients/{fiche['id']}/deletion-request", headers=auth_headers
        )
        assert r.status_code == 200, r.text

    # On antidate l'echeance de la premiere fiche : le cron doit la solder.
    async with async_session() as db:
        row = (
            await db.execute(select(Client).where(Client.id == uuid.UUID(due["id"])))
        ).scalar_one()
        row.deletion_scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()

    await run_daily_client_deletions()

    soldee = await _row(due["id"])
    assert soldee.anonymized_at is not None
    assert soldee.email.endswith("@anonyme.invalid")
    assert soldee.deletion_scheduled_for is None

    encore = await _row(pending["id"])
    assert encore.anonymized_at is None
    assert encore.email == "pas-encore@example.com"
    assert encore.deletion_scheduled_for is not None

    intacte = await _row(untouched["id"])
    assert intacte.anonymized_at is None
    assert intacte.email == "tranquille@example.com"


async def test_cron_is_idempotent_on_an_already_anonymized_client(client, auth_headers):
    fiche = await _create_client(client, auth_headers, email="deja-videe@example.com")
    await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-request", headers=auth_headers
    )
    async with async_session() as db:
        row = (
            await db.execute(select(Client).where(Client.id == uuid.UUID(fiche["id"])))
        ).scalar_one()
        row.deletion_scheduled_for = datetime.now(timezone.utc) - timedelta(days=1)
        await db.commit()

    await run_daily_client_deletions()
    first_pass = await _row(fiche["id"])
    await run_daily_client_deletions()
    second_pass = await _row(fiche["id"])
    assert second_pass.anonymized_at == first_pass.anonymized_at
    assert second_pass.email == first_pass.email


# ---------------------------------------------------------------------------
# JET — trace inalterable, jamais de donnee personnelle
# ---------------------------------------------------------------------------


async def test_jet_records_the_two_gestures_without_any_personal_data(
    client, auth_headers
):
    fiche = await _create_client(
        client, auth_headers, email="journal@example.com", first_name="Jeanne",
        last_name="Dubois", phone="06 55 44 33 22",
    )
    await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-request", headers=auth_headers
    )
    await client.post(
        f"/api/admin/clients/{fiche['id']}/deletion-cancel", headers=auth_headers
    )

    async with async_session() as db:
        events = (
            await db.execute(select(JournalEvent).order_by(JournalEvent.seq))
        ).scalars().all()
    types = [e.event_type for e in events]
    assert "client.deletion_requested" in types
    assert "client.deletion_cancelled" in types

    requested = next(e for e in events if e.event_type == "client.deletion_requested")
    assert requested.payload["client_id"] == fiche["id"]
    assert requested.payload["scheduled_for"]
    cancelled = next(e for e in events if e.event_type == "client.deletion_cancelled")
    assert cancelled.payload == {"client_id": fiche["id"]}

    blob = json.dumps([e.payload for e in events], ensure_ascii=False)
    for pii in ("journal@example.com", "Jeanne", "Dubois", "0655443322", "+33655443322"):
        assert pii not in blob


# ---------------------------------------------------------------------------
# Accuse de reception (fonction pure)
# ---------------------------------------------------------------------------


def test_deletion_request_email_states_the_date_the_reversal_and_the_dpo():
    message = build_deletion_request_email(
        to="cliente@example.com",
        scheduled_for=datetime(2026, 10, 16, 22, 30, tzinfo=timezone.utc),
        shop={"name": "Frip & Co Street", "dpo_email": "dpo@example.com"},
    )
    assert message.subject == SUBJECT
    assert message.to == "cliente@example.com"
    # Date d'effet dans le fuseau de la BOUTIQUE : 22:30 UTC le 16/10 est
    # deja le 17/10 a Paris (UTC+2 en octobre).
    assert "17/10/2026" in message.text
    assert "17/10/2026" in message.html
    assert "boutique" in message.text
    assert "dpo@example.com" in message.text
    assert "dpo@example.com" in message.html
    # Aucun lien : ni annulation en un clic, ni pixel de suivi.
    assert "http://" not in message.html
    assert "https://" not in message.html


def test_deletion_request_email_without_dpo_mentions_nothing():
    message = build_deletion_request_email(
        to="cliente@example.com",
        scheduled_for=datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc),
        shop={"name": "Frip & Co Street"},
    )
    assert "16/10/2026" in message.text
    assert "données" in message.text


async def test_settings_service_reads_the_delay(client, auth_headers):
    async with async_session() as db:
        assert await SettingsService(db).get_deletion_delay_days() == 30
    await client.put(
        "/api/admin/settings/rgpd", json={"deletion_delay_days": 90}, headers=auth_headers
    )
    async with async_session() as db:
        assert await SettingsService(db).get_deletion_delay_days() == 90


async def test_service_level_request_and_cancel(client, auth_headers):
    """Le service est utilisable hors HTTP (c'est lui que le cron appelle)."""
    fiche = await _create_client(client, auth_headers, email="service@example.com")
    async with async_session() as db:
        row = (
            await db.execute(select(Client).where(Client.id == uuid.UUID(fiche["id"])))
        ).scalar_one()
        await ClientService(db).request_deletion(client=row, user_id=None)
        await db.commit()
    assert (await _row(fiche["id"])).deletion_scheduled_for is not None

    async with async_session() as db:
        row = (
            await db.execute(select(Client).where(Client.id == uuid.UUID(fiche["id"])))
        ).scalar_one()
        await ClientService(db).cancel_deletion(client=row, user_id=None)
        await db.commit()
    assert (await _row(fiche["id"])).deletion_scheduled_for is None
