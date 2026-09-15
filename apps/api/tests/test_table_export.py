# Nouveau test (PR4, docs/ARCHITECTURE_PR4.md §7, F4) — exports bruts CSV :
# liste blanche stricte (jamais de table client/PII), filtre from/to, BOM
# UTF-8, separateur « ; », JET `export.downloaded`.
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.jet import JournalEvent
from app.services.table_export import EXPORTABLE_TABLES

pytestmark = pytest.mark.anyio


def _uid() -> str:
    return str(uuid.uuid4())


async def test_whitelist_excludes_pii_tables():
    assert "clients" not in EXPORTABLE_TABLES
    assert "consents" not in EXPORTABLE_TABLES
    assert "communications" not in EXPORTABLE_TABLES
    assert "transactions" in EXPORTABLE_TABLES
    assert "journal_events" in EXPORTABLE_TABLES


async def test_download_unknown_table_returns_404(client, auth_headers):
    r = await client.get("/api/admin/exports/table/clients", headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["code"] == "unknown_table"


async def test_download_transactions_table_csv_has_bom_and_semicolon(
    client, auth_headers, open_drawer
):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _uid(),
            "items": [{"label": "Article", "unit_price": "10.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "10.00", "tendered_amount": "10.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201

    resp = await client.get("/api/admin/exports/table/transactions", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(b"\xef\xbb\xbf")
    text = resp.content.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert ";" in header
    assert "transaction_number" in header
    assert "fripco_transactions_" in resp.headers["content-disposition"]

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "export.downloaded")
            )
        ).scalars().all()
    kinds = [e.payload["kind"] for e in events]
    assert "table_transactions" in kinds


async def test_table_export_filters_by_from_to(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _uid(),
            "items": [{"label": "Article", "unit_price": "10.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "10.00", "tendered_amount": "10.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201

    from datetime import datetime, timedelta, timezone

    future_start = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    resp = await client.get(
        "/api/admin/exports/table/transactions",
        params={"from": future_start},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    text = resp.content.decode("utf-8-sig")
    data_lines = [ln for ln in text.splitlines()[1:] if ln.strip()]
    assert data_lines == []  # rien apres une borne dans le futur
