# Extrait de Vintiz (jobs.py:648-652, test structure inspiree de
# test_z_regularization.py) — garde fiscale 23:59 (§4.3) : ferme toute caisse
# oubliee, Z `counted=false`, `closing_amount=expected`, `closed_by_guard`.
# Echec du job -> JET `system.job_failed` (jamais avale en silence, S-5).
import uuid

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.jobs import run_daily_fiscal_close_guard
from app.models.jet import JournalEvent
from app.models.pos import CashDrawer, ZReport

pytestmark = pytest.mark.anyio


async def _sell(client, auth_headers, amount: str = "10.00"):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Article", "unit_price": amount, "quantity": 1}],
            "payments": [{"method": "cash", "amount": amount, "tendered_amount": amount}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_guard_closes_forgotten_open_drawer(client, auth_headers, open_drawer):
    await _sell(client, auth_headers, "10.00")

    await run_daily_fiscal_close_guard()

    async with async_session() as db:
        drawer = (
            await db.execute(select(CashDrawer).where(CashDrawer.id == uuid.UUID(open_drawer["id"])))
        ).scalar_one()
        assert drawer.is_open is False
        assert drawer.closed_by_guard is True
        assert drawer.closing_amount == drawer.expected_amount

        z = (await db.execute(select(ZReport).where(ZReport.cash_drawer_id == drawer.id))).scalar_one()
        assert z.counted is False
        assert z.transaction_count == 1

        auto_closed_events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "drawer.auto_closed")
            )
        ).scalars().all()
        assert len(auto_closed_events) == 1
        payload = auto_closed_events[0].payload
        assert payload["drawer_id"] == str(drawer.id)
        assert payload["z_number"] == z.report_number
        assert payload["expected_amount"] == float(z.expected_amount)


async def test_guard_is_a_noop_without_open_drawer(client, auth_headers):
    # Aucune caisse ouverte — ne doit rien faire, ne doit pas lever.
    await run_daily_fiscal_close_guard()
    async with async_session() as db:
        assert (await db.execute(select(ZReport))).scalars().all() == []


async def test_guard_failure_is_journaled_to_jet(open_drawer, monkeypatch):
    async def _boom(self, user_id):
        raise RuntimeError("panne simulee")

    monkeypatch.setattr("app.services.fiscal.FiscalService.close_open_drawers", _boom)

    await run_daily_fiscal_close_guard()  # ne doit pas lever (avale + journalise)

    async with async_session() as db:
        events = (
            await db.execute(select(JournalEvent).where(JournalEvent.event_type == "system.job_failed"))
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["job"] == "daily_fiscal_close_guard"
    assert "panne simulee" in events[0].payload["error"]
