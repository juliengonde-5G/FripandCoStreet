# Nouveau test (PR4, docs/ARCHITECTURE_PR4.md §7, F5) — crons de cloture
# periodique automatique (mensuelle 1er 00:15, annuelle 1er janvier 00:30,
# Europe/Paris) : succes idempotent, echec journalise au JET
# `system.job_failed` (jamais avale en silence, S-5).
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.jobs import (
    JOB_ANNUAL_FISCAL_CLOSURE,
    JOB_MONTHLY_FISCAL_CLOSURE,
    run_annual_fiscal_closure,
    run_monthly_fiscal_closure,
)
from app.models.fiscal_closure import ClosureType, FiscalClosure
from app.models.jet import JournalEvent

pytestmark = pytest.mark.anyio


async def test_monthly_closure_job_succeeds_and_is_idempotent():
    await run_monthly_fiscal_closure()
    await run_monthly_fiscal_closure()  # rejouable sans erreur (periode deja close)

    async with async_session() as db:
        rows = (await db.execute(select(FiscalClosure))).scalars().all()
        failed = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "system.job_failed")
            )
        ).scalars().all()
    assert len(rows) == 1  # pas de doublon au deuxieme appel
    assert rows[0].closure_type.value == "monthly"
    assert failed == []


async def test_annual_closure_job_succeeds():
    await run_annual_fiscal_closure()

    async with async_session() as db:
        rows = (
            await db.execute(
                select(FiscalClosure).where(FiscalClosure.closure_type == ClosureType.annual)
            )
        ).scalars().all()
    assert len(rows) == 1


async def test_monthly_closure_job_failure_is_journaled(monkeypatch):
    async def _boom(self, **kwargs):
        raise RuntimeError("panne simulée clôture mensuelle")

    monkeypatch.setattr(
        "app.services.fiscal_closure.FiscalClosureService.close_period", _boom
    )

    await run_monthly_fiscal_closure()  # ne doit pas lever

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "system.job_failed")
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["job"] == JOB_MONTHLY_FISCAL_CLOSURE
    assert "panne simulée" in events[0].payload["error"]


async def test_annual_closure_job_failure_is_journaled(monkeypatch):
    async def _boom(self, **kwargs):
        raise RuntimeError("panne simulée clôture annuelle")

    monkeypatch.setattr(
        "app.services.fiscal_closure.FiscalClosureService.close_period", _boom
    )

    await run_annual_fiscal_closure()

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "system.job_failed")
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["job"] == JOB_ANNUAL_FISCAL_CLOSURE
