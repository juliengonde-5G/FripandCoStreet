# Nouveau routeur (PR9, docs/ARCHITECTURE_PR9.md, contrat K2) — lecture et
# purge du journal des echanges SumUp, et analyse des echecs de paiement
# carte.
#
# Bloc autonome plutot qu'un ajout a `api/admin/router.py` (deja tres long) :
# meme prefixe `/admin`, meme authentification JWT, mais un fichier qu'on
# peut lire d'un bloc quand on cherche pourquoi un terminal fait des
# siennes.
#
# Aucune de ces routes ne touche a une donnee fiscale : `sumup_exchanges` est
# une table d'exploitation, purgeable, dans laquelle aucune vente ne nait.
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.payment_attempt import PaymentAttempt, PaymentAttemptStatus
from app.models.sumup_exchange import SumUpExchange
from app.models.user import User
from app.services.fiscal import PosServiceError
from app.services.jet import EVENT_SUMUP_EXCHANGES_PURGED, JournalService

router = APIRouter(prefix="/admin", tags=["admin", "payments"])

# Types d'erreur connus du journal (K1). La reponse d'analyse les expose
# TOUS, y compris a zero : un tableau de bord dont les lignes apparaissent
# et disparaissent selon la periode est illisible.
ERROR_TYPES = ("transport", "timeout", "http_4xx", "http_5xx", "decode")


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _serialize_exchange(row: SumUpExchange) -> dict:
    return {
        "id": str(row.id),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "operation": row.operation,
        "method": row.method,
        "url_path": row.url_path,
        "request_payload": row.request_payload,
        "response_status": row.response_status,
        "response_payload": row.response_payload,
        "duration_ms": row.duration_ms,
        "retry_count": row.retry_count,
        "is_error": row.is_error,
        "error_type": row.error_type,
        "error_message": row.error_message,
        "checkout_id": row.checkout_id,
        "client_transaction_id": row.client_transaction_id,
        "request_id": row.request_id,
    }


def _parse_bound(value: str | None, label: str) -> datetime | None:
    """Lit une borne ISO 8601 et la ramene en UTC.

    Une borne illisible est une erreur 422 explicite plutot qu'un filtre
    silencieusement ignore : un manager qui filtre mal doit le savoir, sinon
    il conclura a tort que le journal est vide.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PosServiceError(
            f"Date {label} illisible : {value}", code="invalid_date", status_code=422
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _apply_filters(
    query: Select,
    *,
    only_failed: bool,
    operation: str | None,
    error_type: str | None,
    checkout_id: str | None,
    start: datetime | None,
    end: datetime | None,
) -> Select:
    if only_failed:
        query = query.where(SumUpExchange.is_error.is_(True))
    if operation:
        query = query.where(SumUpExchange.operation == operation)
    if error_type:
        query = query.where(SumUpExchange.error_type == error_type)
    if checkout_id:
        query = query.where(SumUpExchange.checkout_id == checkout_id)
    if start is not None:
        query = query.where(SumUpExchange.created_at >= start)
    if end is not None:
        query = query.where(SumUpExchange.created_at <= end)
    return query


# ---------------------------------------------------------------------------
# GET /admin/sumup-exchanges — lecture du journal
# ---------------------------------------------------------------------------


@router.get("/sumup-exchanges")
async def list_sumup_exchanges(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    only_failed: bool = Query(default=False),
    operation: str | None = Query(default=None),
    error_type: str | None = Query(default=None),
    checkout_id: str | None = Query(default=None),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Journal des echanges, du plus recent au plus ancien.

    `total` compte les lignes qui passent les filtres, pas celles renvoyees :
    l'ecran doit pouvoir dire « 100 affichees sur 3 412 » sans quoi on croit
    avoir tout vu.
    """
    start = _parse_bound(date_from, "de début")
    end = _parse_bound(date_to, "de fin")
    filters = {
        "only_failed": only_failed,
        "operation": operation,
        "error_type": error_type,
        "checkout_id": checkout_id,
        "start": start,
        "end": end,
    }
    total = (
        await db.execute(
            _apply_filters(select(func.count(SumUpExchange.id)), **filters)
        )
    ).scalar_one()
    rows = (
        await db.execute(
            _apply_filters(select(SumUpExchange), **filters)
            .order_by(SumUpExchange.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {"exchanges": [_serialize_exchange(row) for row in rows], "total": int(total)}


# ---------------------------------------------------------------------------
# DELETE /admin/sumup-exchanges — purge manuelle
# ---------------------------------------------------------------------------


@router.delete("/sumup-exchanges")
async def purge_sumup_exchanges(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Vide integralement le journal et journalise le geste au JET.

    Le journal est purgeable (table d'exploitation), mais le fait de l'avoir
    vide, lui, ne l'est pas : sans cet evenement, un trou dans les traces
    serait indiscernable d'un terminal qui n'a rien emis.
    """
    total = (await db.execute(select(func.count(SumUpExchange.id)))).scalar_one()
    await db.execute(delete(SumUpExchange))
    await JournalService(db).record(
        EVENT_SUMUP_EXCHANGES_PURGED,
        user_id=user.id,
        username=user.username,
        ip=_client_ip(request),
        request_id=_request_id(request),
        payload={"count": int(total)},
    )
    await db.commit()
    return {"deleted": int(total)}


# ---------------------------------------------------------------------------
# GET /admin/payment-failures — analyse des echecs carte
# ---------------------------------------------------------------------------


async def _retry_counters(db: AsyncSession, since: datetime) -> dict:
    """Compteurs de la file des paiements echoues (table `failed_payments`).

    Code defensif : la table est alimentee par le flux de mise en file, qui
    peut tres bien n'avoir jamais rien ecrit (boutique sans incident carte).
    On retourne alors des zeros, jamais une erreur — cet ecran doit
    s'afficher meme quand tout va bien.
    """
    zero = {"queued": 0, "succeeded": 0, "exhausted": 0, "abandoned": 0}
    try:
        from app.models.failed_payment import FailedPayment, FailedPaymentStatus

        rows = (
            await db.execute(
                select(FailedPayment.status, func.count(FailedPayment.id))
                .where(FailedPayment.created_at >= since)
                .group_by(FailedPayment.status)
            )
        ).all()
    except Exception:  # noqa: BLE001 — cf. docstring
        return zero

    by_status = {
        (status.value if hasattr(status, "value") else str(status)): int(count)
        for status, count in rows
    }
    return {
        # `queued` = tout ce qui est entre en file sur la periode, resolu ou
        # non : c'est le volume d'incidents, pas le reliquat.
        "queued": sum(by_status.values()),
        "succeeded": by_status.get(FailedPaymentStatus.succeeded.value, 0),
        "exhausted": by_status.get(FailedPaymentStatus.exhausted.value, 0),
        "abandoned": by_status.get(FailedPaymentStatus.abandoned.value, 0),
    }


@router.get("/payment-failures")
async def payment_failures(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(default=7, ge=1, le=365),
):
    """Vue d'ensemble des echecs carte sur `days` jours.

    Croise trois sources : les essais de paiement (`payment_attempts`, ce que
    la caisse a vecu), le journal des echanges (`sumup_exchanges`, ce que le
    reseau a vecu) et la file des reessais (`failed_payments`, ce qui a ete
    rattrape). Les trois ensemble disent si le probleme vient du terminal,
    du reseau ou des cartes des clientes.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    attempt_rows = (
        await db.execute(
            select(PaymentAttempt.status, func.count(PaymentAttempt.id))
            .where(PaymentAttempt.created_at >= since)
            .group_by(PaymentAttempt.status)
        )
    ).all()
    by_attempt_status = {status.value: int(count) for status, count in attempt_rows}
    attempts = {
        state.value: by_attempt_status.get(state.value, 0) for state in PaymentAttemptStatus
    }

    top_errors = [
        {"error_message": message, "count": int(count)}
        for message, count in (
            await db.execute(
                select(PaymentAttempt.error_message, func.count(PaymentAttempt.id))
                .where(
                    PaymentAttempt.created_at >= since,
                    PaymentAttempt.error_message.isnot(None),
                    PaymentAttempt.error_message != "",
                )
                .group_by(PaymentAttempt.error_message)
                .order_by(func.count(PaymentAttempt.id).desc())
                .limit(10)
            )
        ).all()
    ]

    error_type_rows = (
        await db.execute(
            select(SumUpExchange.error_type, func.count(SumUpExchange.id))
            .where(SumUpExchange.created_at >= since, SumUpExchange.is_error.is_(True))
            .group_by(SumUpExchange.error_type)
        )
    ).all()
    counted_by_type = {
        (error_type or "unknown"): int(count) for error_type, count in error_type_rows
    }
    exchanges_by_error_type = {name: counted_by_type.get(name, 0) for name in ERROR_TYPES}
    # Un type inattendu (journal ecrit par une version anterieure) ne doit pas
    # disparaitre silencieusement de l'analyse.
    for name, count in counted_by_type.items():
        if name not in exchanges_by_error_type:
            exchanges_by_error_type[name] = count

    by_operation = [
        {"operation": operation, "count": int(count), "errors": int(errors or 0)}
        for operation, count, errors in (
            await db.execute(
                select(
                    SumUpExchange.operation,
                    func.count(SumUpExchange.id),
                    func.count(SumUpExchange.id).filter(SumUpExchange.is_error.is_(True)),
                )
                .where(SumUpExchange.created_at >= since)
                .group_by(SumUpExchange.operation)
                .order_by(func.count(SumUpExchange.id).desc())
            )
        ).all()
    ]

    return {
        "period_days": days,
        "attempts": attempts,
        "top_errors": top_errors,
        "exchanges_by_error_type": exchanges_by_error_type,
        "by_operation": by_operation,
        "retries": await _retry_counters(db, since),
    }
