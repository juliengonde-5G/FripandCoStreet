# Nouveau test (PR4, docs/ARCHITECTURE_PR4.md §7, F6) — export fiscal a la
# demande : 409 si chaine invalide, sha256 stable (corps reproductible sur
# la meme periode), JET+mouvements inclus, JET `export.downloaded`.
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.core.database import async_session, engine
from app.models.jet import JournalEvent

pytestmark = pytest.mark.anyio


def _uid() -> str:
    return str(uuid.uuid4())


async def _sell(client, auth_headers, amount: str = "10.00"):
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
    return r.json()


def _period() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=2)).isoformat(), (now + timedelta(minutes=1)).isoformat()


async def test_fiscal_export_json_sha256_stable_across_two_calls(client, auth_headers, open_drawer):
    await _sell(client, auth_headers, "10.00")
    start, end = _period()

    r1 = await client.get(
        "/api/admin/fiscal-export", params={"from": start, "to": end, "format": "json"},
        headers=auth_headers,
    )
    r2 = await client.get(
        "/api/admin/fiscal-export", params={"from": start, "to": end, "format": "json"},
        headers=auth_headers,
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content
    assert r1.headers["x-export-sha256"] == r2.headers["x-export-sha256"]
    assert hashlib.sha256(r1.content).hexdigest() == r1.headers["x-export-sha256"]
    assert b"generated_at" not in r1.content

    body = r1.json()
    assert "journal_events" in body
    assert "cash_movements" in body
    assert len(body["journal_events"]) > 0


async def test_fiscal_export_xml_sha256_stable(client, auth_headers, open_drawer):
    await _sell(client, auth_headers, "10.00")
    start, end = _period()
    r1 = await client.get(
        "/api/admin/fiscal-export", params={"from": start, "to": end, "format": "xml"},
        headers=auth_headers,
    )
    r2 = await client.get(
        "/api/admin/fiscal-export", params={"from": start, "to": end, "format": "xml"},
        headers=auth_headers,
    )
    assert r1.status_code == 200
    assert r1.content == r2.content
    assert r1.headers["x-export-sha256"] == r2.headers["x-export-sha256"]
    assert r1.headers["content-type"].startswith("application/xml")


async def test_fiscal_export_refused_on_broken_chain(client, auth_headers, open_drawer):
    await _sell(client, auth_headers, "10.00")

    async with async_session() as db:
        user_id = (
            await db.execute(text("SELECT user_id FROM transactions LIMIT 1"))
        ).scalar_one()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO transactions "
                "(id, created_at, updated_at, transaction_number, transaction_type, "
                "user_id, client_uuid, tva_rate, total_ht, total_tva, total_ttc, "
                "hash_chain, previous_hash, receipt_number) "
                "VALUES (gen_random_uuid(), now(), now(), 999, 'sale', "
                ":user_id, :client_uuid, 20.00, 0, 0, 0, 'deadbeef', 'not-the-real-hash', 999)"
            ),
            {"user_id": user_id, "client_uuid": uuid.uuid4()},
        )

    start, end = _period()
    r = await client.get(
        "/api/admin/fiscal-export", params={"from": start, "to": end}, headers=auth_headers
    )
    assert r.status_code == 409
    assert r.json()["code"] == "chain_invalid"


async def test_fiscal_export_logs_jet_export_downloaded(client, auth_headers, open_drawer):
    await _sell(client, auth_headers, "10.00")
    start, end = _period()
    r = await client.get(
        "/api/admin/fiscal-export", params={"from": start, "to": end}, headers=auth_headers
    )
    assert r.status_code == 200

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "export.downloaded")
            )
        ).scalars().all()
    kinds = [e.payload["kind"] for e in events]
    assert "fiscal_export_json" in kinds
    assert events[0].payload["sha256"] == r.headers["x-export-sha256"]
