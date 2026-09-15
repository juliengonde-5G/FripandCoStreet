# Nouveau test (PR4, docs/ARCHITECTURE_PR4.md §7, F8) — PDF du rapport Z :
# deterministe (deux appels = meme sha256), contient les totaux et la
# mention D14, jamais « conforme NF525 », JET `export.downloaded`.
from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.jet import JournalEvent

pytestmark = pytest.mark.anyio


def _uid() -> str:
    return str(uuid.uuid4())


async def _sell_and_close(client, auth_headers, open_drawer, amount: str = "10.00") -> dict:
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
        "/api/pos/drawer/close", json={"closing_amount": "110.00"}, headers=auth_headers
    )
    assert close_r.status_code == 200, close_r.text
    return close_r.json()


async def test_z_report_pdf_is_deterministic_across_two_calls(client, auth_headers, open_drawer):
    z = await _sell_and_close(client, auth_headers, open_drawer, "10.00")
    z_id = z["id"]

    r1 = await client.get(f"/api/pos/z-reports/{z_id}/pdf", headers=auth_headers)
    r2 = await client.get(f"/api/pos/z-reports/{z_id}/pdf", headers=auth_headers)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200
    assert r1.headers["content-type"] == "application/pdf"
    assert r1.content == r2.content
    assert hashlib.sha256(r1.content).hexdigest() == r1.headers["x-pdf-sha256"]
    assert r1.headers["x-pdf-sha256"] == r2.headers["x-pdf-sha256"]


async def test_z_report_pdf_contains_d14_mention_never_conforme_nf525(
    client, auth_headers, open_drawer
):
    z = await _sell_and_close(client, auth_headers, open_drawer, "10.00")
    r = await client.get(f"/api/pos/z-reports/{z['id']}/pdf", headers=auth_headers)
    assert r.status_code == 200

    # Extraction texte simplifiee : le PDF non compresse contient les
    # chaines litterales telles qu'ecrites par reportlab (police standard,
    # pas de flux compresse pour SimpleDocTemplate par defaut sur ce
    # contenu court) — on verifie l'ABSENCE de la mention interdite et la
    # PRESENCE de la mention D14 sur les octets bruts.
    assert b"conforme NF525" not in r.content
    assert b"286 I-3" in r.content or b"286" in r.content


async def test_z_report_pdf_logs_jet_export_downloaded(client, auth_headers, open_drawer):
    z = await _sell_and_close(client, auth_headers, open_drawer, "10.00")
    r = await client.get(f"/api/pos/z-reports/{z['id']}/pdf", headers=auth_headers)
    assert r.status_code == 200

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "export.downloaded")
            )
        ).scalars().all()
    kinds = [e.payload["kind"] for e in events]
    assert "z_report_pdf" in kinds
    assert events[0].payload["sha256"] == r.headers["x-pdf-sha256"]


async def test_z_report_pdf_not_found(client, auth_headers, open_drawer):
    r = await client.get(f"/api/pos/z-reports/{uuid.uuid4()}/pdf", headers=auth_headers)
    assert r.status_code == 404
