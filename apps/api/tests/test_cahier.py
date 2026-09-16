# Nouveau test (PR11, §3 de docs/ARCHITECTURE_PR11.md) — cahier du jour (M2).
#
# Les ventes passent TOUJOURS par la vraie route POS (chaine fiscale signee,
# caisse ouverte) : jamais d'INSERT direct dans `transactions`. Les journees
# passees ne sont pas antidatees non plus — une transaction signee est
# immuable, `created_at` comprise : c'est la DATE CONSULTEE qui bouge.
import json
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.cahier_day import CahierDay
from app.models.jet import JournalEvent
from app.services.cahier import (
    compute_daily_target,
    daily_target_for,
    open_days_in_month,
    weather_snapshot_for,
)
from app.services.cahier import paris_today

pytestmark = pytest.mark.anyio

ALL_OPEN = [True] * 7
CLOSED_SUNDAY = [True, True, True, True, True, True, False]


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


async def _set_targets(client, auth_headers, *, daily="0.00", monthly=None):
    r = await client.put(
        "/api/admin/settings/targets",
        json={"daily": daily, "monthly": monthly or {}},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text


async def _set_weekday_open(client, auth_headers, opens):
    r = await client.put(
        "/api/cahier/config", json={"weekday_open": opens}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    return r.json()["weekday_open"]


async def _get_day(client, auth_headers, day: str):
    r = await client.get(f"/api/cahier/{day}", headers=auth_headers)
    assert r.status_code == 200, r.text
    return r.json()


def _today() -> date:
    return paris_today()


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


# ---------------------------------------------------------------------------
# Objectif du jour (formule plate)
# ---------------------------------------------------------------------------


def test_daily_target_is_monthly_divided_by_open_days():
    # Mars 2026 : 31 jours, 5 dimanches (1, 8, 15, 22, 29) -> 26 jours ouverts.
    day = date(2026, 3, 10)
    assert open_days_in_month(day, CLOSED_SUNDAY) == 26

    target = compute_daily_target(day, {"monthly": {"2026-03": "2600.00"}}, CLOSED_SUNDAY)
    assert target == Decimal("100.00")


def test_closed_day_has_no_target():
    sunday = date(2026, 3, 8)
    assert sunday.weekday() == 6
    target = compute_daily_target(
        sunday, {"monthly": {"2026-03": "2600.00"}, "daily": "80.00"}, CLOSED_SUNDAY
    )
    assert target == Decimal("0.00")


def test_falls_back_to_daily_target_without_monthly():
    day = date(2026, 3, 10)
    assert compute_daily_target(day, {"daily": "80.00"}, CLOSED_SUNDAY) == Decimal("80.00")
    # Ni mensuel ni journalier : aucun objectif du tout (l'ecran invite a en
    # fixer un, il n'affiche pas une barre vers zero).
    assert compute_daily_target(day, {}, CLOSED_SUNDAY) is None


async def test_daily_target_for_reads_the_settings(client, auth_headers):
    today = _today()
    await _set_weekday_open(client, auth_headers, ALL_OPEN)
    await _set_targets(
        client, auth_headers, monthly={_month_key(today): "3000.00"}
    )

    async with async_session() as db:
        target = await daily_target_for(db, today)
    expected = Decimal("3000.00") / open_days_in_month(today, ALL_OPEN)
    assert target == expected.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Figeage de l'objectif a la premiere lecture
# ---------------------------------------------------------------------------


async def test_target_is_frozen_on_first_read(client, auth_headers):
    today = _today().isoformat()
    await _set_targets(client, auth_headers, daily="120.00")

    first = await _get_day(client, auth_headers, today)
    assert first["target"]["daily"] == "120.00"

    # L'objectif change APRES la premiere lecture : la journee deja ouverte
    # garde le sien, sinon la progression affichee ce matin ne serait plus
    # celle relue ce soir.
    await _set_targets(client, auth_headers, daily="999.00")
    second = await _get_day(client, auth_headers, today)
    assert second["target"]["daily"] == "120.00"

    async with async_session() as db:
        row = (
            await db.execute(select(CahierDay).where(CahierDay.day == _today()))
        ).scalar_one()
    assert row.frozen_daily_target == Decimal("120.00")


async def test_closed_day_shows_no_target(client, auth_headers):
    today = _today()
    # On ferme le jour de la semaine qui est justement aujourd'hui.
    opens = [True] * 7
    opens[today.weekday()] = False
    await _set_weekday_open(client, auth_headers, opens)
    await _set_targets(client, auth_headers, monthly={_month_key(today): "3000.00"})

    body = await _get_day(client, auth_headers, today.isoformat())
    assert body["is_open"] is False
    assert body["target"]["daily"] is None
    assert body["realized"]["progress_pct"] is None


# ---------------------------------------------------------------------------
# Lecture d'une journee
# ---------------------------------------------------------------------------


async def test_day_shape_and_realized(client, auth_headers, open_drawer):
    today = _today()
    await _set_targets(client, auth_headers, daily="100.00")
    await _sell_cash(client, auth_headers, "40.00")
    await _sell_cash(client, auth_headers, "60.00")

    body = await _get_day(client, auth_headers, today.isoformat())

    assert body["day"] == today.isoformat()
    assert body["weekday"] == today.weekday()
    assert body["is_today"] is True
    assert body["is_past"] is False
    assert body["realized"]["net"] == "100.00"
    assert body["realized"]["sales_count"] == 2
    assert body["realized"]["average_basket"] == "50.00"
    assert body["realized"]["progress_pct"] == 100.0
    assert len(body["realized"]["by_hour"]) == 24
    assert sum(Decimal(h["net"]) for h in body["realized"]["by_hour"]) == Decimal("100.00")
    assert body["target"]["month_realized"] == "100.00"
    assert body["message"] is None
    assert body["operation"] is None
    assert body["signatures"] == {"manager": None, "team": None}
    # Pas de vente l'an dernier : on ne compare pas a un zero fictif.
    assert body["previous_year"] is None


async def test_empty_day_is_all_zero(client, auth_headers):
    body = await _get_day(client, auth_headers, _today().isoformat())
    assert body["realized"]["net"] == "0.00"
    assert body["realized"]["sales_count"] == 0
    assert body["realized"]["average_basket"] == "0.00"
    assert body["target"]["month_realized"] == "0.00"


async def test_month_situation(client, auth_headers, open_drawer):
    today = _today()
    await _set_weekday_open(client, auth_headers, ALL_OPEN)
    await _set_targets(client, auth_headers, monthly={_month_key(today): "1000.00"})
    await _sell_cash(client, auth_headers, "250.00")

    body = await _get_day(client, auth_headers, today.isoformat())
    assert body["target"]["monthly"] == "1000.00"
    assert body["target"]["month_realized"] == "250.00"
    assert body["target"]["month_progress_pct"] == 25.0
    assert body["target"]["month_remaining"] == "750.00"
    assert Decimal(body["target"]["required_daily_rest_of_month"]) > Decimal("0")


async def test_invalid_day_is_refused(client, auth_headers):
    r = await client.get("/api/cahier/2026-13-45", headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_date"


async def test_cahier_requires_authentication(client):
    assert (await client.get(f"/api/cahier/{_today().isoformat()}")).status_code == 401


# ---------------------------------------------------------------------------
# Textes libres
# ---------------------------------------------------------------------------


async def test_text_is_saved_field_by_field(client, auth_headers):
    today = _today().isoformat()
    r = await client.put(
        f"/api/cahier/{today}/text", json={"message": "Vitrine refaite"}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["message"] == "Vitrine refaite"

    # Un champ ABSENT n'efface pas l'autre (sauvegarde au blur, champ par champ).
    r = await client.put(
        f"/api/cahier/{today}/text",
        json={"operation": "-20 % sur les manteaux"},
        headers=auth_headers,
    )
    body = r.json()
    assert body["message"] == "Vitrine refaite"
    assert body["operation"] == "-20 % sur les manteaux"

    # Un champ vide, lui, efface.
    r = await client.put(
        f"/api/cahier/{today}/text", json={"message": "   "}, headers=auth_headers
    )
    assert r.json()["message"] is None


async def test_text_on_past_day_is_refused(client, auth_headers):
    yesterday = (_today() - timedelta(days=1)).isoformat()
    r = await client.put(
        f"/api/cahier/{yesterday}/text", json={"message": "trop tard"}, headers=auth_headers
    )
    assert r.status_code == 409
    assert r.json()["code"] == "day_closed"


async def test_text_is_capped_at_500_characters(client, auth_headers):
    today = _today().isoformat()
    r = await client.put(
        f"/api/cahier/{today}/text", json={"message": "a" * 501}, headers=auth_headers
    )
    assert r.status_code == 422


async def test_jet_never_receives_the_text(client, auth_headers):
    today = _today().isoformat()
    secret = "Madame Dupont repasse jeudi"
    r = await client.put(
        f"/api/cahier/{today}/text",
        json={"message": secret, "operation": "braderie"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "cahier.text_updated")
            )
        ).scalars().all()
    assert len(events) == 1
    payload = events[0].payload
    assert payload["day"] == today
    assert payload["fields"] == ["message", "operation"]
    # Le journal est immuable : un texte libre qui y tomberait ne pourrait
    # plus jamais en sortir.
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert "braderie" not in json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------


async def test_manager_signature_then_already_signed(client, auth_headers, manager):
    today = _today().isoformat()
    r = await client.put(
        f"/api/cahier/{today}/signature", json={"role": "manager"}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    signature = r.json()["signatures"]["manager"]
    assert signature["username"] == manager.username
    assert signature["at"]

    again = await client.put(
        f"/api/cahier/{today}/signature", json={"role": "manager"}, headers=auth_headers
    )
    assert again.status_code == 409
    assert again.json()["code"] == "already_signed"


async def test_team_signature_without_cashier_needs_a_name(client, auth_headers):
    today = _today().isoformat()
    refused = await client.put(
        f"/api/cahier/{today}/signature", json={"role": "team"}, headers=auth_headers
    )
    assert refused.status_code == 422
    assert refused.json()["code"] == "name_required"

    signed = await client.put(
        f"/api/cahier/{today}/signature",
        json={"role": "team", "name": "Léa"},
        headers=auth_headers,
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["signatures"]["team"]["name"] == "Léa"


async def test_team_signature_uses_the_drawer_cashier(client, auth_headers, open_drawer):
    created = await client.post(
        "/api/admin/cashiers",
        json={"display_name": "Camille", "pin": "4821"},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    cashier = created.json()["cashier"]
    identified = await client.post(
        "/api/pos/cashiers/identify",
        json={"cashier_id": cashier["id"], "pin": "4821"},
        headers=auth_headers,
    )
    assert identified.status_code == 200, identified.text

    today = _today().isoformat()
    signed = await client.put(
        f"/api/cahier/{today}/signature",
        # Le nom saisi est ignore : c'est la vendeuse en caisse qui signe.
        json={"role": "team", "name": "quelqu'un d'autre"},
        headers=auth_headers,
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["signatures"]["team"]["name"] == "Camille"

    async with async_session() as db:
        row = (
            await db.execute(select(CahierDay).where(CahierDay.day == _today()))
        ).scalar_one()
    assert str(row.team_signed_by_cashier_id) == cashier["id"]


async def test_signature_on_past_day_is_refused(client, auth_headers):
    yesterday = (_today() - timedelta(days=1)).isoformat()
    r = await client.put(
        f"/api/cahier/{yesterday}/signature", json={"role": "manager"}, headers=auth_headers
    )
    assert r.status_code == 409
    assert r.json()["code"] == "day_closed"


async def test_jet_records_the_signature_without_any_name(client, auth_headers):
    today = _today().isoformat()
    await client.put(
        f"/api/cahier/{today}/signature",
        json={"role": "team", "name": "Léa"},
        headers=auth_headers,
    )
    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "cahier.signed")
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload == {"day": today, "role": "team"}


# ---------------------------------------------------------------------------
# Instantane meteo
# ---------------------------------------------------------------------------


async def test_weather_snapshot_is_frozen_on_first_read(client, auth_headers, monkeypatch):
    async def _sunny(db):
        return {
            "unavailable": False,
            "description": "ciel dégagé",
            "temp": 21.0,
            "icon": "01d",
            "city": "la boutique",
            "fetched_at": "2026-09-16T08:00:00+00:00",
        }

    monkeypatch.setattr("app.services.weather.get_current", _sunny)
    today = _today().isoformat()
    first = await _get_day(client, auth_headers, today)
    assert first["weather"]["description"] == "ciel dégagé"

    async def _rainy(db):
        return {"unavailable": False, "description": "pluie modérée", "temp": 12.0}

    monkeypatch.setattr("app.services.weather.get_current", _rainy)
    second = await _get_day(client, auth_headers, today)
    # La meteo du matin est celle du cahier : elle ne se reecrit pas a chaque
    # rechargement de la page.
    assert second["weather"]["description"] == "ciel dégagé"

    async with async_session() as db:
        stored = await weather_snapshot_for(db, _today())
    assert stored["description"] == "ciel dégagé"


async def test_unavailable_weather_is_not_frozen(client, auth_headers, monkeypatch):
    async def _down(db):
        return {"unavailable": True, "reason": "service météo injoignable"}

    monkeypatch.setattr("app.services.weather.get_current", _down)
    today = _today().isoformat()
    body = await _get_day(client, auth_headers, today)
    assert body["weather"]["unavailable"] is True

    async with async_session() as db:
        assert await weather_snapshot_for(db, _today()) is None

    async def _sunny(db):
        return {"unavailable": False, "description": "ciel dégagé"}

    monkeypatch.setattr("app.services.weather.get_current", _sunny)
    later = await _get_day(client, auth_headers, today)
    assert later["weather"]["description"] == "ciel dégagé"


async def test_past_day_has_no_weather(client, auth_headers, monkeypatch):
    async def _sunny(db):
        return {"unavailable": False, "description": "ciel dégagé"}

    monkeypatch.setattr("app.services.weather.get_current", _sunny)
    yesterday = (_today() - timedelta(days=1)).isoformat()
    body = await _get_day(client, auth_headers, yesterday)
    # On ne colle jamais la meteo d'aujourd'hui sur une journee passee.
    assert body["weather"] is None


# ---------------------------------------------------------------------------
# Jours d'ouverture
# ---------------------------------------------------------------------------


async def test_config_defaults_to_closed_sunday(client, auth_headers):
    r = await client.get("/api/cahier/config", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["weekday_open"] == CLOSED_SUNDAY


async def test_config_is_saved(client, auth_headers):
    opens = [True, True, True, True, True, False, False]
    assert await _set_weekday_open(client, auth_headers, opens) == opens
    r = await client.get("/api/cahier/config", headers=auth_headers)
    assert r.json()["weekday_open"] == opens


async def test_config_refuses_a_short_week(client, auth_headers):
    r = await client.put(
        "/api/cahier/config", json={"weekday_open": [True] * 6}, headers=auth_headers
    )
    assert r.status_code == 422
