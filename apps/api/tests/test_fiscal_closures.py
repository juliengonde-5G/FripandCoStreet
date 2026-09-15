# Nouveau test (PR4, docs/ARCHITECTURE_PR4.md §7, F5) — clotures fiscales
# periodiques : refus caisse ouverte, refus chaine rompue, archive gzip
# reproductible (deux generations = meme sha256), manifeste verifiable,
# total perpetuel cumule sur deux clotures, JET + mouvements inclus,
# triggers d'immuabilite.
from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import async_session, engine

pytestmark = pytest.mark.anyio


def _uid() -> str:
    return str(uuid.uuid4())


async def _sell_and_close(client, auth_headers, amount: str = "10.00") -> dict:
    open_r = await client.post(
        "/api/pos/drawer/open", json={"opening_amount": "0.00"}, headers=auth_headers
    )
    assert open_r.status_code == 200, open_r.text
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _uid(),
            "items": [{"label": "Article", "unit_price": amount, "quantity": 1}],
            "payments": [{"method": "cash", "amount": amount, "tendered_amount": amount}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    close_r = await client.post(
        "/api/pos/drawer/close", json={"closing_amount": amount}, headers=auth_headers
    )
    assert close_r.status_code == 200, close_r.text
    return close_r.json()


def _wide_period() -> tuple[str, str]:
    # `period_end` ne doit JAMAIS etre dans le futur (§F5 : "clôture future
    # interdite") — `now` lui-meme suffit, l'appel HTTP qui suit avance
    # l'horloge de quelques millisecondes avant que le serveur ne compare.
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=2)).isoformat()
    end = now.isoformat()
    return start, end


async def test_manual_closure_refused_when_drawer_open(client, auth_headers, open_drawer):
    start, end = _wide_period()
    r = await client.post(
        "/api/admin/fiscal-closures",
        json={"closure_type": "manual", "period_start": start, "period_end": end},
        headers=auth_headers,
    )
    assert r.status_code == 409
    assert r.json()["code"] == "drawer_open"

    from app.models.jet import JournalEvent

    async with async_session() as db:
        events = (
            await db.execute(select(JournalEvent).where(JournalEvent.event_type == "closure.failed"))
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["reason"] == "drawer_open"


async def test_manual_closure_succeeds_and_is_sequential(client, auth_headers):
    await _sell_and_close(client, auth_headers, "10.00")
    start, end = _wide_period()
    r = await client.post(
        "/api/admin/fiscal-closures",
        json={"closure_type": "manual", "period_start": start, "period_end": end},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["sequence_number"] == 1
    assert body["closure_type"] == "manual"
    assert body["transaction_count"] == 1
    assert body["previous_hash"] == "0"
    assert body["archive_sha256"]
    assert body["grand_total_sales"] == 10.0
    assert body["perpetual_sales"] == 10.0
    assert body["perpetual_transaction_count"] == 1


async def test_closure_refused_on_broken_chain(client, auth_headers):
    await _sell_and_close(client, auth_headers, "10.00")

    # Casse la chaine des Z par SQL brut (contournement de test — le trigger
    # d'immuabilite interdit un UPDATE, on insere donc une ligne hors chaine).
    async with async_session() as db:
        user_id = (
            await db.execute(text("SELECT user_id FROM z_reports LIMIT 1"))
        ).scalar_one()
    rogue_drawer_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO cash_drawers (id, created_at, updated_at, user_id, opened_at, is_open) "
                "VALUES (:id, now(), now(), :user_id, now(), false)"
            ),
            {"id": rogue_drawer_id, "user_id": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO z_reports "
                "(id, created_at, updated_at, report_number, user_id, cash_drawer_id, "
                "opened_at, closed_at, transaction_count, last_transaction_hash, hash, previous_hash) "
                "VALUES (gen_random_uuid(), now(), now(), 999, :user_id, :drawer_id, "
                "now(), now(), 0, '0', '', 'bidon')"
            ),
            {"user_id": user_id, "drawer_id": rogue_drawer_id},
        )

    start, end = _wide_period()
    r = await client.post(
        "/api/admin/fiscal-closures",
        json={"closure_type": "manual", "period_start": start, "period_end": end},
        headers=auth_headers,
    )
    assert r.status_code == 409
    assert r.json()["code"] == "chain_invalid"

    from app.models.jet import JournalEvent

    async with async_session() as db:
        events = (
            await db.execute(select(JournalEvent).where(JournalEvent.event_type == "closure.failed"))
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["reason"] == "chain_invalid"


async def test_archive_is_gzip_reproducible_across_two_generations(client, auth_headers):
    await _sell_and_close(client, auth_headers, "10.00")
    start = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    mid = datetime.now(timezone.utc).isoformat()

    r1 = await client.post(
        "/api/admin/fiscal-closures",
        json={"closure_type": "manual", "period_start": start, "period_end": mid},
        headers=auth_headers,
    )
    assert r1.status_code == 201, r1.text
    closure_id = r1.json()["id"]

    a1 = await client.get(f"/api/admin/fiscal-closures/{closure_id}/archive", headers=auth_headers)
    a2 = await client.get(f"/api/admin/fiscal-closures/{closure_id}/archive", headers=auth_headers)
    assert a1.status_code == 200 and a2.status_code == 200
    assert a1.content == a2.content
    assert a1.headers["x-archive-sha256"] == a2.headers["x-archive-sha256"]
    assert hashlib.sha256(a1.content).hexdigest() == a1.headers["x-archive-sha256"]

    decompressed = json.loads(gzip.decompress(a1.content))
    assert decompressed["totals"]["sales_ttc"] == "10.00"
    assert decompressed["software_version"]
    assert decompressed["fiscal_version_date"]
    assert "cash_movements" in decompressed
    assert "journal_events" in decompressed
    assert "receipts" in decompressed
    assert "shop_settings" in decompressed


async def test_perpetual_total_cumulates_across_two_closures(client, auth_headers):
    await _sell_and_close(client, auth_headers, "10.00")
    t1 = datetime.now(timezone.utc)
    r1 = await client.post(
        "/api/admin/fiscal-closures",
        json={
            "closure_type": "manual",
            "period_start": (t1 - timedelta(days=1)).isoformat(),
            "period_end": t1.isoformat(),
        },
        headers=auth_headers,
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["perpetual_sales"] == 10.0

    await _sell_and_close(client, auth_headers, "20.00")
    t2 = datetime.now(timezone.utc)
    r2 = await client.post(
        "/api/admin/fiscal-closures",
        json={"closure_type": "manual", "period_start": t1.isoformat(), "period_end": t2.isoformat()},
        headers=auth_headers,
    )
    assert r2.status_code == 201, r2.text
    body2 = r2.json()
    assert body2["sequence_number"] == 2
    assert body2["grand_total_sales"] == 20.0  # cette periode seulement
    assert body2["perpetual_sales"] == 30.0  # cumul depuis le debut
    assert body2["perpetual_transaction_count"] == 2
    assert body2["previous_hash"] == r1.json()["hash"]


async def test_fiscal_closures_integrity_endpoint(client, auth_headers):
    await _sell_and_close(client, auth_headers, "10.00")
    start, end = _wide_period()
    r = await client.post(
        "/api/admin/fiscal-closures",
        json={"closure_type": "manual", "period_start": start, "period_end": end},
        headers=auth_headers,
    )
    assert r.status_code == 201

    ri = await client.get("/api/admin/fiscal-closures/integrity", headers=auth_headers)
    assert ri.status_code == 200
    assert ri.json()["valid"] is True

    rg = await client.get("/api/admin/fiscal/integrity", headers=auth_headers)
    assert rg.status_code == 200
    assert rg.json()["closures"]["valid"] is True
    assert rg.json()["accounting"]["valid"] is True


async def test_closure_creation_journals_jet_export_downloaded_on_archive(client, auth_headers):
    await _sell_and_close(client, auth_headers, "10.00")
    start, end = _wide_period()
    r = await client.post(
        "/api/admin/fiscal-closures",
        json={"closure_type": "manual", "period_start": start, "period_end": end},
        headers=auth_headers,
    )
    closure_id = r.json()["id"]

    from app.models.jet import JournalEvent

    async with async_session() as db:
        created_events = (
            await db.execute(select(JournalEvent).where(JournalEvent.event_type == "closure.created"))
        ).scalars().all()
    assert len(created_events) == 1
    assert created_events[0].payload["sequence"] == 1

    await client.get(f"/api/admin/fiscal-closures/{closure_id}/archive", headers=auth_headers)

    async with async_session() as db:
        dl_events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "export.downloaded")
            )
        ).scalars().all()
    kinds = [e.payload["kind"] for e in dl_events]
    assert "fiscal_closure_archive" in kinds


async def test_fiscal_closures_table_immutable(client, auth_headers):
    await _sell_and_close(client, auth_headers, "10.00")
    start, end = _wide_period()
    r = await client.post(
        "/api/admin/fiscal-closures",
        json={"closure_type": "manual", "period_start": start, "period_end": end},
        headers=auth_headers,
    )
    closure_id = r.json()["id"]

    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE fiscal_closures SET sequence_number = 999 WHERE id = :id"),
                {"id": closure_id},
            )
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM fiscal_closures WHERE id = :id"), {"id": closure_id})


async def test_period_future_rejected(client, auth_headers):
    await _sell_and_close(client, auth_headers, "10.00")
    future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    r = await client.post(
        "/api/admin/fiscal-closures",
        json={"closure_type": "manual", "period_start": datetime.now(timezone.utc).isoformat(), "period_end": future},
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_period"
