# Nouveau service (PR6, H2 de docs/ARCHITECTURE_PR6.md) — tableau de bord
# d'accueil : jour courant, mois courant, sept derniers jours, confrontes aux
# objectifs saisis dans les reglages (`app_settings.targets`, H1).
#
# LECTURE SEULE de la chaine fiscale : on agrege `transactions` / `payments`,
# on n'ecrit rien, on ne signe rien, on ne journalise rien au JET. Les
# sessions de caisse (`cash_drawers` / `z_reports`) ne sont VOLONTAIREMENT
# pas la source : une journee civile n'est pas une session de caisse (une
# caisse peut etre ouverte a cheval sur minuit, fermee par la garde 23:59,
# ou regularisee), et le contrat impose la journee civile Europe/Paris.
#
# Tout est agrege en SQL (`SUM`/`COUNT ... FILTER`) et groupe par jour civil
# Paris : une boutique qui tourne depuis des annees ne doit jamais charger
# toutes ses lignes de vente en memoire pour afficher son accueil.
from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.pos import Payment, PaymentMethod, Transaction, TransactionType
from app.services.pos import _PARIS
from app.services.settings_service import SettingsService

ZERO = Decimal("0.00")

# Bornage du pourcentage de progression (H3) : une journee exceptionnelle
# face a un objectif symbolique ne doit pas produire une barre a 12 000 %.
MAX_PROGRESS_PCT = 999.0

SERIES_DAYS = 7


def _money(value: Decimal | int | float | None) -> Decimal:
    """Arrondi monetaire commercial (2 decimales, ROUND_HALF_UP)."""
    if value is None:
        return ZERO
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _amount_str(value: Decimal) -> str:
    return f"{_money(value):.2f}"


def _progress_pct(realized: Decimal, target: Decimal) -> float:
    """Progression vers l'objectif, en pourcentage a une decimale.

    0.0 quand aucun objectif n'est fixe (H4 affiche alors une invitation a en
    fixer un, pas une barre). Borne a [0, MAX_PROGRESS_PCT].
    """
    if target <= 0:
        return 0.0
    pct = float(realized / target * 100)
    return round(max(0.0, min(pct, MAX_PROGRESS_PCT)), 1)


def _parse_amount(raw: Any) -> Decimal:
    """Lit un objectif stocke en reglage. Une valeur illisible vaut 0 : le
    tableau de bord d'accueil ne doit jamais tomber en erreur a cause d'un
    reglage, il affiche simplement « aucun objectif »."""
    try:
        value = Decimal(str(raw))
    except Exception:
        return ZERO
    if not value.is_finite() or value < 0:
        return ZERO
    return _money(value)


def _month_key(day: date_cls) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _month_target(targets: dict[str, Any], month_key: str) -> Decimal:
    """Objectif du mois : valeur saisie, sinon `monthly["default"]`, sinon 0."""
    monthly = targets.get("monthly")
    if not isinstance(monthly, dict):
        return ZERO
    if month_key in monthly:
        return _parse_amount(monthly[month_key])
    if "default" in monthly:
        return _parse_amount(monthly["default"])
    return ZERO


def _day_bounds(first: date_cls, last_excluded: date_cls) -> tuple[datetime, datetime]:
    """Bornes `[00:00, 24:00)` en heure de Paris — jamais en UTC : une vente
    de 23h30 a Paris appartient a la journee civile de la boutique."""
    return (
        datetime(first.year, first.month, first.day, tzinfo=_PARIS),
        datetime(last_excluded.year, last_excluded.month, last_excluded.day, tzinfo=_PARIS),
    )


def _first_of_next_month(day: date_cls) -> date_cls:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)


async def dashboard(db: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    """Agregats du tableau de bord d'accueil (H2).

    `now` fixe l'instant de reference (tests, consultation d'un jour passe via
    `?date=`) : la journee civile Paris qui le contient devient « aujourd'hui »,
    le mois qui la contient devient « ce mois », et la serie couvre J-6 -> J.

    Montants : `Decimal`. La mise en forme en chaines a 2 decimales attendue
    par la route (H3) est faite par `serialize_dashboard`.
    """
    reference = datetime.now(_PARIS) if now is None else now
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=_PARIS)
    reference = reference.astimezone(_PARIS)

    today = reference.date()
    month_first = today.replace(day=1)
    next_month_first = _first_of_next_month(today)
    series_first = today - timedelta(days=SERIES_DAYS - 1)

    # Une seule lecture couvre le mois ET la serie 7 jours (qui peut deborder
    # sur le mois precedent en debut de mois).
    range_first = min(month_first, series_first)
    range_start, range_end = _day_bounds(range_first, today + timedelta(days=1))

    per_day = await _net_by_day(db, range_start, range_end)

    # -- Aujourd'hui ----------------------------------------------------
    day_start, day_end = _day_bounds(today, today + timedelta(days=1))
    cash, card = await _payments_split(db, day_start, day_end)

    settings_targets = await SettingsService(db).get("targets")
    daily_target = _parse_amount(settings_targets.get("daily"))
    month_key = _month_key(today)
    monthly_target = _month_target(settings_targets, month_key)

    today_row = per_day.get(today, _EMPTY_DAY)
    today_net = today_row["net"]
    today_sales = today_row["sales_count"]
    average_basket = _money(today_net / today_sales) if today_sales else ZERO

    # -- Ce mois --------------------------------------------------------
    month_days = [d for d in per_day if month_first <= d <= today]
    month_net = _money(sum((per_day[d]["net"] for d in month_days), ZERO))
    month_sales = sum(per_day[d]["sales_count"] for d in month_days)
    # « Jour ouvert » = jour ou au moins une vente a ete encaissee, meme si
    # elle a ete annulee depuis : la boutique a bien ouvert ce jour-la.
    days_open = sum(1 for d in month_days if per_day[d]["gross_sales_count"] > 0)
    # Jour courant inclus : le rythme necessaire doit tenir compte du fait
    # qu'on peut encore vendre aujourd'hui.
    remaining_days = (next_month_first - today).days
    missing = max(ZERO, monthly_target - month_net)
    required_daily = _money(missing / remaining_days) if remaining_days > 0 else ZERO

    sellable_days = [d for d in month_days if per_day[d]["gross_sales_count"] > 0]
    best_day_date = max(sellable_days, key=lambda d: per_day[d]["net"], default=None)
    best_day = {
        "date": best_day_date,
        "net": per_day[best_day_date]["net"] if best_day_date is not None else ZERO,
    }

    # -- Sept derniers jours --------------------------------------------
    last_7_days = []
    for offset in range(SERIES_DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        row = per_day.get(day, _EMPTY_DAY)
        last_7_days.append(
            {"date": day, "net": row["net"], "sales_count": row["sales_count"]}
        )

    return {
        "generated_at": reference,
        "today": {
            "date": today,
            "sales_count": today_sales,
            "refunds_count": today_row["refunds_count"],
            "net": today_net,
            "average_basket": average_basket,
            "cash": cash,
            "card": card,
            "target": daily_target,
            "progress_pct": _progress_pct(today_net, daily_target),
        },
        "month": {
            "month": month_key,
            "net": month_net,
            "sales_count": month_sales,
            "target": monthly_target,
            "progress_pct": _progress_pct(month_net, monthly_target),
            "days_open": days_open,
            "remaining_days": remaining_days,
            "required_daily": required_daily,
            "best_day": best_day,
        },
        "last_7_days": last_7_days,
    }


_EMPTY_DAY: dict[str, Any] = {
    "net": ZERO,
    "sales_count": 0,
    "gross_sales_count": 0,
    "refunds_count": 0,
}


async def _net_by_day(
    db: AsyncSession, range_start: datetime, range_end: datetime
) -> dict[date_cls, dict[str, Any]]:
    """Net, nombre de ventes et nombre d'annulations par jour civil Paris.

    - `net` = somme des ventes moins somme des annulations (une annulation est
      une transaction `refund` de montant positif miroir).
    - `sales_count` = ventes **non annulees** (le panier moyen se calcule
      dessus : une vente annulee n'est pas un panier).
    - `gross_sales_count` = toutes les ventes, annulees comprises (sert a
      savoir si la boutique a ouvert ce jour-la).
    """
    refunded = aliased(Transaction)
    refunded_sale_ids = (
        select(refunded.original_transaction_id)
        .where(
            refunded.transaction_type == TransactionType.refund,
            refunded.original_transaction_id.is_not(None),
        )
        .scalar_subquery()
    )

    is_sale = Transaction.transaction_type == TransactionType.sale
    is_cancelled = Transaction.id.in_(refunded_sale_ids)
    day_col = cast(func.timezone("Europe/Paris", Transaction.created_at), Date).label("day")

    rows = (
        await db.execute(
            select(
                day_col,
                func.coalesce(
                    func.sum(
                        case((is_sale, Transaction.total_ttc), else_=-Transaction.total_ttc)
                    ),
                    0,
                ).label("net"),
                func.count().filter(is_sale & ~is_cancelled).label("sales_count"),
                func.count().filter(is_sale).label("gross_sales_count"),
                func.count().filter(~is_sale).label("refunds_count"),
            )
            .where(Transaction.created_at >= range_start, Transaction.created_at < range_end)
            .group_by(day_col)
        )
    ).all()

    return {
        row.day: {
            "net": _money(row.net),
            "sales_count": int(row.sales_count),
            "gross_sales_count": int(row.gross_sales_count),
            "refunds_count": int(row.refunds_count),
        }
        for row in rows
    }


async def _payments_split(
    db: AsyncSession, day_start: datetime, day_end: datetime
) -> tuple[Decimal, Decimal]:
    """Ventilation especes / carte du jour, nette des annulations.

    Les paiements d'une annulation sont le miroir (positif) de ceux de la
    vente d'origine : on les retranche methode par methode, pour qu'une vente
    CB annulee ne laisse pas 25 € fantomes en carte.
    """
    is_sale = Transaction.transaction_type == TransactionType.sale
    rows = (
        await db.execute(
            select(
                Payment.method,
                func.coalesce(
                    func.sum(case((is_sale, Payment.amount), else_=-Payment.amount)), 0
                ).label("total"),
            )
            .join(Transaction, Transaction.id == Payment.transaction_id)
            .where(Transaction.created_at >= day_start, Transaction.created_at < day_end)
            .group_by(Payment.method)
        )
    ).all()

    totals = {row.method: _money(row.total) for row in rows}
    return totals.get(PaymentMethod.cash, ZERO), totals.get(PaymentMethod.card, ZERO)


def serialize_dashboard(data: dict[str, Any]) -> dict[str, Any]:
    """Met la sortie de `dashboard()` au format exact de la route (H3) :
    montants en chaines a 2 decimales, dates `YYYY-MM-DD`, aucun `null`
    hors `month.best_day.date` quand aucune vente n'a eu lieu."""
    today = data["today"]
    month = data["month"]
    best_day = month["best_day"]
    return {
        "generated_at": data["generated_at"].isoformat(),
        "today": {
            "date": today["date"].isoformat(),
            "sales_count": today["sales_count"],
            "refunds_count": today["refunds_count"],
            "net": _amount_str(today["net"]),
            "average_basket": _amount_str(today["average_basket"]),
            "cash": _amount_str(today["cash"]),
            "card": _amount_str(today["card"]),
            "target": _amount_str(today["target"]),
            "progress_pct": today["progress_pct"],
        },
        "month": {
            "month": month["month"],
            "net": _amount_str(month["net"]),
            "sales_count": month["sales_count"],
            "target": _amount_str(month["target"]),
            "progress_pct": month["progress_pct"],
            "days_open": month["days_open"],
            "remaining_days": month["remaining_days"],
            "required_daily": _amount_str(month["required_daily"]),
            "best_day": {
                "date": best_day["date"].isoformat() if best_day["date"] else None,
                "net": _amount_str(best_day["net"]),
            },
        },
        "last_7_days": [
            {
                "date": entry["date"].isoformat(),
                "net": _amount_str(entry["net"]),
                "sales_count": entry["sales_count"],
            }
            for entry in data["last_7_days"]
        ],
    }
