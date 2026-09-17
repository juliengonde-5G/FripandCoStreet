# Nouveau test (PR11, §3 de docs/ARCHITECTURE_PR11.md) — service
# `reports.py` (M1) et routes `GET /api/reports/{daily,weekly,monthly}`.
#
# Comme pour `test_reporting.py`, les ventes sont TOUJOURS creees par la
# vraie route POS : jamais d'INSERT direct dans `transactions`, qui
# produirait des lignes non signees et fausserait aussi bien les agregats
# que l'esprit du contrat.
#
# Consequence directe de l'immuabilite fiscale : `created_at` est dans le
# payload signe ET gele par le trigger `fripco_protect_signed_transaction`
# (migration 0002) — on ne peut donc PAS antidater un jeu d'essai sur trois
# jours. Les periodes multi-jours sont donc exercees comme la boutique les
# vit : le meme jeu d'essai relu par des fenetres differentes (la veille,
# le jour, le lendemain, la semaine, le mois), ce qui couvre exactement ce
# qui peut casser — le bornage des periodes, le remplissage des trous et la
# comparaison a la periode precedente.
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.jet import JournalEvent
from app.services.pos import _PARIS
from app.services.reports import (
    DAILY,
    MONTHLY,
    WEEKLY,
    build_report,
    previous_period,
    resolve_period,
    serialize_report,
)

pytestmark = pytest.mark.anyio

PIN_LEA = "7391"
PIN_MANON = "5074"


def _today() -> date:
    """Journee civile de la boutique (Paris), pas celle du serveur."""
    return datetime.now(_PARIS).date()


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


async def _sell_card(client, auth_headers, amount: str, *, label: str = "Manteau") -> dict:
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": label, "unit_price": amount, "quantity": 1}],
            "payments": [{"method": "card", "amount": amount, "checkout_id": "chk_report"}],
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
    """Le serveur relit SumUp avant d'ecrire une vente CB : ici, faux TPE."""

    async def _verify(db, tender, client_uuid):
        return SimpleNamespace(
            sumup_checkout_id=tender.checkout_id,
            sumup_transaction_id="TXN-REPORT",
            sumup_transaction_code="CODE-R",
            sumup_auth_code="000000",
            sumup_card_brand="VISA",
            sumup_card_last4="4242",
        )

    monkeypatch.setattr("app.services.pos.verify_card_tender", _verify)


async def _create_cashier(client, auth_headers, name: str, pin: str) -> dict:
    r = await client.post(
        "/api/admin/cashiers", json={"display_name": name, "pin": pin}, headers=auth_headers
    )
    assert r.status_code == 201, r.text
    return r.json()["cashier"]


async def _identify(client, auth_headers, cashier_id: str, pin: str) -> None:
    r = await client.post(
        "/api/pos/cashiers/identify",
        json={"cashier_id": cashier_id, "pin": pin},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text


async def _report(kind: str, day: date) -> dict:
    async with async_session() as db:
        return serialize_report(await build_report(db, kind, day=day))


async def _set_targets(client, auth_headers, body: dict) -> None:
    r = await client.put("/api/admin/settings/targets", json=body, headers=auth_headers)
    assert r.status_code == 200, r.text


async def _jet_export_payloads() -> list[dict]:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(JournalEvent)
                .where(JournalEvent.event_type == "export.downloaded")
                .order_by(JournalEvent.seq.asc())
            )
        ).scalars().all()
        return [dict(row.payload or {}) for row in rows]


# ---------------------------------------------------------------------------
# Bornage des periodes — fonctions pures
# ---------------------------------------------------------------------------


def test_period_bounds_and_labels():
    # Mardi 15 septembre 2026 -> semaine ISO du lundi 14 au dimanche 20.
    first, last, label = resolve_period(WEEKLY, day=date(2026, 9, 15))
    assert (first, last) == (date(2026, 9, 14), date(2026, 9, 20))
    assert label == "semaine du 14 au 20 septembre 2026"

    first, last, label = resolve_period(MONTHLY, day=date(2026, 2, 17))
    assert (first, last) == (date(2026, 2, 1), date(2026, 2, 28))
    assert label == "février 2026"

    first, last, label = resolve_period(DAILY, day=date(2026, 9, 14))
    assert (first, last) == (date(2026, 9, 14), date(2026, 9, 14))
    assert label == "lundi 14 septembre 2026"


def test_previous_period_is_the_same_period_before():
    assert previous_period(DAILY, date(2026, 9, 14), date(2026, 9, 14)) == (
        date(2026, 9, 13),
        date(2026, 9, 13),
    )
    assert previous_period(WEEKLY, date(2026, 9, 14), date(2026, 9, 20)) == (
        date(2026, 9, 7),
        date(2026, 9, 13),
    )
    # Mois CIVIL precedent, pas « 31 jours avant » : mars se compare a
    # fevrier entier, meme s'il est plus court.
    assert previous_period(MONTHLY, date(2026, 3, 1), date(2026, 3, 31)) == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )


# ---------------------------------------------------------------------------
# Periode vide
# ---------------------------------------------------------------------------


async def test_empty_period_is_zeros_and_empty_lists():
    """Une periode future est acceptee (contrat M1) et rend des zeros."""
    report = await _report(DAILY, _today() + timedelta(days=400))

    assert report["totals"] == {
        "sales_count": 0,
        "refunds_count": 0,
        "gross": "0.00",
        "refunds": "0.00",
        "net": "0.00",
        "average_basket": "0.00",
        "items_count": 0,
    }
    assert report["payments"] == {"cash": "0.00", "card": "0.00"}
    assert report["by_cashier"] == []
    assert report["top_items"] == []
    assert report["z_reports"] == []
    assert report["target"] is None
    assert report["previous"]["delta_pct"] is None
    # 24 heures, toujours : le graphique doit montrer une journee entiere,
    # creux compris.
    assert len(report["by_hour"]) == 24
    assert [row["hour"] for row in report["by_hour"]] == list(range(24))
    assert {row["net"] for row in report["by_hour"]} == {"0.00"}


# ---------------------------------------------------------------------------
# Jeu d'essai connu (§3) : deux vendeuses, deux moyens de paiement,
# une annulation.
# ---------------------------------------------------------------------------


@pytest.fixture
async def dataset(client, auth_headers, open_drawer, monkeypatch) -> dict:
    _fake_card_verify(monkeypatch)
    lea = await _create_cashier(client, auth_headers, "Léa", PIN_LEA)
    manon = await _create_cashier(client, auth_headers, "Manon", PIN_MANON)

    await _identify(client, auth_headers, lea["id"], PIN_LEA)
    await _sell_cash(client, auth_headers, "25.00", label="Robe")
    await _sell_cash(client, auth_headers, "12.00", label="  ROBE  ")
    cancelled = await _sell_cash(client, auth_headers, "40.00", label="Veste")

    await _identify(client, auth_headers, manon["id"], PIN_MANON)
    await _sell_card(client, auth_headers, "30.00", label="Manteau")
    await _cancel(client, auth_headers, cancelled)

    return {"lea": lea, "manon": manon}


async def test_daily_totals_payments_and_items(dataset):
    report = await _report(DAILY, _today())
    totals = report["totals"]

    # 25 + 12 + 40 + 30 encaisses, 40 rendus.
    assert totals["gross"] == "107.00"
    assert totals["refunds"] == "40.00"
    assert totals["net"] == "67.00"
    # La vente annulee n'est pas un panier : 3 ventes retenues sur 4.
    assert totals["sales_count"] == 3
    assert totals["refunds_count"] == 1
    assert totals["average_basket"] == "22.33"
    # Articles des ventes NON annulees seulement (la veste ne compte pas).
    assert totals["items_count"] == 3
    # Les especes rendues par l'annulation sortent de la ventilation.
    assert report["payments"] == {"cash": "37.00", "card": "30.00"}


async def test_top_items_normalises_labels(dataset):
    report = await _report(DAILY, _today())
    top = {row["label"]: row for row in report["top_items"]}

    # « Robe » et «   ROBE   » sont le meme article : casse et espaces.
    assert set(top) == {"robe", "manteau"}
    assert top["robe"]["quantity"] == 2
    assert top["robe"]["net"] == "37.00"
    assert top["manteau"]["quantity"] == 1
    # La veste annulee n'apparait pas au palmares.
    assert "veste" not in top
    # Tri par chiffre decroissant.
    assert [row["label"] for row in report["top_items"]] == ["robe", "manteau"]


async def test_by_cashier_splits_sales_and_cancellations(dataset):
    report = await _report(DAILY, _today())
    rows = {row["display_name"]: row for row in report["by_cashier"]}

    assert set(rows) == {"Léa", "Manon"}
    # Lea a encaisse 25 + 12 + 40 ; l'annulation a ete passee par Manon,
    # elle porte donc SON identifiant, pas celui de Lea.
    assert rows["Léa"]["sales_count"] == 3
    assert rows["Léa"]["sales_total"] == "77.00"
    assert rows["Léa"]["refunds_count"] == 0
    assert rows["Léa"]["net_total"] == "77.00"

    assert rows["Manon"]["sales_count"] == 1
    assert rows["Manon"]["sales_total"] == "30.00"
    assert rows["Manon"]["refunds_count"] == 1
    assert rows["Manon"]["refunds_total"] == "40.00"
    assert rows["Manon"]["net_total"] == "-10.00"


async def test_by_hour_carries_the_whole_net_on_the_selling_hour(dataset):
    report = await _report(DAILY, _today())
    non_zero = [row for row in report["by_hour"] if row["net"] != "0.00"]

    assert len(non_zero) == 1
    assert non_zero[0]["net"] == "67.00"
    assert non_zero[0]["sales_count"] == 3


async def test_weekly_covers_seven_days_with_holes_filled(dataset):
    today = _today()
    report = await _report(WEEKLY, today)

    assert report["period"]["kind"] == WEEKLY
    assert len(report["by_day"]) == 7
    assert "by_hour" not in report
    assert "weather" not in report

    series = {row["date"]: row for row in report["by_day"]}
    assert series[today.isoformat()]["net"] == "67.00"
    assert series[today.isoformat()]["sales_count"] == 3
    # Tous les autres jours de la semaine sont presents, a zero.
    assert sum(1 for row in report["by_day"] if row["net"] == "0.00") == 6
    assert report["totals"]["net"] == "67.00"


async def test_monthly_covers_every_day_of_the_month(dataset):
    today = _today()
    report = await _report(MONTHLY, today)

    first, last, _ = resolve_period(MONTHLY, day=today)
    assert len(report["by_day"]) == (last - first).days + 1
    assert report["by_day"][0]["date"] == first.isoformat()
    assert report["by_day"][-1]["date"] == last.isoformat()
    assert report["totals"]["net"] == "67.00"


async def test_previous_delta_pct_is_none_against_an_empty_period(dataset):
    report = await _report(DAILY, _today())
    assert report["previous"]["net"] == "0.00"
    # Une variation face a zero ne se calcule pas — surtout pas « +100 % ».
    assert report["previous"]["delta_pct"] is None


async def test_previous_delta_pct_compares_to_the_day_before(dataset):
    """Vu depuis demain, la veille (aujourd'hui) pese 67 € et le jour
    courant zero : la variation est de -100 %."""
    report = await _report(DAILY, _today() + timedelta(days=1))

    assert report["previous"]["from"] == _today().isoformat()
    assert report["previous"]["net"] == "67.00"
    assert report["totals"]["net"] == "0.00"
    assert report["previous"]["delta_pct"] == -100.0


# ---------------------------------------------------------------------------
# Annulation hors periode (revue Codex #17)
# ---------------------------------------------------------------------------


async def test_a_cancellation_outside_the_period_does_not_erase_the_sale(
    client, auth_headers, open_drawer
):
    """Une vente remboursee PLUS TARD reste une vente dans son rapport.

    `created_at` est signee et gelee : on ne peut pas antidater une vente
    pour simuler « vendue lundi, remboursee mardi ». On exerce donc la
    regle la ou elle vit — les bornes de la periode — en interrogeant les
    agregats avec une fenetre qui contient la vente mais pas son
    annulation, puis avec une fenetre qui contient les deux.

    Sans le bornage, la premiere fenetre afficherait 100 € de chiffre, zero
    vente et aucun article : un rapport qui se contredit lui-meme.
    """
    sale = await _sell_cash(client, auth_headers, "100.00", label="Trench")
    refund = await _cancel(client, auth_headers, sale)

    sold_at = datetime.fromisoformat(sale["created_at"])
    refunded_at = datetime.fromisoformat(refund["created_at"])
    assert refunded_at >= sold_at

    from app.services.reports import _items, _totals

    async with async_session() as db:
        # Fenetre 1 : la vente y est, son annulation non.
        before = await _totals(db, sold_at, refunded_at)
        items_before, top_before = await _items(db, sold_at, refunded_at)
        # Fenetre 2 : les deux y sont.
        after = await _totals(db, sold_at, refunded_at + timedelta(seconds=1))
        items_after, top_after = await _items(
            db, sold_at, refunded_at + timedelta(seconds=1)
        )

    assert before["gross"] == Decimal("100.00")
    assert before["refunds"] == Decimal("0.00")
    assert before["net"] == Decimal("100.00")
    assert before["sales_count"] == 1
    assert before["average_basket"] == Decimal("100.00")
    assert items_before == 1
    assert [row["label"] for row in top_before] == ["trench"]

    # La meme vente, vue depuis une periode qui contient aussi son
    # annulation : elle et son annulation se neutralisent exactement.
    assert after["gross"] == Decimal("100.00")
    assert after["refunds"] == Decimal("100.00")
    assert after["net"] == Decimal("0.00")
    assert after["sales_count"] == 0
    assert after["average_basket"] == Decimal("0.00")
    assert items_after == 0
    assert top_after == []


async def test_day_series_sums_to_the_period_counters(dataset):
    """Garde-fou de cohérence : la serie par jour et les totaux comptent
    les memes ventes. Deux chiffres d'un meme ecran qui ne s'additionnent
    pas, c'est un rapport qu'on cesse de lire."""
    for kind in (WEEKLY, MONTHLY):
        report = await _report(kind, _today())
        assert sum(row["sales_count"] for row in report["by_day"]) == (
            report["totals"]["sales_count"]
        )

    daily = await _report(DAILY, _today())
    assert sum(row["sales_count"] for row in daily["by_hour"]) == (
        daily["totals"]["sales_count"]
    )


# ---------------------------------------------------------------------------
# Objectifs
# ---------------------------------------------------------------------------


async def test_monthly_target_comes_from_the_settings(client, auth_headers, dataset):
    today = _today()
    month_key = f"{today.year:04d}-{today.month:02d}"
    await _set_targets(client, auth_headers, {"daily": "0.00", "monthly": {month_key: "670.00"}})

    report = await _report(MONTHLY, today)
    assert report["target"] == {"amount": "670.00", "progress_pct": 10.0}


async def test_weekly_target_is_the_sum_of_the_open_days(client, auth_headers, dataset):
    """L'objectif hebdo est la somme des objectifs journaliers du cahier :
    une semaine avec un jour de fermeture n'attend pas sept fois l'objectif
    d'un jour ouvre."""
    await _set_targets(client, auth_headers, {"daily": "100.00", "monthly": {}})
    # Dimanche ferme (defaut du cahier) -> six jours attendus a 100 €.
    report = await _report(WEEKLY, _today())
    assert report["target"] is not None
    assert report["target"]["amount"] == "600.00"

    daily = await _report(DAILY, _today())
    assert daily["target"]["amount"] == "100.00"


async def test_daily_target_follows_the_cahier_once_frozen(client, auth_headers, dataset):
    """Le rapport du jour et le cahier doivent afficher le MEME objectif.

    Le cahier fige l'objectif d'une journee a sa premiere lecture : relever
    l'objectif mensuel a midi ne reecrit pas ce qu'on a demande le matin.
    Si le rapport, lui, recalculait en direct, le manager verrait deux
    chiffres sur deux ecrans — et ne saurait plus lequel croire.
    """
    today = _today()
    month_key = f"{today.year:04d}-{today.month:02d}"
    await _set_targets(client, auth_headers, {"daily": "0.00", "monthly": {month_key: "3000.00"}})

    # Premiere lecture du cahier : l'objectif du jour est fige.
    r = await client.get(f"/api/cahier/{today.isoformat()}", headers=auth_headers)
    assert r.status_code == 200, r.text
    frozen = r.json()["target"]["daily"]

    # Le manager releve son objectif mensuel en cours de journee.
    await _set_targets(client, auth_headers, {"daily": "0.00", "monthly": {month_key: "9000.00"}})

    report = await _report(DAILY, today)
    assert report["target"]["amount"] == frozen

    r = await client.get(f"/api/cahier/{today.isoformat()}", headers=auth_headers)
    assert r.json()["target"]["daily"] == report["target"]["amount"]

    # Le MENSUEL, lui, suit bien le nouvel objectif : c'est le reglage du
    # mois, il n'est fige nulle part.
    assert (await _report(MONTHLY, today))["target"]["amount"] == "9000.00"


async def test_a_day_never_opened_in_the_cahier_follows_the_current_target(
    client, auth_headers, dataset
):
    """Consulter un rapport ne fige rien : un jour jamais ouvert dans le
    cahier suit l'objectif courant, et n'apparait pas en base."""
    today = _today()
    month_key = f"{today.year:04d}-{today.month:02d}"
    await _set_targets(client, auth_headers, {"daily": "150.00", "monthly": {}})

    before = await _report(DAILY, today)
    assert before["target"]["amount"] == "150.00"

    await _set_targets(client, auth_headers, {"daily": "200.00", "monthly": {}})
    after = await _report(DAILY, today)
    assert after["target"]["amount"] == "200.00"

    async with async_session() as db:
        from app.models.cahier_day import CahierDay

        rows = (await db.execute(select(CahierDay))).scalars().all()
    assert rows == []
    assert month_key  # le mois n'entre pas en jeu ici : repli sur `daily`


async def test_weekly_target_sums_the_frozen_and_the_current_days(
    client, auth_headers, dataset
):
    """La somme hebdomadaire passe par le meme objectif opposable : le jour
    deja ouvert garde le sien, les six autres suivent l'objectif courant."""
    today = _today()
    await _set_targets(client, auth_headers, {"daily": "100.00", "monthly": {}})
    r = await client.get(f"/api/cahier/{today.isoformat()}", headers=auth_headers)
    assert r.status_code == 200, r.text
    frozen = Decimal(r.json()["target"]["daily"])

    await _set_targets(client, auth_headers, {"daily": "10.00", "monthly": {}})

    report = await _report(WEEKLY, today)
    open_days = sum(
        1
        for offset in range(7)
        if (today - timedelta(days=today.weekday()) + timedelta(days=offset)).weekday() != 6
    )
    # Le jour fige compte pour son ancienne valeur, les autres jours ouverts
    # pour la nouvelle (le dimanche est ferme : 0).
    others = open_days - (1 if today.weekday() != 6 else 0)
    expected = (frozen if today.weekday() != 6 else Decimal("0.00")) + Decimal("10.00") * others
    assert report["target"]["amount"] == f"{expected:.2f}"


async def test_no_target_means_null(client, auth_headers, dataset):
    await _set_targets(client, auth_headers, {"daily": "0.00", "monthly": {}})
    assert (await _report(DAILY, _today()))["target"] is None
    assert (await _report(WEEKLY, _today()))["target"] is None
    assert (await _report(MONTHLY, _today()))["target"] is None


# ---------------------------------------------------------------------------
# Z de la periode
# ---------------------------------------------------------------------------


async def test_z_reports_of_the_period_are_listed(client, auth_headers, dataset):
    r = await client.post(
        "/api/pos/drawer/close",
        json={"closing_amount": "167.00"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    report = await _report(DAILY, _today())
    assert len(report["z_reports"]) == 1
    assert report["z_reports"][0]["report_number"] == 1
    assert report["z_reports"][0]["net"] == "67.00"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def test_routes_return_the_contract_envelope(client, auth_headers, dataset):
    today = _today().isoformat()
    month = _today().strftime("%Y-%m")

    r = await client.get(f"/api/reports/daily?date={today}", headers=auth_headers)
    assert r.status_code == 200, r.text
    daily = r.json()
    assert daily["period"]["kind"] == "daily"
    assert "by_hour" in daily and "by_day" not in daily
    # Le quotidien porte la meteo (instantane du cahier), vide tant que le
    # cahier n'a pas fige de journee.
    assert "weather" in daily

    r = await client.get(f"/api/reports/weekly?date={today}", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert "by_day" in r.json() and "by_hour" not in r.json()

    r = await client.get(f"/api/reports/monthly?month={month}", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["period"]["kind"] == "monthly"


async def test_routes_require_authentication(client):
    for path in ("/api/reports/daily?date=2026-09-15", "/api/reports/weekly?date=2026-09-15"):
        assert (await client.get(path)).status_code == 401


async def test_invalid_dates_are_422_invalid_date(client, auth_headers):
    for path in (
        "/api/reports/daily?date=15-09-2026",
        "/api/reports/weekly?date=pas-une-date",
        "/api/reports/monthly?month=2026-13",
    ):
        r = await client.get(path, headers=auth_headers)
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "invalid_date"


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


async def test_csv_is_bom_semicolon_comma_and_sectioned(client, auth_headers, dataset):
    today = _today().isoformat()
    r = await client.get(f"/api/reports/daily?date={today}&format=csv", headers=auth_headers)
    assert r.status_code == 200, r.text

    raw = r.content
    # BOM : sans lui, un tableur ouvre « Espèces » en « EspÃ¨ces ».
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")

    assert r.headers["content-disposition"] == (
        f'attachment; filename="rapport_daily_{today}.csv"'
    )
    assert "text/csv" in r.headers["content-type"]

    assert ";" in text
    # Decimales a la virgule, jamais au point.
    assert "67,00" in text
    assert "67.00" not in text
    for section in ("Totaux", "Par heure", "Par vendeuse", "Articles"):
        assert f"{section};" in text or f"\r\n{section}\r\n" in text or text.startswith(section)
    assert "Léa" in text


async def test_weekly_csv_has_a_day_section(client, auth_headers, dataset):
    today = _today().isoformat()
    r = await client.get(f"/api/reports/weekly?date={today}&format=csv", headers=auth_headers)
    assert r.status_code == 200, r.text
    text = r.content.decode("utf-8-sig")
    assert "Par jour" in text
    assert "Par heure" not in text


def test_neutralize_csv_cell_only_touches_dangerous_text():
    from decimal import Decimal as D

    from app.services.csv_safety import neutralize_csv_cell

    for dangerous in ("=1+1", "+3", "-Robe", "@x", "\tcache", "\r=1+1"):
        assert neutralize_csv_cell(dangerous) == f"'{dangerous}"
    # Texte ordinaire et valeurs numeriques intacts : prefixer un montant
    # casserait l'addition dans le tableur, ce qu'on vient y faire.
    for harmless in ("Robe", "Léa", "", "12,50"):
        assert neutralize_csv_cell(harmless) == harmless
    assert neutralize_csv_cell(3) == 3
    assert neutralize_csv_cell(D("1.50")) == D("1.50")
    assert neutralize_csv_cell(None) is None


async def test_csv_neutralises_a_formula_typed_as_an_item_label(
    client, auth_headers, open_drawer
):
    """Un libelle saisi en caisse ne doit pas devenir une formule chez la
    personne qui ouvre le fichier — la boutique, son comptable."""
    await _sell_cash(client, auth_headers, "5.00", label="=1+1")

    today = _today().isoformat()
    r = await client.get(f"/api/reports/daily?date={today}&format=csv", headers=auth_headers)
    assert r.status_code == 200, r.text
    text = r.content.decode("utf-8-sig")

    assert "'=1+1" in text
    # Aucune cellule ne commence par `=` : c'est la seule chose qu'un
    # tableur regarde.
    for line in text.split("\r\n"):
        for cell in line.split(";"):
            assert not cell.startswith(("=", "@")), cell


async def test_csv_download_is_journalled_without_its_content(client, auth_headers, dataset):
    today = _today()
    r = await client.get(
        f"/api/reports/monthly?month={today.strftime('%Y-%m')}&format=csv", headers=auth_headers
    )
    assert r.status_code == 200, r.text

    payloads = await _jet_export_payloads()
    assert len(payloads) == 1
    first, last, _ = resolve_period(MONTHLY, day=today)
    assert payloads[0] == {
        "kind": "report_csv",
        "period_kind": "monthly",
        "from": first.isoformat(),
        "to": last.isoformat(),
    }
    # Le journal dit qu'une extraction a eu lieu, jamais ce qu'elle contient.
    assert "Léa" not in str(payloads[0])


async def test_json_reading_writes_nothing_to_the_journal(client, auth_headers, dataset):
    await client.get(f"/api/reports/daily?date={_today().isoformat()}", headers=auth_headers)
    assert await _jet_export_payloads() == []


def test_amounts_are_strings_with_two_decimals():
    """Garde-fou de forme : le contrat impose des chaines, pas des flottants
    (un JSON `67.0` cote front deviendrait « 67 » a l'affichage)."""
    report = {
        "period": {"kind": DAILY, "from": date(2026, 9, 14), "to": date(2026, 9, 14), "label": "x"},
        "totals": {
            "gross": Decimal("7"),
            "refunds": Decimal("0"),
            "net": Decimal("7"),
            "sales_count": 1,
            "refunds_count": 0,
            "average_basket": Decimal("7"),
            "items_count": 1,
        },
        "payments": {"cash": Decimal("7"), "card": Decimal("0")},
        "by_cashier": [],
        "top_items": [],
        "previous": {
            "from": date(2026, 9, 13),
            "to": date(2026, 9, 13),
            "net": Decimal("0"),
            "delta_pct": None,
        },
        "target": None,
        "z_reports": [],
        "by_hour": [{"hour": 0, "net": Decimal("7"), "sales_count": 1}],
        "weather": None,
    }
    payload = serialize_report(report)
    assert payload["totals"]["net"] == "7.00"
    assert payload["payments"]["cash"] == "7.00"
    assert payload["by_hour"][0]["net"] == "7.00"
