# Nouveau service (PR11, M1 de docs/ARCHITECTURE_PR11.md) — rapports
# quotidien, hebdomadaire et mensuel.
#
# LECTURE SEULE de la chaine fiscale, comme `reporting.py` (dont ce module
# reutilise les briques : `_payments_split`, les arrondis et
# la borne de progression). Rien n'est ecrit, rien n'est signe — le SEUL
# geste journalise est le telechargement du CSV (JET `export.downloaded`),
# et il l'est par la route, pas ici.
#
# Deux regles structurantes, heritees du tableau de bord d'accueil :
#
# 1. **Journee civile Europe/Paris**, jamais UTC : une vente de 23h30 a
#    Paris appartient a la journee de la boutique, et la session de caisse
#    (qui peut etre a cheval sur minuit) n'est donc PAS la source.
# 2. **Tout est agrege en SQL.** Une boutique qui tourne depuis des annees
#    ne doit jamais charger un mois de lignes de vente en memoire pour
#    afficher un rapport — d'ou `SUM(...) FILTER (...)` et `GROUP BY` plutot
#    qu'une boucle par transaction.
#
# Une annulation est rattachee a SA date propre (celle ou elle a ete
# passee), jamais a celle de la vente d'origine : le rapport doit se
# reconcilier avec la caisse du jour, pas avec l'histoire.
from __future__ import annotations

import csv
import io
from datetime import date as date_cls
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, Integer, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.cashier import Cashier
from app.models.pos import (
    Transaction,
    TransactionItem,
    TransactionType,
    ZReport,
)
from app.services.cashier_service import UNIDENTIFIED_LABEL
from app.services.csv_safety import neutralize_csv_row
from app.services.fiscal import PosServiceError
from app.services.reporting import (
    ZERO,
    _amount_str,
    _day_bounds,
    _first_of_next_month,
    _money,
    _month_key,
    _month_target,
    _payments_split,
    _progress_pct,
)
from app.services.settings_service import SettingsService

DAILY = "daily"
WEEKLY = "weekly"
MONTHLY = "monthly"

TOP_ITEMS_LIMIT = 10

_MONTH_NAMES = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)
_WEEKDAY_NAMES = (
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
    "samedi",
    "dimanche",
)


# ---------------------------------------------------------------------------
# Periodes
# ---------------------------------------------------------------------------


def parse_day(raw: str) -> date_cls:
    """`AAAA-MM-JJ` -> date, 422 `invalid_date` sinon (contrat M1)."""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise PosServiceError(
            f"Date invalide ({raw!r}) : format AAAA-MM-JJ attendu.",
            code="invalid_date",
            status_code=422,
        )


def parse_month(raw: str) -> date_cls:
    """`AAAA-MM` -> premier jour du mois, 422 `invalid_date` sinon."""
    try:
        return datetime.strptime(raw, "%Y-%m").date()
    except (TypeError, ValueError):
        raise PosServiceError(
            f"Mois invalide ({raw!r}) : format AAAA-MM attendu.",
            code="invalid_date",
            status_code=422,
        )


def _french_date(day: date_cls) -> str:
    """« lundi 14 septembre 2026 » — sans dependre de la locale du systeme.

    `strftime("%A %d %B %Y")` renvoie de l'anglais sur un conteneur sans
    locale fr_FR installee : la boutique lirait « Monday ». Les deux tables
    ci-dessus coutent dix lignes et ne dependent de rien.
    """
    return (
        f"{_WEEKDAY_NAMES[day.weekday()]} {day.day} "
        f"{_MONTH_NAMES[day.month - 1]} {day.year}"
    )


def resolve_period(kind: str, *, day: date_cls) -> tuple[date_cls, date_cls, str]:
    """Bornes civiles inclusives et libelle lisible de la periode.

    `day` est le jour de reference : pour l'hebdomadaire c'est n'importe
    quel jour de la semaine ISO voulue (lundi -> dimanche), pour le mensuel
    n'importe quel jour du mois.
    """
    if kind == DAILY:
        return day, day, _french_date(day)
    if kind == WEEKLY:
        first = day - timedelta(days=day.weekday())
        last = first + timedelta(days=6)
        if first.month == last.month:
            span = f"du {first.day} au {last.day} {_MONTH_NAMES[last.month - 1]} {last.year}"
        else:
            span = (
                f"du {first.day} {_MONTH_NAMES[first.month - 1]} "
                f"au {last.day} {_MONTH_NAMES[last.month - 1]} {last.year}"
            )
        return first, last, f"semaine {span}"
    if kind == MONTHLY:
        first = day.replace(day=1)
        last = _first_of_next_month(first) - timedelta(days=1)
        return first, last, f"{_MONTH_NAMES[first.month - 1]} {first.year}"
    raise PosServiceError(
        f"Période inconnue ({kind!r}).", code="invalid_period", status_code=422
    )


def previous_period(kind: str, first: date_cls, last: date_cls) -> tuple[date_cls, date_cls]:
    """Meme periode precedente : la veille, la semaine d'avant, le mois d'avant.

    Pour le mensuel on recule d'un mois CIVIL (pas de 30 jours) : comparer
    fevrier a « les 28 jours precedant fevrier » n'aurait aucun sens pour la
    boutique.
    """
    if kind == MONTHLY:
        previous_last = first - timedelta(days=1)
        return previous_last.replace(day=1), previous_last
    span = (last - first) + timedelta(days=1)
    return first - span, last - span


# ---------------------------------------------------------------------------
# Agregats SQL
# ---------------------------------------------------------------------------


def _refunded_sale_ids(start: datetime, end: datetime):
    """Ventes annulees **par une annulation tombant dans la periode**.

    Le bornage est le coeur de la regle d'annulation des rapports, et il
    n'est pas un detail de requete.

    Une annulation est rattachee a SA date propre : les montants d'une
    vente du lundi remboursee le mardi comptent en positif lundi et en
    negatif mardi. Si les COMPTEURS, eux, cherchaient l'annulation sans
    borne de temps, le rapport du lundi retirerait cette vente de
    `sales_count`, du panier moyen, de `by_hour` et des articles tout en
    gardant ses 100 € dans `gross` et `net` : lundi afficherait 100 € de
    chiffre, zero vente et aucun article vendu. Un rapport deja imprime
    changerait en plus retroactivement le jour ou la cliente revient.

    D'ou la regle, la meme pour tous les compteurs de la periode : **une
    vente n'est « annulee » que si son annulation appartient a la meme
    periode que la vente.**

    - Rapport du lundi : 1 vente, l'article present, net 100 €.
    - Rapport du mardi : 0 vente, 100 € d'annulations, net -100 €.
    - Rapport de la semaine (les deux y sont) : 0 vente nette, aucun
      article, net 0 € — la vente et son annulation se neutralisent
      exactement, comme les montants.
    """
    refunded = aliased(Transaction)
    return (
        select(refunded.original_transaction_id)
        .where(
            refunded.transaction_type == TransactionType.refund,
            refunded.original_transaction_id.is_not(None),
            refunded.created_at >= start,
            refunded.created_at < end,
        )
        .scalar_subquery()
    )


async def _totals(db: AsyncSession, start: datetime, end: datetime) -> dict[str, Any]:
    """Totaux de la periode en UNE requete.

    - `gross` : ventes brutes, annulations non deduites ;
    - `refunds` : annulations (valeur positive) ;
    - `net` : la difference ;
    - `sales_count` : ventes **non annulees** — une vente annulee n'est pas
      un panier, et le panier moyen se calcule dessus.
    """
    is_sale = Transaction.transaction_type == TransactionType.sale
    is_cancelled = Transaction.id.in_(_refunded_sale_ids(start, end))

    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(Transaction.total_ttc).filter(is_sale), 0),
                func.coalesce(func.sum(Transaction.total_ttc).filter(~is_sale), 0),
                func.count().filter(is_sale & ~is_cancelled),
                func.count().filter(~is_sale),
            ).where(Transaction.created_at >= start, Transaction.created_at < end)
        )
    ).one()

    gross, refunds, sales_count, refunds_count = row
    gross = _money(gross)
    refunds = _money(refunds)
    net = _money(gross - refunds)
    sales_count = int(sales_count or 0)
    return {
        "gross": gross,
        "refunds": refunds,
        "net": net,
        "sales_count": sales_count,
        "refunds_count": int(refunds_count or 0),
        "average_basket": _money(net / sales_count) if sales_count else ZERO,
    }


async def _net_total(db: AsyncSession, start: datetime, end: datetime) -> Decimal:
    """Net seul — pour la periode precedente, dont on n'affiche que ce chiffre."""
    is_sale = Transaction.transaction_type == TransactionType.sale
    value = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(
                        case((is_sale, Transaction.total_ttc), else_=-Transaction.total_ttc)
                    ),
                    0,
                )
            ).where(Transaction.created_at >= start, Transaction.created_at < end)
        )
    ).scalar_one()
    return _money(value)


async def _by_hour(db: AsyncSession, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Net et nombre de ventes par heure civile Paris — 24 entrees, trous compris.

    Les 24 lignes sont toujours presentes : le graphique du rapport doit
    montrer une journee entiere, creux compris (une boutique fermee entre
    13h et 14h se voit).
    """
    is_sale = Transaction.transaction_type == TransactionType.sale
    is_cancelled = Transaction.id.in_(_refunded_sale_ids(start, end))
    hour_col = cast(
        func.extract("hour", func.timezone("Europe/Paris", Transaction.created_at)),
        Integer,
    ).label("hour")

    rows = (
        await db.execute(
            select(
                hour_col,
                func.coalesce(
                    func.sum(
                        case((is_sale, Transaction.total_ttc), else_=-Transaction.total_ttc)
                    ),
                    0,
                ),
                func.count().filter(is_sale & ~is_cancelled),
            )
            .where(Transaction.created_at >= start, Transaction.created_at < end)
            .group_by(hour_col)
        )
    ).all()

    found = {int(hour): (_money(net), int(count or 0)) for hour, net, count in rows}
    return [
        {
            "hour": hour,
            "net": found.get(hour, (ZERO, 0))[0],
            "sales_count": found.get(hour, (ZERO, 0))[1],
        }
        for hour in range(24)
    ]


async def _by_day(
    db: AsyncSession, start: datetime, end: datetime, first: date_cls, last: date_cls
) -> list[dict[str, Any]]:
    """Net et nombre de ventes par jour civil Paris — tous les jours, trous compris.

    Volontairement LOCAL plutot que `reporting._net_by_day` : ce dernier
    sert le tableau de bord d'accueil, dont la fenetre n'est pas celle d'un
    rapport. Ici, « annulee » se juge sur la PERIODE DU RAPPORT
    (`_refunded_sale_ids`), pas sur toute l'histoire de la boutique — ce
    qui garantit que la somme des `sales_count` de la serie est exactement
    le `sales_count` des totaux. Deux chiffres d'un meme ecran qui ne
    s'additionnent pas, c'est un rapport qu'on cesse de lire.
    """
    is_sale = Transaction.transaction_type == TransactionType.sale
    is_cancelled = Transaction.id.in_(_refunded_sale_ids(start, end))
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
                ),
                func.count().filter(is_sale & ~is_cancelled),
            )
            .where(Transaction.created_at >= start, Transaction.created_at < end)
            .group_by(day_col)
        )
    ).all()

    found = {day: (_money(net), int(count or 0)) for day, net, count in rows}
    series = []
    current = first
    while current <= last:
        net, count = found.get(current, (ZERO, 0))
        series.append({"date": current, "net": net, "sales_count": count})
        current += timedelta(days=1)
    return series


async def _items(db: AsyncSession, start: datetime, end: datetime) -> tuple[int, list[dict]]:
    """Nombre d'articles vendus et palmares des libelles.

    Agrege sur les ventes NON ANNULEES seulement : un article dont la vente
    a ete annulee n'a pas ete vendu, il n'a rien a faire dans le palmares.
    Il n'y a pas de catalogue dans cette caisse — un « article » est le
    libelle saisi en caisse, d'ou la normalisation (minuscules, espaces
    reduits) qui regroupe « Robe   Ete » et « robe été »... a la casse et
    aux espaces pres, sans jamais inventer de rapprochement plus malin.
    """
    is_sale = Transaction.transaction_type == TransactionType.sale
    is_cancelled = Transaction.id.in_(_refunded_sale_ids(start, end))
    scope = (
        (Transaction.created_at >= start),
        (Transaction.created_at < end),
        is_sale,
        ~is_cancelled,
    )

    label_col = func.lower(
        func.regexp_replace(func.btrim(TransactionItem.label), r"\s+", " ", "g")
    ).label("label")

    rows = (
        await db.execute(
            select(
                label_col,
                func.coalesce(func.sum(TransactionItem.quantity), 0),
                func.coalesce(func.sum(TransactionItem.line_total), 0),
            )
            .join(Transaction, Transaction.id == TransactionItem.transaction_id)
            .where(*scope)
            .group_by(label_col)
            # Le tri vit en SQL : sur un mois charge, on ne rapatrie que les
            # dix lignes affichees, pas tous les libelles de la boutique.
            .order_by(func.coalesce(func.sum(TransactionItem.line_total), 0).desc(), label_col)
            .limit(TOP_ITEMS_LIMIT)
        )
    ).all()

    items_count = (
        await db.execute(
            select(func.coalesce(func.sum(TransactionItem.quantity), 0))
            .select_from(TransactionItem)
            .join(Transaction, Transaction.id == TransactionItem.transaction_id)
            .where(*scope)
        )
    ).scalar_one()

    top_items = [
        {"label": label, "quantity": int(quantity or 0), "net": _money(net)}
        for label, quantity, net in rows
    ]
    return int(items_count or 0), top_items


async def _by_cashier(db: AsyncSession, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Ventes ventilees par vendeuse sur la periode.

    Meme forme que `cashier_service.sales_by_cashier` (qui, lui, borne par
    session de caisse pour le PDF du Z) : `sales_*` est le brut, `refunds_*`
    les annulations passees par la meme vendeuse, `net_total` la difference.
    Sans le net, une vendeuse dont l'unique vente a ete annulee affiche un
    chiffre qui ne se reconcilie avec rien.

    Les ventes encaissees sans identification (reglage `pos.cashier_required`
    a false) sont regroupees sous `cashier_id: null`.
    """
    is_sale = Transaction.transaction_type == TransactionType.sale
    rows = (
        await db.execute(
            select(
                Transaction.cashier_id,
                Cashier.display_name,
                func.count().filter(is_sale),
                func.coalesce(func.sum(Transaction.total_ttc).filter(is_sale), 0),
                func.count().filter(~is_sale),
                func.coalesce(func.sum(Transaction.total_ttc).filter(~is_sale), 0),
            )
            .outerjoin(Cashier, Cashier.id == Transaction.cashier_id)
            .where(Transaction.created_at >= start, Transaction.created_at < end)
            .group_by(Transaction.cashier_id, Cashier.display_name)
        )
    ).all()

    entries = []
    for cashier_id, display_name, sales_count, sales_total, refunds_count, refunds_total in rows:
        sales = _money(sales_total)
        refunds = _money(refunds_total)
        entries.append(
            {
                "cashier_id": str(cashier_id) if cashier_id else None,
                "display_name": display_name or UNIDENTIFIED_LABEL,
                "sales_count": int(sales_count or 0),
                "sales_total": sales,
                "refunds_count": int(refunds_count or 0),
                "refunds_total": refunds,
                "net_total": _money(sales - refunds),
            }
        )
    entries.sort(key=lambda e: (e["display_name"].lower(), e["cashier_id"] or ""))
    return entries


async def _z_reports(db: AsyncSession, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Z cloturés dans la periode, dans l'ordre des numeros.

    Bornes sur `closed_at` : un Z appartient au jour ou il a ete tire.
    """
    rows = (
        await db.execute(
            select(ZReport.report_number, ZReport.closed_at, ZReport.total_net)
            .where(ZReport.closed_at >= start, ZReport.closed_at < end)
            .order_by(ZReport.report_number)
        )
    ).all()
    return [
        {
            "report_number": int(number),
            "closed_at": closed_at,
            "net": _money(net),
        }
        for number, closed_at, net in rows
    ]


# ---------------------------------------------------------------------------
# Objectif et meteo — deleges au cahier du jour (M2)
# ---------------------------------------------------------------------------


async def _target_amount(
    db: AsyncSession, kind: str, first: date_cls, last: date_cls
) -> Decimal | None:
    """Objectif de la periode, ou None quand aucun objectif n'est fixe.

    - mensuel : directement le reglage `targets.monthly` ;
    - quotidien : l'objectif OPPOSABLE du jour selon le cahier (M2) —
      celui qui y est fige s'il l'est, le calcul courant sinon ;
    - hebdomadaire : la somme des objectifs opposables des sept jours — une
      semaine contenant deux jours de fermeture n'attend pas sept fois
      l'objectif d'un jour ouvre.

    On passe par `effective_daily_target_for` et non par le calcul
    theorique : le cahier fige l'objectif d'une journee des sa premiere
    lecture, et un manager qui releve son objectif mensuel a midi doit voir
    le MEME chiffre sur le rapport du jour et sur son cahier. Deux ecrans
    qui se contredisent, c'est un objectif auquel plus personne ne croit.

    Le module du cahier est importe PARESSEUSEMENT : les rapports doivent
    tourner meme si le cahier n'est pas encore livre (l'objectif est alors
    simplement absent), et non se mettre en erreur au chargement du module.
    """
    if kind == MONTHLY:
        targets = await SettingsService(db).get("targets")
        amount = _month_target(targets, _month_key(first))
        return amount if amount > 0 else None

    try:
        from app.services.cahier import effective_daily_target_for
    except ImportError:
        return None

    if kind == DAILY:
        return await effective_daily_target_for(db, first)

    total = ZERO
    found = False
    day = first
    while day <= last:
        value = await effective_daily_target_for(db, day)
        if value is not None:
            total += value
            found = True
        day += timedelta(days=1)
    return _money(total) if found and total > 0 else None


async def _weather(db: AsyncSession, day: date_cls) -> dict | None:
    """Instantane meteo fige par le cahier pour ce jour (M2), s'il existe.

    On ne rappelle JAMAIS le fournisseur meteo ici : pour un jour passe,
    l'API ne sait pas rejouer le passe, et coller la meteo d'aujourd'hui
    sur hier serait un mensonge.
    """
    try:
        from app.services.cahier import weather_snapshot_for
    except ImportError:
        return None
    return await weather_snapshot_for(db, day)


# ---------------------------------------------------------------------------
# Rapport complet
# ---------------------------------------------------------------------------


async def build_report(db: AsyncSession, kind: str, *, day: date_cls) -> dict[str, Any]:
    """Rapport complet d'une periode (montants en `Decimal`).

    La mise en forme (chaines a deux decimales, dates ISO) est faite par
    `serialize_report` : le CSV consomme la version `Decimal`, la route la
    version serialisee.
    """
    first, last, label = resolve_period(kind, day=day)
    start, end = _day_bounds(first, last + timedelta(days=1))

    totals = await _totals(db, start, end)
    cash, card = await _payments_split(db, start, end)
    items_count, top_items = await _items(db, start, end)

    previous_first, previous_last = previous_period(kind, first, last)
    previous_start, previous_end = _day_bounds(previous_first, previous_last + timedelta(days=1))
    previous_net = await _net_total(db, previous_start, previous_end)
    # Une variation face a zero n'est pas « +100 % », c'est une variation
    # qui ne se calcule pas : le front affiche alors un tiret.
    delta_pct = (
        round(float((totals["net"] - previous_net) / previous_net * 100), 1)
        if previous_net > 0
        else None
    )

    target_amount = await _target_amount(db, kind, first, last)

    report: dict[str, Any] = {
        "period": {"kind": kind, "from": first, "to": last, "label": label},
        "totals": {**totals, "items_count": items_count},
        "payments": {"cash": cash, "card": card},
        "by_cashier": await _by_cashier(db, start, end),
        "top_items": top_items,
        "previous": {
            "from": previous_first,
            "to": previous_last,
            "net": previous_net,
            "delta_pct": delta_pct,
        },
        "target": (
            {
                "amount": target_amount,
                "progress_pct": _progress_pct(totals["net"], target_amount),
            }
            if target_amount is not None and target_amount > 0
            else None
        ),
        "z_reports": await _z_reports(db, start, end),
    }

    if kind == DAILY:
        report["by_hour"] = await _by_hour(db, start, end)
        report["weather"] = await _weather(db, first)
    else:
        report["by_day"] = await _by_day(db, start, end, first, last)

    return report


def serialize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Forme exacte de la route (M1) : montants en chaines a deux decimales."""
    period = report["period"]
    totals = report["totals"]
    previous = report["previous"]
    target = report["target"]

    payload: dict[str, Any] = {
        "period": {
            "kind": period["kind"],
            "from": period["from"].isoformat(),
            "to": period["to"].isoformat(),
            "label": period["label"],
        },
        "totals": {
            "sales_count": totals["sales_count"],
            "refunds_count": totals["refunds_count"],
            "gross": _amount_str(totals["gross"]),
            "refunds": _amount_str(totals["refunds"]),
            "net": _amount_str(totals["net"]),
            "average_basket": _amount_str(totals["average_basket"]),
            "items_count": totals["items_count"],
        },
        "payments": {
            "cash": _amount_str(report["payments"]["cash"]),
            "card": _amount_str(report["payments"]["card"]),
        },
        "by_cashier": [
            {
                "cashier_id": row["cashier_id"],
                "display_name": row["display_name"],
                "sales_count": row["sales_count"],
                "sales_total": _amount_str(row["sales_total"]),
                "refunds_count": row["refunds_count"],
                "refunds_total": _amount_str(row["refunds_total"]),
                "net_total": _amount_str(row["net_total"]),
            }
            for row in report["by_cashier"]
        ],
        "top_items": [
            {
                "label": row["label"],
                "quantity": row["quantity"],
                "net": _amount_str(row["net"]),
            }
            for row in report["top_items"]
        ],
        "previous": {
            "from": previous["from"].isoformat(),
            "to": previous["to"].isoformat(),
            "net": _amount_str(previous["net"]),
            "delta_pct": previous["delta_pct"],
        },
        "target": (
            {
                "amount": _amount_str(target["amount"]),
                "progress_pct": target["progress_pct"],
            }
            if target
            else None
        ),
        "z_reports": [
            {
                "report_number": row["report_number"],
                "closed_at": row["closed_at"].isoformat(),
                "net": _amount_str(row["net"]),
            }
            for row in report["z_reports"]
        ],
    }

    if "by_hour" in report:
        payload["by_hour"] = [
            {
                "hour": row["hour"],
                "net": _amount_str(row["net"]),
                "sales_count": row["sales_count"],
            }
            for row in report["by_hour"]
        ]
        payload["weather"] = report.get("weather")
    else:
        payload["by_day"] = [
            {
                "date": row["date"].isoformat(),
                "net": _amount_str(row["net"]),
                "sales_count": row["sales_count"],
            }
            for row in report["by_day"]
        ]

    return payload


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def _fr_amount(value: Decimal) -> str:
    """Montant a la francaise : deux decimales, virgule decimale.

    Sans cela, un tableur configure en francais lit « 1234.50 » comme du
    texte (ou, pire, comme une date) et la boutique ne peut rien additionner.
    """
    return _amount_str(value).replace(".", ",")


def report_csv_filename(report: dict[str, Any]) -> str:
    period = report["period"]
    return f"rapport_{period['kind']}_{period['from'].isoformat()}.csv"


def report_to_csv(report: dict[str, Any]) -> str:
    """CSV en sections, separateur `;`, decimales a la virgule (M1).

    Un seul fichier pour toute la periode : les sections « Totaux », « Par
    jour »/« Par heure », « Par vendeuse » et « Articles » se suivent,
    separees par une ligne vide, chacune avec son entete. C'est ce que la
    boutique colle dans son tableur, feuille par feuille.
    """
    buffer = io.StringIO()
    # `\r\n` : les tableurs Windows (le parc de la boutique) restent le cas
    # le plus courant, et les autres s'en accommodent.
    raw_writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")

    def writerow(row: list[Any]) -> None:
        """Ecrit une ligne en neutralisant chaque cellule (revue Codex #17).

        Les libelles d'articles et les noms de vendeuses sont saisis
        librement en caisse : un libelle `=1+1` serait interprete comme une
        formule a l'ouverture du fichier dans un tableur. On passe donc
        TOUTES les cellules par le neutraliseur, y compris les en-tetes —
        pour n'avoir jamais a se demander lesquelles sont sures. Les
        montants et les compteurs ne sont pas des chaines : ils traversent
        intacts, et restent additionnables dans le tableur.
        """
        raw_writer.writerow(neutralize_csv_row(row))

    period = report["period"]
    totals = report["totals"]

    writerow(["Rapport", period["label"]])
    writerow(["Période", period["from"].isoformat(), period["to"].isoformat()])
    writerow([])

    writerow(["Totaux"])
    writerow(["Libellé", "Valeur"])
    writerow(["Ventes", totals["sales_count"]])
    writerow(["Annulations", totals["refunds_count"]])
    writerow(["Chiffre brut", _fr_amount(totals["gross"])])
    writerow(["Annulations (montant)", _fr_amount(totals["refunds"])])
    writerow(["Chiffre net", _fr_amount(totals["net"])])
    writerow(["Panier moyen", _fr_amount(totals["average_basket"])])
    writerow(["Articles", totals["items_count"]])
    writerow(["Espèces", _fr_amount(report["payments"]["cash"])])
    writerow(["Carte", _fr_amount(report["payments"]["card"])])
    if report["target"]:
        writerow(["Objectif", _fr_amount(report["target"]["amount"])])
        writerow(["Progression (%)", str(report["target"]["progress_pct"]).replace(".", ",")])
    writerow(
        [
            "Période précédente (net)",
            _fr_amount(report["previous"]["net"]),
        ]
    )
    writerow([])

    if "by_hour" in report:
        writerow(["Par heure"])
        writerow(["Heure", "Chiffre net", "Ventes"])
        for row in report["by_hour"]:
            writerow([f"{row['hour']:02d}", _fr_amount(row["net"]), row["sales_count"]])
    else:
        writerow(["Par jour"])
        writerow(["Jour", "Chiffre net", "Ventes"])
        for row in report["by_day"]:
            writerow(
                [row["date"].isoformat(), _fr_amount(row["net"]), row["sales_count"]]
            )
    writerow([])

    writerow(["Par vendeuse"])
    writerow(
        ["Vendeuse", "Ventes", "Total ventes", "Annulations", "Total annulations", "Net"]
    )
    for row in report["by_cashier"]:
        writerow(
            [
                row["display_name"],
                row["sales_count"],
                _fr_amount(row["sales_total"]),
                row["refunds_count"],
                _fr_amount(row["refunds_total"]),
                _fr_amount(row["net_total"]),
            ]
        )
    writerow([])

    writerow(["Articles"])
    writerow(["Article", "Quantité", "Chiffre net"])
    for row in report["top_items"]:
        writerow([row["label"], row["quantity"], _fr_amount(row["net"])])

    return buffer.getvalue()


__all__ = [
    "DAILY",
    "MONTHLY",
    "WEEKLY",
    "build_report",
    "parse_day",
    "parse_month",
    "previous_period",
    "report_csv_filename",
    "report_to_csv",
    "resolve_period",
    "serialize_report",
]

