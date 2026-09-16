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
