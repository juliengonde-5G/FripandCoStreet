# Nouveau service (PR9, docs/ARCHITECTURE_PR9.md, contrat K1) — deversement
# en base du journal des echanges SumUp accumule en memoire par
# `SumUpService`.
#
# Regle cardinale de ce module : il n'echoue JAMAIS son appelant. Une trace
# de debogage qui ne s'ecrit pas ne doit pas faire perdre une vente a la
# boutique. C'est l'exact oppose du JET (`services/jet.py`), ou un echec
# d'ecriture fait echouer la requete : le JET prouve, `sumup_exchanges`
# depanne.
#
# Concretement, l'ecriture se fait dans un SAVEPOINT (`begin_nested`) : si
# elle echoue, seul le point de sauvegarde est annule et la transaction de
# l'appelant reste utilisable pour committer le paiement. Sans savepoint, un
# flush en erreur empoisonnerait la session et ferait tomber le commit
# suivant — soit precisement ce qu'on veut eviter.
#
# Deux voies d'ecriture, selon le sort de la transaction appelante :
#   - `persist` : dans la session de l'appelant, quand celle-ci va etre
#     commitee. C'est le chemin normal.
#   - `persist_detached` : dans une session INDEPENDANTE, immediatement
#     commitee, quand l'appelant s'apprete a lever et que sa transaction va
#     donc etre annulee. Sans cela, les echanges d'une operation RATEE — les
#     seuls qu'on ira vraiment relire au debogage — disparaitraient avec le
#     rollback.
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sumup_exchange import SumUpExchange
from app.services.sumup_service import ExchangeRecord

logger = logging.getLogger("fripco")

# Bornes du reglage `payments.exchange_retention_days` (contrat K1).
RETENTION_DAYS_DEFAULT = 90
RETENTION_DAYS_MIN = 7
RETENTION_DAYS_MAX = 730


def clamp_retention_days(value) -> int:
    """Ramene une retention lue en base dans les bornes 7-730 jours.

    Tolerant aux valeurs heritees d'un JSONB ecrit a la main (chaine, None) :
    un reglage aberrant ne doit pas empecher la purge nocturne de tourner.
    """
    try:
        days = int(value)
    except (TypeError, ValueError):
        return RETENTION_DAYS_DEFAULT
    return max(RETENTION_DAYS_MIN, min(RETENTION_DAYS_MAX, days))


def _row(record: ExchangeRecord, request_id: str | None) -> SumUpExchange:
    """Transpose un echange en memoire vers sa ligne de journal.

    Les troncatures reprennent les largeurs du schema : une trace un peu
    rabotee vaut mieux qu'une trace refusee par la base.
    """
    return SumUpExchange(
        operation=record.operation[:40],
        method=record.method[:10],
        url_path=record.url_path[:300],
        request_payload=record.request_payload,
        response_status=record.response_status,
        response_payload=record.response_payload,
        duration_ms=record.duration_ms,
        retry_count=record.retry_count,
        is_error=record.is_error,
        error_type=(record.error_type or None),
        error_message=(record.error_message or None),
        checkout_id=(record.checkout_id or None),
        client_transaction_id=(record.client_transaction_id or None),
        request_id=(record.request_id or request_id),
    )


async def persist(
    db: AsyncSession,
    records: list[ExchangeRecord] | None,
    *,
    request_id: str | None = None,
) -> int:
    """Ecrit les echanges accumules et retourne le nombre de lignes ajoutees.

    A utiliser quand la session de l'appelant sera commitee. Si l'appelant
    s'apprete au contraire a lever (operation refusee), passer par
    :func:`persist_detached`, sinon le rollback emporte la trace.

    Ne leve jamais : toute erreur est journalisee cote serveur et renvoie 0.
    La liste passee est videe dans tous les cas, pour qu'un appelant qui
    reutilise le meme service ne reecrive pas deux fois les memes lignes.
    """
    if not records:
        return 0
    pending = list(records)
    records.clear()
    try:
        async with db.begin_nested():
            for record in pending:
                db.add(_row(record, request_id))
        return len(pending)
    except Exception:  # noqa: BLE001 — cf. docstring du module
        logger.exception(
            "Journal des échanges SumUp non écrit (%d ligne(s)) — le paiement n'est pas affecté",
            len(pending),
        )
        return 0


async def persist_detached(
    records: list[ExchangeRecord] | None,
    *,
    request_id: str | None = None,
) -> int:
    """Ecrit les echanges dans une session INDEPENDANTE, commitee aussitot.

    Pour les chemins ou l'appelant va lever : sa transaction sera annulee par
    `get_db`, et un `persist` ordinaire y perdrait la trace. Or ce sont
    exactement les echanges d'une operation ratee qu'on ira relire — un
    remboursement refuse par SumUp, par exemple. Une session a part echappe
    a ce rollback.

    Ne leve jamais, comme `persist` : le journal ne doit jamais changer le
    sort de l'operation metier, meme deja perdue.
    """
    if not records:
        return 0
    pending = list(records)
    records.clear()
    try:
        from app.core.database import async_session

        async with async_session() as db:
            for record in pending:
                db.add(_row(record, request_id))
            await db.commit()
        return len(pending)
    except Exception:  # noqa: BLE001 — cf. docstring
        logger.exception(
            "Journal des échanges SumUp non écrit hors transaction (%d ligne(s))",
            len(pending),
        )
        return 0


async def purge(db: AsyncSession, retention_days: int) -> int:
    """Supprime les echanges plus vieux que `retention_days` et retourne le compte.

    Table d'exploitation, hors perimetre fiscal : rien n'interdit de la
    purger (aucune vente n'y nait, aucun trigger d'immuabilite).
    """
    days = clamp_retention_days(retention_days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(delete(SumUpExchange).where(SumUpExchange.created_at < cutoff))
    return int(result.rowcount or 0)
