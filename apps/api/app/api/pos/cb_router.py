# Nouveau routeur (PR2, §4.5 + §5 ARCHITECTURE_PR2.md) — endpoints CB SumUp.
# Monté par `app/main.py` (agent A) via un import protégé si ce module
# existe. Modelé sur le module équivalent de l'application source
# (`apps/api/app/api/pos/router.py`, bloc
# `payments/cb/*`, L1433-1830), réduit au push-reader seul (D5/D7 : pas de
# mode « lien de paiement »).
from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.payment_attempt import PaymentAttempt, PaymentAttemptStatus
from app.models.pos import CashDrawer
from app.models.user import User
from app.services import failed_payment_service
from app.services.fiscal import PosServiceError
from app.services.jet import JournalService
from app.services.settings_service import SettingsService
from app.services.sumup_exchange_log import persist as persist_exchanges
from app.services.sumup_service import SumUpService, is_test_api_key, redact_sumup_error

router = APIRouter(prefix="/pos/payments/cb", tags=["cb"])

# Types d'événements JET propres au cycle de vie CB (§4.6). Définis ici
# plutôt que dans `app/services/jet.py` (fichier partagé, hors du périmètre
# d'écriture de cet agent) — mêmes chaînes que celles listées au contrat.
EVENT_CB_INITIATED = "payment.cb_initiated"
EVENT_CB_PAID = "payment.cb_paid"
EVENT_CB_FAILED = "payment.cb_failed"
EVENT_CB_CANCELLED = "payment.cb_cancelled"


# ---------------------------------------------------------------------------
# Schémas
# ---------------------------------------------------------------------------


class CbInitiateRequest(BaseModel):
    amount: Decimal = Field(gt=0, decimal_places=2)
    client_uuid: uuid.UUID


# ---------------------------------------------------------------------------
# Cache 15 s du ping reader — au niveau du module (mono-boutique,
# mono-terminal : un seul TPE, donc un seul cache global a du sens).
# ---------------------------------------------------------------------------

_PING_CACHE_TTL_SECONDS = 15.0
_ping_cache: dict = {"result": None, "expires_at": 0.0}


def _reset_ping_cache() -> None:
    """Vide le cache de ping — utilisé par les tests pour isoler les cas."""
    _ping_cache["result"] = None
    _ping_cache["expires_at"] = 0.0


async def _cached_ping(svc: SumUpService) -> dict:
    now = time.monotonic()
    if _ping_cache["result"] is not None and now < _ping_cache["expires_at"]:
        return _ping_cache["result"]
    result = await svc.ping_reader()
    _ping_cache["result"] = result
    _ping_cache["expires_at"] = now + _PING_CACHE_TTL_SECONDS
    return result


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


async def _log_exchanges(db: AsyncSession, svc: SumUpService, request: Request) -> None:
    """Déverse le journal des échanges SumUp (PR9/K1) APRÈS l'opération métier.

    Jamais avant : une trace de débogage ne doit pas peser sur le chemin du
    paiement, et `persist` est écrit pour ne jamais faire échouer son
    appelant (savepoint + log). Le `request_id` corrèle l'échange à la ligne
    de log de la requête entrante.
    """
    await persist_exchanges(db, svc.drain_exchanges(), request_id=_request_id(request))


async def _cb_description(db: AsyncSession) -> str:
    """Libellé envoyé au TPE (PR5, G7, docs/ARCHITECTURE_PR5.md §1) — dérivé
    du nom de boutique courant (`SettingsService.get("shop")`), lu à chaque
    appel (pas de cache : un manager qui renomme la boutique doit voir l'
    effet dès la prochaine vente). Repli si le nom est vide."""
    shop = await SettingsService(db).get("shop")
    name = (shop.get("name") or "").strip() if isinstance(shop, dict) else ""
    return f"Vente {name}" if name else "Vente Frip & Co Street"


async def _find_pending_or_terminal(db: AsyncSession, client_uuid: uuid.UUID) -> PaymentAttempt | None:
    """Dernier essai (le plus avancé) déjà enregistré pour cette vente."""
    return (
        await db.execute(
            select(PaymentAttempt)
            .where(PaymentAttempt.client_uuid == client_uuid)
            .order_by(PaymentAttempt.attempt_count.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _get_attempt(db: AsyncSession, checkout_id: str) -> PaymentAttempt | None:
    return (
        await db.execute(
            select(PaymentAttempt).where(PaymentAttempt.checkout_id == checkout_id)
        )
    ).scalar_one_or_none()


async def _current_cashier_id(db: AsyncSession):
    """Vendeuse identifiee en caisse a cet instant (PR8/J2), ou ``None``.

    Lue sur le tiroir ouvert plutot que demandee au front : c'est la meme
    source que celle recopiee sur chaque vente, donc une ligne en file porte
    le nom de la personne qui etait devant le terminal.
    """
    drawer = (
        await db.execute(select(CashDrawer).where(CashDrawer.is_open.is_(True)).limit(1))
    ).scalar_one_or_none()
    return drawer.current_cashier_id if drawer is not None else None


async def _fail_and_queue(
    db: AsyncSession,
    request: Request,
    current_user: User,
    attempt: PaymentAttempt,
    result: dict,
) -> JSONResponse:
    """Repond 409 `payment_failed` en mettant la vente en file si c'est rattrapable (K3).

    Le corps porte A LA RACINE `recoverable`, `failed_payment_id`,
    `retry_count` et `max_retries` pour que la caisse sache quoi afficher
    sans second appel : « le terminal n'a pas repondu, la vente n'est pas
    perdue » + bouton Reessayer quand la cause est recuperable, simple
    invitation a changer de moyen de paiement quand la carte a ete refusee
    (un declin n'entre jamais en file : il n'y a rien a rejouer). Les memes
    cles sont toujours presentes, a `None` dans ce dernier cas : la forme de
    la reponse ne change pas selon le cas, seules les valeurs changent.

    Reponse construite ici plutot que levee via `PosServiceError` : le
    handler global ne sait produire que `{detail, code}`, et on ne modifie
    pas un contrat d'erreur partage pour des champs propres a la CB — meme
    parti pris que `pos/router.py::identify_cashier` avec `retry_after`.
    """
    recoverable, error_type = failed_payment_service.classify_push_result(result)
    failed_payment = None
    if recoverable:
        failed_payment = await failed_payment_service.enqueue(
            db,
            attempt,
            error_type=error_type,
            last_error=result.get("error_detail") or result.get("error_friendly"),
            cashier_id=await _current_cashier_id(db),
            user_id=current_user.id,
            username=current_user.username,
            ip=_client_ip(request),
            request_id=_request_id(request),
        )

    # L'essai, son evenement JET et l'eventuelle ligne en file sont deja
    # ecrits : on commite explicitement avant de repondre en erreur, sinon
    # une exception ulterieure et le rollback de `get_db` effaceraient cette
    # trace (meme raison que dans `auth/router.py::login`, PR1).
    await db.commit()
    return JSONResponse(
        status_code=409,
        content={
            "detail": (
                result.get("error_friendly") or "Le paiement a été refusé par le terminal."
            ),
            "code": "payment_failed",
            "recoverable": recoverable,
            **failed_payment_service.error_fields(failed_payment),
        },
    )


def _guard_configured_and_key(svc: SumUpService) -> None:
    """Vérifications communes à initiate/retry — lève `PosServiceError` sinon."""
    if not svc.is_configured:
        raise PosServiceError(
            "Aucun terminal de paiement configuré — encaissez en espèces.",
            code="reader_unavailable",
            status_code=409,
        )
    if settings.is_production and is_test_api_key(svc.api_key):
        raise PosServiceError(
            "Une clé SumUp de test est interdite en production.",
            code="test_key_in_production",
            status_code=409,
        )


# ---------------------------------------------------------------------------
# GET /pos/payments/cb/status — état pour activer/désactiver le bouton CB
# ---------------------------------------------------------------------------


@router.get("/status")
async def cb_status(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    svc = SumUpService()
    if not svc.is_configured:
        return {
            "configured": False,
            "reader_id": svc.reader_id or None,
            "reader_online": False,
            "reader_status": "unconfigured",
            "message": "Aucun terminal de paiement configuré — encaissez en espèces.",
        }
    ping = await _cached_ping(svc)
    # PR9/K1 — le pré-vol TPE est justement l'échange que l'on veut relire
    # quand la caissière dit « le bouton CB était grisé ».
    await _log_exchanges(db, svc, request)
    return {
        "configured": True,
        "reader_id": svc.reader_id,
        "reader_online": bool(ping.get("online")),
        "reader_status": ping.get("status"),
        "battery": ping.get("battery_level"),
        "message": ping.get("message"),
    }


# ---------------------------------------------------------------------------
# POST /pos/payments/cb/initiate
# ---------------------------------------------------------------------------


@router.post("/initiate")
async def initiate_cb_payment(
    request: Request,
    body: CbInitiateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Pousse un paiement sur le TPE et journalise l'essai (§4.5).

    Aucune écriture n'a lieu tant que le TPE n'a pas réellement été
    sollicité : une clé absente, une clé de test en production, ou un TPE
    hors ligne renvoient 409 sans créer de `PaymentAttempt` (il n'existe pas
    encore de `checkout_id` à journaliser).
    """
    svc = SumUpService()
    _guard_configured_and_key(svc)

    existing = await _find_pending_or_terminal(db, body.client_uuid)
    if existing is not None:
        if existing.status == PaymentAttemptStatus.pending:
            raise PosServiceError(
                f"Un paiement est déjà en attente pour cette vente "
                f"(checkout_id={existing.checkout_id}).",
                code="attempt_pending",
                status_code=409,
            )
        if existing.status == PaymentAttemptStatus.paid:
            raise PosServiceError(
                f"Cette vente a déjà un paiement CB validé (checkout_id={existing.checkout_id}).",
                code="already_paid",
                status_code=409,
            )
        # failed / cancelled : un essai existe déjà pour ce client_uuid —
        # on ne peut pas repousser sur le MÊME checkout_id (contrainte
        # d'unicité) : la caissière doit utiliser le réessai dédié.
        raise PosServiceError(
            f"Ce paiement CB a déjà échoué pour cette vente "
            f"(checkout_id={existing.checkout_id}) — utilisez le réessai.",
            code="attempt_failed",
            status_code=409,
        )

    ping = await _cached_ping(svc)
    if not ping.get("ready"):
        raise PosServiceError(
            ping.get("message") or "Terminal de paiement indisponible.",
            code="reader_unavailable",
            status_code=409,
        )

    client_transaction_id = str(body.client_uuid)
    description = await _cb_description(db)
    result = await svc._push_to_reader(  # noqa: SLF001 — service interne, même paquet
        amount=body.amount, client_transaction_id=client_transaction_id, description=description
    )
    failed = str(result.get("status", "")).upper() == "FAILED"
    # PR9/K1 — le push est joué, on peut journaliser l'échange (pré-vol
    # compris) : `persist` ne peut pas faire échouer le paiement.
    await _log_exchanges(db, svc, request)

    attempt = PaymentAttempt(
        client_uuid=body.client_uuid,
        amount=body.amount,
        status=PaymentAttemptStatus.failed if failed else PaymentAttemptStatus.pending,
        checkout_id=result.get("checkout_id") or client_transaction_id,
        client_transaction_id=result.get("client_transaction_id") or client_transaction_id,
        reader_id=svc.reader_id,
        error_message=(
            redact_sumup_error(result.get("error_detail") or result.get("error_friendly") or "Refusé par SumUp")
            if failed
            else None
        ),
        attempt_count=1,
    )
    db.add(attempt)
    await db.flush()

    journal = JournalService(db)
    await journal.record(
        EVENT_CB_FAILED if failed else EVENT_CB_INITIATED,
        user_id=current_user.id,
        username=current_user.username,
        ip=_client_ip(request),
        request_id=_request_id(request),
        payload={
            "checkout_id": attempt.checkout_id,
            "amount": str(body.amount),
            "client_uuid": str(body.client_uuid),
        },
    )

    if failed:
        # PR9/K3 : la 409 met la vente en file quand la cause est
        # rattrapable, et porte `failed_payment_id` + `recoverable`.
        return await _fail_and_queue(db, request, current_user, attempt, result)
    return {"checkout_id": attempt.checkout_id, "status": "pending"}


# ---------------------------------------------------------------------------
# GET /pos/payments/cb/{checkout_id}/status
# ---------------------------------------------------------------------------


@router.get("/{checkout_id}/status")
async def get_cb_payment_status(
    checkout_id: str,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    attempt = await _get_attempt(db, checkout_id)
    if attempt is None:
        raise PosServiceError("Paiement CB introuvable.", code="not_found", status_code=404)

    if attempt.status != PaymentAttemptStatus.pending:
        # Déjà résolu — pas de nouveau poll SumUp, pas de nouvel événement JET
        # (transition déjà journalisée une seule fois, §4.5).
        return _status_response(attempt)

    svc = SumUpService()
    poll = await svc.get_checkout_status(checkout_id)
    norm = str(poll.get("status", "PENDING")).upper()
    # PR9/K1 — avant le retour anticipé « pending » : c'est la suite des
    # polls qui raconte ce qu'a fait le terminal.
    await _log_exchanges(db, svc, request)

    if norm == "PENDING":
        return {"status": "pending"}

    journal = JournalService(db)
    payload = {"checkout_id": checkout_id, "amount": str(attempt.amount)}

    if norm == "PAID":
        attempt.status = PaymentAttemptStatus.paid
        attempt.sumup_transaction_id = poll.get("sumup_transaction_id")
        attempt.sumup_transaction_code = poll.get("sumup_transaction_code")
        attempt.sumup_auth_code = poll.get("sumup_auth_code")
        attempt.sumup_card_brand = poll.get("sumup_card_brand")
        attempt.sumup_card_last4 = poll.get("sumup_card_last4")
        await db.flush()
        await journal.record(
            EVENT_CB_PAID,
            user_id=current_user.id,
            username=current_user.username,
            ip=_client_ip(request),
            request_id=_request_id(request),
            payload=payload,
        )
        # PR9/K3 — point de constat n° 1 du `paid` : si cette vente avait un
        # incident en file, il se referme ici. La vente, elle, n'existe pas
        # encore (regle PR2) : `transaction_id` sera pose au constat n° 2,
        # dans `sumup_verify.verify_card_tender`, a l'ecriture de la vente.
        await failed_payment_service.resolve_if_queued(
            db,
            attempt.client_uuid,
            user_id=current_user.id,
            username=current_user.username,
            ip=_client_ip(request),
            request_id=_request_id(request),
        )
    elif norm == "CANCELLED":
        attempt.status = PaymentAttemptStatus.cancelled
        await db.flush()
        await journal.record(
            EVENT_CB_CANCELLED,
            user_id=current_user.id,
            username=current_user.username,
            ip=_client_ip(request),
            request_id=_request_id(request),
            payload=payload,
        )
    else:  # FAILED (ou tout statut non reconnu, prudemment traité en échec)
        attempt.status = PaymentAttemptStatus.failed
        attempt.error_message = redact_sumup_error(
            poll.get("error_friendly") or poll.get("error") or "Paiement refusé"
        )
        await db.flush()
        await journal.record(
            EVENT_CB_FAILED,
            user_id=current_user.id,
            username=current_user.username,
            ip=_client_ip(request),
            request_id=_request_id(request),
            payload=payload,
        )

    return _status_response(attempt)


def _status_response(attempt: PaymentAttempt) -> dict:
    out: dict = {"status": attempt.status.value}
    if attempt.status == PaymentAttemptStatus.paid:
        out["transaction_code"] = attempt.sumup_transaction_code
        out["card_brand"] = attempt.sumup_card_brand
        out["last4"] = attempt.sumup_card_last4
    elif attempt.status == PaymentAttemptStatus.failed:
        out["error"] = attempt.error_message
    return out


# ---------------------------------------------------------------------------
# DELETE /pos/payments/cb/{checkout_id}
# ---------------------------------------------------------------------------


@router.delete("/{checkout_id}")
async def cancel_cb_payment(
    checkout_id: str,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    attempt = await _get_attempt(db, checkout_id)
    if attempt is None:
        raise PosServiceError("Paiement CB introuvable.", code="not_found", status_code=404)
    if attempt.status == PaymentAttemptStatus.paid:
        raise PosServiceError(
            "Ce paiement CB est déjà validé — impossible de l'annuler.",
            code="already_paid",
            status_code=409,
        )
    if attempt.status != PaymentAttemptStatus.pending:
        raise PosServiceError(
            "Ce paiement CB n'est plus en attente.", code="not_pending", status_code=409
        )

    svc = SumUpService()
    ok = await svc.cancel_checkout(checkout_id)
    if not ok:
        # Le TPE a pu refuser l'annulation parce que le client a déjà tapé sa
        # carte (paiement en cours de finalisation côté SumUp) — on revérifie
        # avant de marquer localement « annulé » pour ne jamais créer un écart
        # comptable (client débité mais vente marquée annulée).
        recheck = await svc.get_checkout_status(checkout_id)
        if str(recheck.get("status", "")).upper() == "PAID":
            raise PosServiceError(
                "Le client vient de valider sa carte — ce paiement ne peut plus être annulé.",
                code="already_paid",
                status_code=409,
            )

    # PR9/K1 — couvre l'annulation ET la revérification éventuelle.
    await _log_exchanges(db, svc, request)

    attempt.status = PaymentAttemptStatus.cancelled
    await db.flush()

    journal = JournalService(db)
    await journal.record(
        EVENT_CB_CANCELLED,
        user_id=current_user.id,
        username=current_user.username,
        ip=_client_ip(request),
        request_id=_request_id(request),
        payload={"checkout_id": checkout_id, "amount": str(attempt.amount)},
    )
    return {"cancelled": ok, "checkout_id": checkout_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# POST /pos/payments/cb/{checkout_id}/retry
# ---------------------------------------------------------------------------


@router.post("/{checkout_id}/retry")
async def retry_cb_payment(
    checkout_id: str,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    attempt = await _get_attempt(db, checkout_id)
    if attempt is None:
        raise PosServiceError("Paiement CB introuvable.", code="not_found", status_code=404)
    if attempt.status not in (PaymentAttemptStatus.failed, PaymentAttemptStatus.cancelled):
        raise PosServiceError(
            "Ce paiement CB n'est pas réessayable dans son état actuel.",
            code="not_retryable",
            status_code=409,
        )

    svc = SumUpService()
    _guard_configured_and_key(svc)

    ping = await _cached_ping(svc)
    if not ping.get("ready"):
        raise PosServiceError(
            ping.get("message") or "Terminal de paiement indisponible.",
            code="reader_unavailable",
            status_code=409,
        )

    new_count = attempt.attempt_count + 1
    new_client_transaction_id = f"{attempt.client_uuid}:r{new_count}"
    description = await _cb_description(db)
    result = await svc._push_to_reader(  # noqa: SLF001
        amount=attempt.amount,
        client_transaction_id=new_client_transaction_id,
        description=description,
    )
    failed = str(result.get("status", "")).upper() == "FAILED"
    # PR9/K1 — même journalisation que dans `initiate_cb_payment`.
    await _log_exchanges(db, svc, request)

    new_attempt = PaymentAttempt(
        client_uuid=attempt.client_uuid,
        amount=attempt.amount,
        status=PaymentAttemptStatus.failed if failed else PaymentAttemptStatus.pending,
        checkout_id=result.get("checkout_id") or new_client_transaction_id,
        client_transaction_id=result.get("client_transaction_id") or new_client_transaction_id,
        reader_id=svc.reader_id,
        error_message=(
            redact_sumup_error(result.get("error_detail") or result.get("error_friendly") or "Refusé par SumUp")
            if failed
            else None
        ),
        attempt_count=new_count,
    )
    db.add(new_attempt)
    await db.flush()

    journal = JournalService(db)
    await journal.record(
        EVENT_CB_FAILED if failed else EVENT_CB_INITIATED,
        user_id=current_user.id,
        username=current_user.username,
        ip=_client_ip(request),
        request_id=_request_id(request),
        payload={
            "checkout_id": new_attempt.checkout_id,
            "amount": str(attempt.amount),
            "client_uuid": str(attempt.client_uuid),
            "attempt_count": new_count,
        },
    )

    if failed:
        # Même raison que dans `initiate_cb_payment` : l'essai est déjà écrit,
        # il doit survivre au rollback déclenché par `get_db` — et la vente
        # rejoint la file si la cause est rattrapable (PR9/K3).
        return await _fail_and_queue(db, request, current_user, new_attempt, result)
    return {"checkout_id": new_attempt.checkout_id, "status": "pending"}


# ---------------------------------------------------------------------------
# POST /pos/payments/cb/retry-failed/{failed_payment_id} (PR9, K3)
#
# Le réessai est un GESTE DE LA VENDEUSE, devant la cliente : pas de tâche de
# fond qui relancerait un terminal sans personne pour surveiller ce qui
# s'affiche dessus (hors périmètre, §2 du contrat). La route ne crée aucune
# vente : elle repousse un paiement, le polling existant conclura.
#
# Déclarée après `/{checkout_id}/retry` sans ambiguïté de routage : ce
# dernier exige « retry » en second segment, là où l'on a ici un identifiant.
# ---------------------------------------------------------------------------


@router.post("/retry-failed/{failed_payment_id}")
async def retry_failed_payment(
    failed_payment_id: uuid.UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    failed_payment = await failed_payment_service.get_or_404(db, failed_payment_id)

    svc = SumUpService()
    _guard_configured_and_key(svc)

    ping = await _cached_ping(svc)
    if not ping.get("ready"):
        raise PosServiceError(
            ping.get("message") or "Terminal de paiement indisponible.",
            code="reader_unavailable",
            status_code=409,
        )

    description = await _cb_description(db)
    try:
        outcome = await failed_payment_service.retry(
            db,
            failed_payment,
            svc=svc,
            description=description,
            user_id=current_user.id,
            username=current_user.username,
            ip=_client_ip(request),
            request_id=_request_id(request),
        )
    except failed_payment_service.RetriesExhausted as exc:
        # Corps enrichi (`failed_payment_id`, `retry_count`, `max_retries` à
        # la racine) : la caisse affiche « réessais épuisés » avec le
        # compteur, sans second appel. Rien n'a été écrit ici — pas de push,
        # donc pas de commit à forcer.
        return JSONResponse(status_code=409, content=exc.as_body())
    attempt = outcome.attempt
    new_checkout_id = attempt.checkout_id
    new_attempt_count = attempt.attempt_count
    # PR9/K1 — même journalisation des échanges que sur les autres routes de
    # push : un réessai est précisément l'échange que l'on relira pour
    # comprendre pourquoi le terminal restait muet.
    await _log_exchanges(db, svc, request)

    await JournalService(db).record(
        EVENT_CB_FAILED if outcome.failed else EVENT_CB_INITIATED,
        user_id=current_user.id,
        username=current_user.username,
        ip=_client_ip(request),
        request_id=_request_id(request),
        payload={
            "checkout_id": new_checkout_id,
            "amount": str(failed_payment.amount),
            "client_uuid": str(failed_payment.client_uuid),
            "attempt_count": new_attempt_count,
        },
    )

    serialized = await failed_payment_service.snapshot(db, failed_payment)
    # Le checkout à repoller est celui du nouvel essai, pas celui de l'essai
    # d'origine auquel la ligne en file reste rattachée.
    serialized["checkout_id"] = new_checkout_id

    if outcome.failed:
        # Le nouvel essai, le compteur de réessais et l'éventuel passage en
        # « réessais épuisés » sont déjà écrits : ils doivent survivre au
        # rollback de `get_db` (même raison que dans `initiate`).
        await db.commit()
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    outcome.result.get("error_friendly")
                    or "Le paiement a été refusé par le terminal."
                ),
                "code": "payment_failed",
                "recoverable": failed_payment_service.classify_push_result(outcome.result)[0],
                "failed_payment_id": str(failed_payment.id),
                "retry_count": serialized["retry_count"],
                "max_retries": serialized["max_retries"],
                "failed_payment": serialized,
            },
        )

    return {
        "checkout_id": new_checkout_id,
        "status": "pending",
        # À plat, ce dont la caisse a besoin pour reprendre son suivi et
        # afficher « réessai 2 sur 3 » ; la ligne complète suit pour l'écran
        # d'administration.
        "failed_payment_id": str(failed_payment.id),
        "retry_count": serialized["retry_count"],
        "failed_payment": serialized,
    }
