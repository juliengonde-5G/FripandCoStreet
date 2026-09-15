# Nouveau test (PR6, §3 de docs/ARCHITECTURE_PR6.md) — service `reporting.py`
# (H2) et route `GET /api/reports/dashboard` (H3).
#
# Les ventes sont TOUJOURS creees par la vraie route POS (chaine fiscale
# signee, caisse ouverte, annulation miroir) : jamais d'INSERT direct dans
# `transactions`, qui produirait des lignes non signees et fausserait aussi
# bien les agregats que l'esprit du contrat. La frontiere de journee est
# testee par l'instant de reference injecte (`now=`), pas en antidatant des
# ventes — une transaction signee est immuable, `created_at` comprise.
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.database import async_session
from app.services.pos import _PARIS
from app.services.reporting import dashboard, serialize_dashboard

pytestmark = pytest.mark.anyio


async def _sell_cash(client, auth_headers, amount: str, *, label: str = "Robe") -> dict:
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": label, "unit_price": amount, "quantity": 1}],
            "payments": [{"method": "cash", "amount": amount, "tendered_amount": amount}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _cancel(client, auth_headers, sale: dict) -> dict:
    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel",
        json={"reason": "cliente insatisfaite"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _fake_card_verify(monkeypatch) -> None:
    async def _verify(db, tender, client_uuid):
        return SimpleNamespace(
            sumup_checkout_id=tender.checkout_id,
            sumup_transaction_id="TXN-ABC",
            sumup_transaction_code="CODE-1",
            sumup_auth_code="000000",
            sumup_card_brand="VISA",
            sumup_card_last4="4242",
        )

    monkeypatch.setattr("app.services.pos.verify_card_tender", _verify)


async def _dashboard(now: datetime | None = None) -> dict:
    async with async_session() as db:
        return await dashboard(db, now=now)


# ---------------------------------------------------------------------------
# Service (H2)
# ---------------------------------------------------------------------------


async def test_empty_day_is_all_zero():
    data = await _dashboard()

    assert data["today"]["net"] == Decimal("0.00")
    assert data["today"]["sales_count"] == 0
    assert data["today"]["refunds_count"] == 0
    assert data["today"]["average_basket"] == Decimal("0.00")
    assert data["today"]["cash"] == Decimal("0.00")
    assert data["today"]["card"] == Decimal("0.00")
    assert data["today"]["target"] == Decimal("0.00")
    assert data["today"]["progress_pct"] == 0.0
    assert data["month"]["net"] == Decimal("0.00")
    assert data["month"]["days_open"] == 0
    assert data["month"]["required_daily"] == Decimal("0.00")
    assert data["month"]["best_day"] == {"date": None, "net": Decimal("0.00")}
    assert [entry["net"] for entry in data["last_7_days"]] == [Decimal("0.00")] * 7


async def test_net_excludes_cancelled_sale_from_average_basket(
    client, auth_headers, open_drawer
):
    cancelled = await _sell_cash(client, auth_headers, "25.00")
    await _sell_cash(client, auth_headers, "12.00")
    await _sell_cash(client, auth_headers, "10.00")
    await _cancel(client, auth_headers, cancelled)

    today = (await _dashboard())["today"]

    # 25 + 12 + 10 - 25 (annulation miroir)
    assert today["net"] == Decimal("22.00")
    # La vente annulee ne compte pas comme un panier.
    assert today["sales_count"] == 2
    assert today["refunds_count"] == 1
    assert today["average_basket"] == Decimal("11.00")
    # L'annulation rend aussi les especes : 47 encaisses - 25 rendus.
    assert today["cash"] == Decimal("22.00")


async def test_mixed_payment_split_cash_and_card(
    client, auth_headers, open_drawer, monkeypatch
):
    _fake_card_verify(monkeypatch)
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Manteau", "unit_price": "50.00", "quantity": 1}],
            "payments": [
                {"method": "cash", "amount": "20.00", "tendered_amount": "20.00"},
                {"method": "card", "amount": "30.00", "checkout_id": "chk_mix"},
            ],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text

    today = (await _dashboard())["today"]
    assert today["net"] == Decimal("50.00")
    assert today["cash"] == Decimal("20.00")
    assert today["card"] == Decimal("30.00")


async def test_cancelled_card_sale_empties_the_card_split(
    client, auth_headers, open_drawer, monkeypatch
):
    _fake_card_verify(monkeypatch)
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Robe", "unit_price": "30.00", "quantity": 1}],
            "payments": [{"method": "card", "amount": "30.00", "checkout_id": "chk_1"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text

    async def _refund_ok(sumup_transaction_id, amount):
        return {"ok": True}

    monkeypatch.setattr("app.services.refund.refund_card_payment", _refund_ok)
    await _cancel(client, auth_headers, r.json())

    today = (await _dashboard())["today"]
    assert today["net"] == Decimal("0.00")
    assert today["card"] == Decimal("0.00")
    assert today["sales_count"] == 0
    assert today["refunds_count"] == 1


async def test_civil_day_follows_paris_not_utc():
    """23:30 a Paris appartient a la journee J, 00:30 a la journee J+1 —
    meme quand l'instant UTC correspondant est encore, ou deja, un autre
    jour calendaire."""
    # 22:30 UTC le 15/03 = 23:30 a Paris le 15/03 (heure d'hiver, UTC+1).
    data = await _dashboard(datetime(2026, 3, 15, 22, 30, tzinfo=timezone.utc))
    assert data["today"]["date"] == date(2026, 3, 15)

    # 23:30 UTC le 15/03 = 00:30 a Paris le 16/03 : jour suivant cote boutique.
    data = await _dashboard(datetime(2026, 3, 15, 23, 30, tzinfo=timezone.utc))
    assert data["today"]["date"] == date(2026, 3, 16)

    # Et en heure d'ete (UTC+2), 22:30 UTC le 15/07 est deja le 16/07 a Paris.
    data = await _dashboard(datetime(2026, 7, 15, 22, 30, tzinfo=timezone.utc))
    assert data["today"]["date"] == date(2026, 7, 16)


async def test_sale_of_today_falls_out_of_today_tomorrow(client, auth_headers, open_drawer):
    """La meme vente, vue depuis le lendemain, quitte « aujourd'hui » mais
    reste dans la serie 7 jours a sa date."""
    await _sell_cash(client, auth_headers, "18.00")
    sold_on = datetime.now(_PARIS).date()

    tomorrow = await _dashboard(datetime.now(_PARIS) + timedelta(days=1))
    assert tomorrow["today"]["net"] == Decimal("0.00")
    assert tomorrow["today"]["sales_count"] == 0

    series = {entry["date"]: entry for entry in tomorrow["last_7_days"]}
    assert series[sold_on]["net"] == Decimal("18.00")
    assert series[sold_on]["sales_count"] == 1


async def test_last_7_days_always_has_7_ordered_entries(client, auth_headers, open_drawer):
    await _sell_cash(client, auth_headers, "9.90")
    data = await _dashboard()

    series = data["last_7_days"]
    assert len(series) == 7
    expected = [
        datetime.now(_PARIS).date() - timedelta(days=offset) for offset in range(6, -1, -1)
    ]
    assert [entry["date"] for entry in series] == expected
    assert series[-1]["date"] == data["today"]["date"]
    assert series[-1]["net"] == Decimal("9.90")


async def test_month_required_daily_and_remaining_days(client, auth_headers):
    """Mois sans vente, objectif fixe, `now` fige : le rythme necessaire est
    l'objectif restant divise par les jours calendaires restants, jour de
    reference inclus (avril 2026 : 30 jours, le 10 -> 21 jours restants)."""
    r = await client.put(
        "/api/admin/settings/targets",
        json={"daily": "200", "monthly": {"2026-04": "3000"}},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    month = (await _dashboard(datetime(2026, 4, 10, 12, 0, tzinfo=_PARIS)))["month"]
    assert month["month"] == "2026-04"
    assert month["target"] == Decimal("3000.00")
    assert month["net"] == Decimal("0.00")
    assert month["remaining_days"] == 21
    assert month["required_daily"] == Decimal("142.86")  # 3000 / 21
    assert month["days_open"] == 0
    assert month["progress_pct"] == 0.0


async def test_month_default_target_is_inherited(client, auth_headers):
    r = await client.put(
        "/api/admin/settings/targets",
        json={"daily": "0", "monthly": {"default": "2000", "2026-04": "3000"}},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    avril = (await _dashboard(datetime(2026, 4, 10, 12, 0, tzinfo=_PARIS)))["month"]
    assert avril["target"] == Decimal("3000.00")

    mai = (await _dashboard(datetime(2026, 5, 10, 12, 0, tzinfo=_PARIS)))["month"]
    assert mai["target"] == Decimal("2000.00")


async def test_month_aggregates_and_best_day(client, auth_headers, open_drawer):
    await _sell_cash(client, auth_headers, "30.00")
    await _sell_cash(client, auth_headers, "20.00")

    today = datetime.now(_PARIS).date()
    month = (await _dashboard())["month"]
    assert month["month"] == f"{today.year:04d}-{today.month:02d}"
    assert month["net"] == Decimal("50.00")
    assert month["sales_count"] == 2
    assert month["days_open"] == 1
    assert month["best_day"] == {"date": today, "net": Decimal("50.00")}


async def test_progress_pct_uses_daily_target(client, auth_headers, open_drawer):
    r = await client.put(
        "/api/admin/settings/targets",
        json={"daily": "100", "monthly": {}},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    await _sell_cash(client, auth_headers, "25.50")
    today = (await _dashboard())["today"]
    assert today["target"] == Decimal("100.00")
    assert today["progress_pct"] == 25.5


# ---------------------------------------------------------------------------
# Route (H3)
# ---------------------------------------------------------------------------


async def test_dashboard_requires_jwt(client):
    r = await client.get("/api/reports/dashboard")
    assert r.status_code == 401


async def test_dashboard_response_matches_contract(client, auth_headers, open_drawer):
    await _sell_cash(client, auth_headers, "25.00")

    r = await client.get("/api/reports/dashboard", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()

    assert set(body) == {"generated_at", "today", "month", "last_7_days"}
    assert set(body["today"]) == {
        "date",
        "sales_count",
        "refunds_count",
        "net",
        "average_basket",
        "cash",
        "card",
        "target",
        "progress_pct",
    }
    assert set(body["month"]) == {
        "month",
        "net",
        "sales_count",
        "target",
        "progress_pct",
        "days_open",
        "remaining_days",
        "required_daily",
        "best_day",
    }
    assert set(body["month"]["best_day"]) == {"date", "net"}

    for key in ("net", "average_basket", "cash", "card", "target"):
        assert body["today"][key] == f"{Decimal(body['today'][key]):.2f}"
    assert body["today"]["net"] == "25.00"
    assert body["today"]["average_basket"] == "25.00"
    assert body["today"]["cash"] == "25.00"
    assert body["today"]["card"] == "0.00"
    assert isinstance(body["today"]["progress_pct"], float)

    assert len(body["last_7_days"]) == 7
    assert set(body["last_7_days"][0]) == {"date", "net", "sales_count"}
    assert body["last_7_days"][-1]["date"] == body["today"]["date"]
    assert body["month"]["best_day"]["net"] == "25.00"

    # Aucune valeur nulle hors `best_day.date` d'un mois sans vente.
    assert None not in body["today"].values()
    assert None not in body["last_7_days"][0].values()


async def test_dashboard_date_query_moves_today(client, auth_headers, open_drawer):
    await _sell_cash(client, auth_headers, "25.00")
    yesterday = (datetime.now(_PARIS).date() - timedelta(days=1)).isoformat()

    r = await client.get(
        f"/api/reports/dashboard?date={yesterday}", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["today"]["date"] == yesterday
    assert body["today"]["net"] == "0.00"
    assert body["last_7_days"][-1]["date"] == yesterday


async def test_dashboard_rejects_malformed_date(client, auth_headers):
    r = await client.get("/api/reports/dashboard?date=15-09-2026", headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_date"


async def test_serialize_dashboard_handles_empty_best_day():
    data = await _dashboard(datetime(2026, 4, 10, 12, 0, tzinfo=_PARIS))
    body = serialize_dashboard(data)
    assert body["month"]["best_day"] == {"date": None, "net": "0.00"}
