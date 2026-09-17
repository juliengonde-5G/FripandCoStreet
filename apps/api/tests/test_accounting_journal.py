# Nouveau test (PR12, docs/ARCHITECTURE_PR12.md §3, N1) — journal comptable
# consultable : lignes relues depuis l'ecriture enregistree quand elle
# existe, recalculees a la volee sinon, totaux equilibres, filtre compte,
# garde-fou de periode.
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core.database import async_session
from app.models.pos import ZReport
from app.services.accounting_service import AccountingService

pytestmark = pytest.mark.anyio


def _uid() -> str:
    return str(uuid.uuid4())


async def _sale_and_close(client, auth_headers, *, amount: str, counted: str) -> dict:
    """Une journee complete : ouverture 100,00, une vente especes, cloture."""
    r = await client.post(
        "/api/pos/drawer/open", json={"opening_amount": "100.00"}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _uid(),
            "items": [{"label": "Robe", "unit_price": amount, "quantity": 1}],
            "payments": [{"method": "cash", "amount": amount, "tendered_amount": amount}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/pos/drawer/close", json={"closing_amount": counted}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _keep_export_only_for(z_number: int) -> None:
    """Ne laisse d'ecriture enregistree que pour un seul Z.

    Les ecritures comptables sont immuables (trigger NF525) : impossible de
    supprimer une ligne par un DELETE ordinaire — c'est justement ce que
    `tests/test_triggers.py` verifie. On vide donc les deux tables par
    TRUNCATE (seul chemin qui contourne les triggers ROW, comme le fixture
    d'isolation de `conftest.py`) puis on recree l'ecriture du Z a
    conserver, exactement comme la cloture l'aurait fait. On obtient l'etat
    reel a reproduire : un Z anterieur a la mise en place des exports.
    """
    async with async_session() as db:
        await db.execute(
            text("TRUNCATE accounting_exports, accounting_export_lines CASCADE")
        )
        z = (
            await db.execute(select(ZReport).where(ZReport.report_number == z_number))
        ).scalar_one()
        await AccountingService(db).create_export_for_z(z)
        await db.commit()


async def _journal(client, auth_headers, **params) -> dict:
    today = datetime.now(timezone.utc).date()
    query = {"from": today.isoformat(), "to": today.isoformat(), **params}
    r = await client.get(
        "/api/admin/accounting/journal", params=query, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_journal_reads_stored_export_and_recomputes_the_other(
    client, auth_headers
):
    """Deux Z : le premier garde son ecriture enregistree (`source="export"`),
    le second n'en a plus (`source="computed"` + `z_without_export`)."""
    await _sale_and_close(client, auth_headers, amount="40.00", counted="140.00")
    await _sale_and_close(client, auth_headers, amount="25.00", counted="125.00")
    await _keep_export_only_for(1)

    body = await _journal(client, auth_headers)

    assert body["period"]["from"] == datetime.now(timezone.utc).date().isoformat()
    assert body["z_without_export"] == [2]

    sources = {ln["z_report_number"]: ln["source"] for ln in body["lines"]}
    assert sources[1] == "export"
    assert sources[2] == "computed"

    # Les deux Z produisent les memes comptes (531000 especes, 707100 ventes,
    # 44571 TVA) : la relecture et le recalcul disent la meme chose.
    accounts_z1 = {ln["account_number"] for ln in body["lines"] if ln["z_report_number"] == 1}
    accounts_z2 = {ln["account_number"] for ln in body["lines"] if ln["z_report_number"] == 2}
    assert accounts_z1 == accounts_z2
    assert "531000" in accounts_z1

    # Tri : date, puis n° de Z, puis n° de compte.
    keys = [(ln["date"], ln["z_report_number"], ln["account_number"]) for ln in body["lines"]]
    assert keys == sorted(keys)


async def test_journal_totals_are_balanced_strings(client, auth_headers):
    await _sale_and_close(client, auth_headers, amount="40.00", counted="140.00")
    body = await _journal(client, auth_headers)

    assert body["totals"]["balanced"] is True
    assert body["totals"]["debit"] == body["totals"]["credit"]
    # Montants en chaines a deux decimales, jamais des flottants.
    for line in body["lines"]:
        assert isinstance(line["debit"], str)
        assert line["debit"].count(".") == 1
        assert len(line["debit"].split(".")[1]) == 2

    debit = sum(Decimal(ln["debit"]) for ln in body["lines"])
    assert Decimal(body["totals"]["debit"]) == debit

    # Le debit especes vaut le montant encaisse.
    cash = [ln for ln in body["lines"] if ln["account_number"] == "531000"]
    assert cash and cash[0]["debit"] == "40.00"


async def test_journal_accounts_aggregate_two_z(client, auth_headers):
    await _sale_and_close(client, auth_headers, amount="40.00", counted="140.00")
    await _sale_and_close(client, auth_headers, amount="60.00", counted="160.00")
    body = await _journal(client, auth_headers)

    cash = next(a for a in body["accounts"] if a["account_number"] == "531000")
    assert cash["debit"] == "100.00"
    assert cash["credit"] == "0.00"
    numbers = [a["account_number"] for a in body["accounts"]]
    assert numbers == sorted(numbers)


async def test_journal_account_filter_is_a_prefix(client, auth_headers):
    await _sale_and_close(client, auth_headers, amount="40.00", counted="140.00")
    body = await _journal(client, auth_headers, account="53")
    assert body["lines"]
    assert {ln["account_number"] for ln in body["lines"]} == {"531000"}
    # Un sous-ensemble filtre n'est evidemment plus equilibre : la pastille
    # reflete ce qui est affiche.
    assert body["totals"]["balanced"] is False


async def test_journal_z_filter(client, auth_headers):
    await _sale_and_close(client, auth_headers, amount="40.00", counted="140.00")
    await _sale_and_close(client, auth_headers, amount="60.00", counted="160.00")
    body = await _journal(client, auth_headers, z=2)
    assert {ln["z_report_number"] for ln in body["lines"]} == {2}


async def test_journal_defaults_to_current_month(client, auth_headers):
    await _sale_and_close(client, auth_headers, amount="40.00", counted="140.00")
    r = await client.get("/api/admin/accounting/journal", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    today = datetime.now(timezone.utc).date()
    assert body["period"]["from"] == today.replace(day=1).isoformat()
    assert body["period"]["to"] == today.isoformat()
    assert body["lines"]


async def test_journal_period_outside_range_is_empty(client, auth_headers):
    await _sale_and_close(client, auth_headers, amount="40.00", counted="140.00")
    past = date(2020, 1, 1)
    r = await client.get(
        "/api/admin/accounting/journal",
        params={"from": past.isoformat(), "to": past.isoformat()},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lines"] == []
    assert body["accounts"] == []
    assert body["z_without_export"] == []
    assert body["totals"] == {"debit": "0.00", "credit": "0.00", "balanced": True}


async def test_journal_rejects_invalid_date(client, auth_headers):
    r = await client.get(
        "/api/admin/accounting/journal",
        params={"from": "2026-13-01", "to": "2026-13-02"},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_date"


async def test_journal_rejects_too_long_period(client, auth_headers):
    start = date(2025, 1, 1)
    r = await client.get(
        "/api/admin/accounting/journal",
        params={"from": start.isoformat(), "to": (start + timedelta(days=400)).isoformat()},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "period_too_long"


async def test_journal_rejects_reversed_period(client, auth_headers):
    r = await client.get(
        "/api/admin/accounting/journal",
        params={"from": "2026-02-10", "to": "2026-02-01"},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_date"


async def test_journal_requires_authentication(client):
    r = await client.get("/api/admin/accounting/journal")
    assert r.status_code == 401
