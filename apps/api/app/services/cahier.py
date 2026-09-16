# Nouveau service (PR11, docs/ARCHITECTURE_PR11.md, contrat M2) — le cahier
# du jour : l'objectif de la journee, ce que la boutique en a fait, ce qu'elle
# en a ecrit, et qui l'a signe.
#
# LECTURE SEULE de la chaine fiscale : les chiffres realises viennent de
# `services/reporting._net_by_day` (agregats SQL sur `transactions`), rien
# n'est recopie ni signe ici. Les seules ecritures du service portent sur
# `cahier_days`, table d'exploitation (§M2 : « rapports et cahier sont des
# lectures/exploitation, hors perimetre »).
#
# Deux fonctions sont publiques pour les rapports (M1) : `daily_target_for`
# (objectif du jour, que le rapport quotidien et le rapport hebdomadaire
# somment) et `weather_snapshot_for` (instantane meteo fige, repris tel quel
# par le rapport quotidien).
from __future__ import annotations

import calendar
from datetime import date as date_cls
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cahier_day import CahierDay
from app.services.fiscal import PosServiceError
from app.services.jet import (
    EVENT_CAHIER_SIGNED,
    EVENT_CAHIER_TEXT_UPDATED,
    JournalService,
)
from app.services.pos import _PARIS
from app.services.reporting import _month_key, _month_target, _parse_amount
from app.services.settings_service import SettingsService

ZERO = Decimal("0.00")

# Longueur maximale des deux textes libres (M2). Le cahier est une ardoise,
# pas un traitement de texte : au-dela, c'est une note de service, elle a sa
# place ailleurs.
TEXT_MAX_LENGTH = 500

# Semaine « lundi -> dimanche » : c'est l'ordre du reglage `weekday_open` et
# celui de `date.weekday()` (0 = lundi), pas celui de `strftime('%w')`.
DEFAULT_WEEKDAY_OPEN = [True, True, True, True, True, True, False]

WEEKDAY_LABELS = [
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
    "samedi",
    "dimanche",
]


def _money(value: Decimal | int | float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize_weekday_open(raw: Any) -> list[bool]:
    """Ramene le reglage a exactement 7 booleens (lundi -> dimanche).

    Tolerant a un JSONB ecrit a la main ou herite d'une version anterieure :
    une valeur illisible rend les jours d'ouverture par defaut plutot que de
    faire tomber la page en erreur. Un objectif du jour n'est pas un montant
    fiscal, il ne merite pas de casser la lecture du cahier.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 7:
        return list(DEFAULT_WEEKDAY_OPEN)
    return [bool(value) for value in raw]


async def weekday_open(db: AsyncSession) -> list[bool]:
    cahier = await SettingsService(db).get("cahier")
    return normalize_weekday_open(cahier.get("weekday_open"))


def open_days_in_month(day: date_cls, opens: list[bool]) -> int:
    """Nombre de jours ouverts du mois qui contient `day`."""
    days_in_month = calendar.monthrange(day.year, day.month)[1]
    return sum(
        1
        for number in range(1, days_in_month + 1)
        if opens[date_cls(day.year, day.month, number).weekday()]
    )


def compute_daily_target(
    day: date_cls, targets: dict[str, Any], opens: list[bool]
) -> Decimal | None:
    """Objectif du jour (M2), sans toucher a la base.

    Formule PLATE, a dessein : objectif mensuel divise par le nombre de jours
    ouverts du mois. On ne pondere pas par le poids historique des jours de
    la semaine — la boutique doit pouvoir refaire le calcul de tete devant sa
    caisse, et un objectif qu'on ne sait pas expliquer n'est pas tenu.

    - jour ferme -> 0 (on n'attend rien d'un jour ou la boutique est close) ;
    - aucun objectif mensuel -> repli sur `targets.daily` ;
    - aucun objectif du tout -> `None` (l'ecran invite a en fixer un, il
      n'affiche pas une barre a 0 %).
    """
    if not opens[day.weekday()]:
        return ZERO

    monthly = _month_target(targets, _month_key(day))
    if monthly > 0:
        open_days = open_days_in_month(day, opens)
        if open_days > 0:
            return _money(monthly / open_days)

    daily = _parse_amount(targets.get("daily"))
    return daily if daily > 0 else None


async def daily_target_for(db: AsyncSession, day: date_cls) -> Decimal | None:
    """Objectif du jour, reglages lus en base.

    Utilise par le cahier (figeage a la premiere lecture) ET par les rapports
    M1 (objectif du rapport quotidien, somme des objectifs pour l'hebdo).
    Ne fige rien, n'ecrit rien : c'est le calcul theorique du jour, pas
    l'objectif eventuellement deja fige dans `cahier_days`.
    """
    settings_service = SettingsService(db)
    targets = await settings_service.get("targets")
    cahier = await settings_service.get("cahier")
    return compute_daily_target(
        day, targets, normalize_weekday_open(cahier.get("weekday_open"))
    )


async def weather_snapshot_for(db: AsyncSession, day: date_cls) -> dict | None:
    """Instantane meteo fige dans le cahier pour ce jour, s'il existe.

    Le rapport quotidien (M1) l'affiche tel quel : il ne rappelle jamais le
    fournisseur meteo pour un jour passe (l'API ne sait pas rejouer le
    passe, et une meteo d'aujourd'hui collee sur hier serait un mensonge).
    """
    row = (
        await db.execute(select(CahierDay).where(CahierDay.day == day))
    ).scalar_one_or_none()
    if row is None:
        return None
    snapshot = row.weather_snapshot
    return dict(snapshot) if isinstance(snapshot, dict) else None


def paris_today(now: datetime | None = None) -> date_cls:
    """Journee civile Europe/Paris courante — la boutique, pas le serveur."""
    reference = datetime.now(_PARIS) if now is None else now
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=_PARIS)
    return reference.astimezone(_PARIS).date()


def previous_year_day(day: date_cls) -> date_cls:
    """Meme date l'annee precedente.

    Le 29 fevrier n'existe pas tous les ans : on retombe alors sur le 28,
    plutot que de renvoyer une erreur pour une journee qui a bel et bien eu
    lieu.
    """
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def month_bounds(day: date_cls) -> tuple[date_cls, date_cls]:
    """Premier jour du mois et premier jour du mois suivant."""
    first = day.replace(day=1)
    return first, (first + timedelta(days=32)).replace(day=1)


# ---------------------------------------------------------------------------
# Lecture d'une journee
# ---------------------------------------------------------------------------


async def _net_by_hour(
    db: AsyncSession, day_start: datetime, day_end: datetime
) -> dict[int, Decimal]:
    """Net encaisse par heure civile Paris, agrege en SQL.

    Meme convention que `reporting._net_by_day` : une annulation est une
    transaction `refund` de montant positif miroir, donc retranchee. Elle est
    rattachee a SON heure, pas a celle de la vente d'origine — c'est bien a
    l'heure ou la caisse a rendu l'argent que le creux apparait.
    """
    from sqlalchemy import case, func, select

    from app.models.pos import Transaction, TransactionType

    is_sale = Transaction.transaction_type == TransactionType.sale
    hour_col = func.extract(
        "hour", func.timezone("Europe/Paris", Transaction.created_at)
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
                ).label("net"),
            )
            .where(
                Transaction.created_at >= day_start,
                Transaction.created_at < day_end,
            )
            .group_by(hour_col)
        )
    ).all()
    return {int(row.hour): _money(row.net) for row in rows}


def _remaining_open_days(day: date_cls, today: date_cls, opens: list[bool]) -> int:
    """Jours ouverts restants du mois, vus depuis `day`.

    La journee consultee compte encore si elle n'est pas revolue (on peut
    toujours vendre aujourd'hui, et a plus forte raison demain) ; sur un jour
    passe, on regarde ce qu'il restait le soir meme, donc a partir du
    lendemain. C'est ce qui rend la page d'un jour ancien relisible telle
    qu'elle etait ce jour-la, au lieu d'un melange de deux instants.
    """
    first = day if day >= today else day + timedelta(days=1)
    _, next_month_first = month_bounds(day)
    count = 0
    cursor = first
    while cursor < next_month_first:
        if opens[cursor.weekday()]:
            count += 1
        cursor += timedelta(days=1)
    return count


async def get_or_create_row(
    db: AsyncSession, day: date_cls, *, now: datetime | None = None
) -> CahierDay:
    """Ligne du cahier pour ce jour, creee au besoin.

    C'est ICI que l'objectif est FIGE (M2) : la premiere fois qu'une journee
    est ouverte, on inscrit l'objectif tel qu'il est calcule a cet instant.
    Revoir l'objectif du mois ensuite ne reecrira pas les jours deja
    consultes — une progression affichee hier doit etre la meme relue demain.

    L'insertion passe par `ON CONFLICT DO NOTHING` : deux onglets ouverts sur
    la meme journee ne doivent pas se solder par une violation de cle
    primaire a l'affichage.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    row = (
        await db.execute(select(CahierDay).where(CahierDay.day == day))
    ).scalar_one_or_none()
    if row is not None:
        return row

    target = await daily_target_for(db, day)
    await db.execute(
        pg_insert(CahierDay.__table__)
        .values(day=day, frozen_daily_target=target)
        .on_conflict_do_nothing(index_elements=["day"])
    )
    await db.flush()
    return (
        await db.execute(select(CahierDay).where(CahierDay.day == day))
    ).scalar_one()


async def _current_weather(db: AsyncSession) -> dict | None:
    """Meteo courante via le service M3, s'il est disponible.

    Import PARESSEUX et defensif : le cahier doit s'afficher meme si le
    service meteo n'est pas (encore) la, ou s'il tombe. Une boutique ne
    renonce pas a son cahier du jour parce qu'un service tiers est en panne.
    """
    try:
        from app.services.weather import get_current
    except Exception:  # noqa: BLE001 — module absent : le cahier s'en passe.
        return None
    try:
        snapshot = await get_current(db)
    except Exception:  # noqa: BLE001 — jamais d'exception vers l'appelant (M3).
        return None
    return snapshot if isinstance(snapshot, dict) else None


async def _weather_for(
    db: AsyncSession, row: CahierDay, *, is_today: bool
) -> dict | None:
    """Instantane meteo de la journee, fige au premier appel du jour meme.

    Un instantane deja fige est rendu tel quel, pour toujours : la meteo du
    12 mars ne se relit nulle part, elle ne s'invente pas non plus. Une
    indisponibilite, elle, n'est PAS figee — on la renvoie pour affichage
    mais le prochain chargement de la page retentera sa chance.
    """
    if isinstance(row.weather_snapshot, dict) and row.weather_snapshot:
        return dict(row.weather_snapshot)
    if not is_today:
        return None
    snapshot = await _current_weather(db)
    if snapshot is None:
        return None
    if not snapshot.get("unavailable"):
        row.weather_snapshot = snapshot
        await db.flush()
    return dict(snapshot)


async def _signature_username(db: AsyncSession, user_id) -> str | None:
    if user_id is None:
        return None
    from app.models.user import User

    return (
        await db.execute(select(User.username).where(User.id == user_id))
    ).scalar_one_or_none()


async def read_day(
    db: AsyncSession, day: date_cls, *, now: datetime | None = None
) -> dict[str, Any]:
    """Journee complete du cahier, au format de la route (M2).

    Montants en chaines a deux decimales, comme partout ailleurs. Les
    chiffres realises sont RECALCULES depuis `transactions` a chaque lecture
    (jamais recopies dans `cahier_days`) : le cahier est une vue sur la
    caisse, pas un second livre de comptes.
    """
    from app.services.reporting import _net_by_day, _progress_pct

    today = paris_today(now)
    row = await get_or_create_row(db, day, now=now)

    settings_service = SettingsService(db)
    targets = await settings_service.get("targets")
    cahier_settings = await settings_service.get("cahier")
    opens = normalize_weekday_open(cahier_settings.get("weekday_open"))

    month_first, _ = month_bounds(day)
    per_day = await _net_by_day(
        db,
        *_paris_bounds(month_first, day + timedelta(days=1)),
    )

    day_row = per_day.get(day)
    day_net = day_row["net"] if day_row else ZERO
    day_sales = day_row["sales_count"] if day_row else 0
    average_basket = _money(day_net / day_sales) if day_sales else ZERO

    month_realized = _money(sum((entry["net"] for entry in per_day.values()), ZERO))
    monthly_target = _month_target(targets, _month_key(day))

    frozen = row.frozen_daily_target
    daily_target = Decimal(str(frozen)) if frozen is not None else None

    by_hour = await _net_by_hour(db, *_paris_bounds(day, day + timedelta(days=1)))

    previous_day = previous_year_day(day)
    previous_map = await _net_by_day(
        db, *_paris_bounds(previous_day, previous_day + timedelta(days=1))
    )
    previous_row = previous_map.get(previous_day)

    remaining_open_days = _remaining_open_days(day, today, opens)
    month_remaining = max(ZERO, monthly_target - month_realized)

    weather = await _weather_for(db, row, is_today=day == today)
    manager_username = await _signature_username(db, row.manager_signed_by_user_id)

    return {
        "day": day.isoformat(),
        # 0 = lundi, comme `weekday_open` et `date.weekday()`.
        "weekday": day.weekday(),
        "is_today": day == today,
        "is_past": day < today,
        "is_open": opens[day.weekday()],
        "target": {
            # Un objectif nul (jour ferme, ou aucun objectif saisi) est rendu
            # `null` et non « 0.00 » : l'ecran invite alors a en fixer un, il
            # n'affiche pas une barre de progression vers zero.
            "daily": _amount(daily_target) if daily_target and daily_target > 0 else None,
            "monthly": _amount(monthly_target) if monthly_target > 0 else None,
            "month_realized": _amount(month_realized),
            "month_progress_pct": (
                _progress_pct(month_realized, monthly_target)
                if monthly_target > 0
                else None
            ),
            "month_remaining": _amount(month_remaining) if monthly_target > 0 else None,
            # `null` quand il ne reste aucun jour ouvert : il n'y a plus
            # rien a repartir, et « 0,00 € par jour » se lirait comme un
            # objectif atteint alors que le mois peut etre tres en retard.
            "required_daily_rest_of_month": (
                _amount(_money(month_remaining / remaining_open_days))
                if monthly_target > 0 and remaining_open_days > 0
                else None
            ),
        },
        "realized": {
            "net": _amount(day_net),
            "sales_count": day_sales,
            "average_basket": _amount(average_basket),
            "progress_pct": (
                _progress_pct(day_net, daily_target)
                if daily_target and daily_target > 0
                else None
            ),
            "by_hour": [
                {"hour": hour, "net": _amount(by_hour.get(hour, ZERO))}
                for hour in range(24)
            ],
        },
        # « Aucune vente l'an dernier » n'est pas « 0 € l'an dernier » : sans
        # vente ce jour-la (boutique fermee, ou pas encore ouverte), on ne
        # compare rien du tout.
        "previous_year": (
            {
                "day": previous_day.isoformat(),
                "net": _amount(previous_row["net"]),
            }
            if previous_row and previous_row["gross_sales_count"] > 0
            else None
        ),
        "message": row.message,
        "operation": row.operation,
        "signatures": {
            "manager": (
                {
                    "at": row.manager_signed_at.isoformat(),
                    "username": manager_username or "",
                }
                if row.manager_signed_at is not None
                else None
            ),
            "team": (
                {
                    "at": row.team_signed_at.isoformat(),
                    "name": row.team_signed_by_name or "",
                }
                if row.team_signed_at is not None
                else None
            ),
        },
        "weather": weather,
    }


def _amount(value: Decimal) -> str:
    return f"{_money(value):.2f}"


def _paris_bounds(first: date_cls, last_excluded: date_cls) -> tuple[datetime, datetime]:
    """Bornes `[00:00, 24:00)` en heure de Paris (jamais en UTC : une vente
    de 23h30 appartient a la journee civile de la boutique)."""
    return (
        datetime(first.year, first.month, first.day, tzinfo=_PARIS),
        datetime(
            last_excluded.year, last_excluded.month, last_excluded.day, tzinfo=_PARIS
        ),
    )


# ---------------------------------------------------------------------------
# Ecritures : textes libres et signatures
# ---------------------------------------------------------------------------


def _refuse_past_day(day: date_cls, today: date_cls) -> None:
    """Une journee revolue ne s'ecrit plus (M2).

    Le cahier du jour se tient le jour meme : reecrire le message d'hier ou
    signer une journee close apres coup viderait les signatures de leur sens.
    """
    if day < today:
        raise PosServiceError(
            "Cette journée est close : son cahier ne peut plus être modifié.",
            code="day_closed",
            status_code=409,
        )


async def update_text(
    db: AsyncSession,
    day: date_cls,
    *,
    message: str | None = None,
    operation: str | None = None,
    fields: set[str] | frozenset[str] | None = None,
    user_id=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Enregistre le message du jour et/ou l'operation en cours.

    `fields` porte les champs REELLEMENT envoyes : le front n'envoie que
    celui qu'il vient de modifier (sauvegarde au blur), et un champ absent ne
    doit pas effacer l'autre.

    Le JET recoit le jour et les noms des champs touches — JAMAIS leur
    contenu (M2) : le journal est immuable, un texte libre qui y tomberait ne
    pourrait plus jamais en sortir.
    """
    touched = set(fields) if fields is not None else {
        name
        for name, value in (("message", message), ("operation", operation))
        if value is not None
    }
    _refuse_past_day(day, paris_today(now))
    row = await get_or_create_row(db, day, now=now)

    if "message" in touched:
        row.message = _clean_text(message)
    if "operation" in touched:
        row.operation = _clean_text(operation)
    await db.flush()

    if touched:
        await JournalService(db).record(
            EVENT_CAHIER_TEXT_UPDATED,
            user_id=user_id,
            payload={"day": day.isoformat(), "fields": sorted(touched)},
        )
    return await read_day(db, day, now=now)


def _clean_text(value: str | None) -> str | None:
    """Texte libre normalise : vide -> `None` (le champ redevient vierge)."""
    if value is None:
        return None
    cleaned = value.strip()[:TEXT_MAX_LENGTH]
    return cleaned or None


async def sign(
    db: AsyncSession,
    day: date_cls,
    *,
    role: str,
    name: str | None = None,
    user,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Appose une signature (manager ou equipe) sur la journee.

    Manager : le compte connecte, rien a saisir. Equipe : la vendeuse
    identifiee sur le tiroir si la caisse en porte une — c'est elle qui tient
    la boutique, son nom n'a pas a etre retape — sinon un nom libre, exige.

    Une signature ne se reprend pas (`already_signed`) : on ne signe pas deux
    fois la meme journee, et on ne reecrit pas la signature de quelqu'un
    d'autre.
    """
    reference_now = datetime.now(_PARIS) if now is None else now
    _refuse_past_day(day, paris_today(now))
    row = await get_or_create_row(db, day, now=now)

    if role == "manager":
        if row.manager_signed_at is not None:
            raise _already_signed()
        row.manager_signed_at = reference_now
        row.manager_signed_by_user_id = user.id
    else:
        if row.team_signed_at is not None:
            raise _already_signed()
        cashier = await _drawer_cashier(db)
        signer = cashier.display_name if cashier is not None else (name or "").strip()
        if not signer:
            raise PosServiceError(
                "Aucune vendeuse identifiée en caisse : indiquez le nom de la "
                "personne qui signe.",
                code="name_required",
                status_code=422,
            )
        row.team_signed_at = reference_now
        row.team_signed_by_name = signer[:60]
        row.team_signed_by_cashier_id = cashier.id if cashier is not None else None

    await db.flush()
    # Le JET retient QUI a signe QUOI et QUAND par la seule identite
    # technique : ni le nom saisi, ni le message du jour n'y figurent.
    await JournalService(db).record(
        EVENT_CAHIER_SIGNED,
        user_id=user.id,
        payload={"day": day.isoformat(), "role": role},
    )
    return await read_day(db, day, now=now)


def _already_signed() -> PosServiceError:
    return PosServiceError(
        "Cette journée est déjà signée.",
        code="already_signed",
        status_code=409,
    )


async def _drawer_cashier(db: AsyncSession):
    """Vendeuse identifiee sur le tiroir ouvert, s'il y en a une.

    `cash_drawers.current_cashier_id` est l'etat COURANT de la caisse (PR8) :
    c'est deja lui qui est recopie sur chaque vente, c'est donc lui qui
    signe pour l'equipe.
    """
    from app.models.pos import CashDrawer
    from app.services.cashier_service import CashierService

    drawer = (
        await db.execute(select(CashDrawer).where(CashDrawer.is_open.is_(True)).limit(1))
    ).scalar_one_or_none()
    return await CashierService(db).current_for_drawer(drawer)


async def set_weekday_open(db: AsyncSession, opens: list[bool], *, user_id) -> list[bool]:
    """Enregistre les jours d'ouverture (reglage `cahier`).

    Passe par `SettingsService.set`, comme tout reglage : l'ecriture est
    serialisee par le verrou dedie et journalisee au JET (`config.changed`,
    avec son diff avant/apres). Les journees deja consultees gardent leur
    objectif fige — changer les jours d'ouverture n'a d'effet que sur les
    journees a venir.
    """
    normalized = normalize_weekday_open(opens)
    cahier = await SettingsService(db).get("cahier")
    await SettingsService(db).set(
        "cahier", {**cahier, "weekday_open": normalized}, user_id=user_id
    )
    return normalized
