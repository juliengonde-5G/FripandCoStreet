# Nouveau routeur (PR2, §4.1/§4.3/§5 ARCHITECTURE_PR2.md) — vente, caisse
# especes, tickets, Z. Le bloc CB (`/pos/payments/cb/*`) vit dans
# `cb_router.py` (agent B) : voir le montage protege dans `app/main.py`.
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.cash_movement import CashMovementDirection, CashMovementReason
from app.models.client import ConsentPurpose, ConsentSource
from app.models.pos import CashDrawer, Transaction, TransactionType, ZReport
from app.models.receipt import Receipt
from app.models.user import User
from app.services import escpos_service
from app.services.cashier_service import (
    CashierService,
    PinRateLimited,
    sales_by_cashier,
    serialize_cashier,
)
from app.services.client_service import ClientService, mask_email, mask_phone
from app.services.fiscal import FiscalService, PosServiceError
from app.services.jet import (
    EVENT_DRAWER_KICKED,
    EVENT_EXPORT_DOWNLOADED,
    EVENT_PRINTER_UNREACHABLE,
    EVENT_RECEIPT_DUPLICATE,
    EVENT_RECEIPT_PRINTED,
    EVENT_Z_REGULARIZATION,
    JournalService,
)
from app.services.pos import PosService
from app.services.refund import RefundService
from app.services.settings_service import SettingsService

from .schemas import (
    CancelTransactionRequest,
    CashMovementIn,
    CloseDrawerRequest,
    CreateTransactionRequest,
    OpenDrawerRequest,
    RegularizationRequest,
)

logger = logging.getLogger("fripco")

router = APIRouter(prefix="/pos", tags=["pos"])


# ---------------------------------------------------------------------------
# PR3 (§4 ARCHITECTURE_PR3.md) — client + ticket par e-mail. Schemas définis
# ici (pas dans `schemas.py`, hors périmètre de cet agent — voir §6 du
# contrat) plutôt que d'y toucher.
# ---------------------------------------------------------------------------


class AttachClientRequest(BaseModel):
    email: str
    first_name: str | None = None
    last_name: str | None = None
    newsletter_optin: bool = False
    send_receipt: bool = True


class ResendReceiptEmailRequest(BaseModel):
    email: str | None = None


# ---------------------------------------------------------------------------
# PR7 (docs/ARCHITECTURE_PR7.md, I3) — client en caisse, sans fidelite.
# Schemas definis ici, comme ceux de PR3 ci-dessus.
# ---------------------------------------------------------------------------


class CreatePosClientRequest(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    # Aucun des deux n'est obligatoire PRIS ISOLEMENT, mais l'un des deux
    # l'est : la regle est portee par le service (`contact_required`) et par
    # la base (contrainte CHECK, migration 0007), jamais par Pydantic — pour
    # que l'erreur soit une erreur METIER `{detail, code}` lisible en caisse.
    email: str | None = None
    phone: str | None = None
    newsletter_optin: bool = False


def _client_ip(request: Request) -> str | None:
    """IP client en tenant compte de X-Forwarded-For (Caddy est en frontal)
    — meme extraction que `api/auth/router.py`, utilisee ici pour le
    rate-limit du code PIN (PR8/J2)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _raise(exc: PosServiceError):
    """Re-leve l'erreur metier telle quelle.

    Bug corrige (passe d'integration) : ce helper enveloppait auparavant
    `exc` dans `HTTPException(detail={"detail":..., "code":...})`, ce qui
    produit cote HTTP un corps IMBRIQUE `{"detail": {"detail":..., "code":
    ...}}` — `body.detail` cote front devient alors un objet, pas la phrase
    attendue (symptome observe : "[object Object]"). `PosServiceError` est
    deja gere par `app.main::pos_exception_handler`, qui construit le corps
    PLAT `{"detail": "phrase francaise", "code": "snake_case"}` exige par le
    contrat (§5) — il suffit donc de laisser l'exception remonter telle
    quelle jusqu'a lui, jamais de la reemballer dans une HTTPException ici.
    """
    raise exc


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _serialize_item(item) -> dict:
    return {
        "id": str(item.id),
        "label": item.label,
        "quantity": item.quantity,
        "unit_price": float(item.unit_price),
        "discount_amount": float(item.discount_amount),
        "line_total": float(item.line_total),
        "tva_rate": float(item.tva_rate),
        "line_ht": float(item.line_ht),
        "line_tva": float(item.line_tva),
        "position": item.position,
        "original_transaction_item_id": (
            str(item.original_transaction_item_id) if item.original_transaction_item_id else None
        ),
    }


def _serialize_payment(payment) -> dict:
    return {
        "id": str(payment.id),
        "method": payment.method.value,
        "amount": float(payment.amount),
        "tendered_amount": (
            float(payment.tendered_amount) if payment.tendered_amount is not None else None
        ),
        "change_amount": (
            float(payment.change_amount) if payment.change_amount is not None else None
        ),
        "sumup_checkout_id": payment.sumup_checkout_id,
        "sumup_transaction_id": payment.sumup_transaction_id,
        "sumup_transaction_code": payment.sumup_transaction_code,
        "sumup_auth_code": payment.sumup_auth_code,
        "sumup_card_brand": payment.sumup_card_brand,
        "sumup_card_last4": payment.sumup_card_last4,
        "sumup_refunded_amount": (
            float(payment.sumup_refunded_amount) if payment.sumup_refunded_amount is not None else None
        ),
    }


def _serialize_transaction(
    transaction: Transaction,
    *,
    receipt_text: str | None = None,
    refund_of_sale: dict[str, str] | None = None,
    original_number_by_refund_id: dict[str, int] | None = None,
    clients_by_id: dict[str, dict] | None = None,
    invoice_numbers: dict[str, str] | None = None,
) -> dict:
    """Serialise une transaction.

    ``refund_of_sale``/``original_number_by_refund_id`` viennent de
    `_load_refund_links` (une seule requete agregee pour toute la page
    appelante, jamais une requete par transaction — voir ce helper) :
    - pour une VENTE : ``cancelled`` = elle a une annulation (mapping
      ``refund_of_sale``), ``refund_transaction_id`` = son id ou null.
    - pour une ANNULATION : ``cancelled`` est toujours false (on n'annule
      pas une annulation, D4/`not_a_sale`), ``original_transaction_number``
      = le n° de la vente d'origine, pour l'affichage cote front.

    ``clients_by_id`` (PR3, §4/complement client "Tickets du jour" —
    voir `_load_clients`) mappe ``str(client_id) -> {id, email,
    first_name, last_name}`` pour TOUTE la page appelante — jamais une
    requete par transaction. ``client`` vaut ``None`` quand `client_id`
    est absent, ou (defensif) quand l'appelant n'a pas fourni le mapping.

    ``invoice_numbers`` (PR8/J5, voir `_load_invoice_numbers`) mappe de la
    meme facon ``str(transaction_id) -> invoice_number`` : ``invoice_number``
    vaut ``F-AAAA-NNNN`` sur une vente facturee, ``A-AAAA-NNNN`` sur
    l'annulation d'une vente facturee, ``None`` partout ailleurs.
    """
    refund_of_sale = refund_of_sale or {}
    original_number_by_refund_id = original_number_by_refund_id or {}
    clients_by_id = clients_by_id or {}
    invoice_numbers = invoice_numbers or {}
    tx_id = str(transaction.id)
    is_refund = transaction.transaction_type == TransactionType.refund
    refund_transaction_id = None if is_refund else refund_of_sale.get(tx_id)
    return {
        "id": tx_id,
        "transaction_number": transaction.transaction_number,
        "transaction_type": transaction.transaction_type.value,
        "user_id": str(transaction.user_id),
        "client_uuid": str(transaction.client_uuid) if transaction.client_uuid else None,
        "client": clients_by_id.get(str(transaction.client_id)) if transaction.client_id else None,
        "original_transaction_id": (
            str(transaction.original_transaction_id) if transaction.original_transaction_id else None
        ),
        "original_transaction_number": (
            original_number_by_refund_id.get(tx_id) if is_refund else None
        ),
        "cancelled": False if is_refund else refund_transaction_id is not None,
        "refund_transaction_id": refund_transaction_id,
        "refund_reason": transaction.refund_reason,
        "invoice_number": invoice_numbers.get(tx_id),
        "discount_type": transaction.discount_type.value if transaction.discount_type else None,
        "discount_value": (
            float(transaction.discount_value) if transaction.discount_value is not None else None
        ),
        "discount_amount": float(transaction.discount_amount),
        "tva_rate": float(transaction.tva_rate),
        "total_ht": float(transaction.total_ht),
        "total_tva": float(transaction.total_tva),
        "total_ttc": float(transaction.total_ttc),
        "hash_chain": transaction.hash_chain,
        "receipt_number": transaction.receipt_number,
        "created_at": transaction.created_at.isoformat() if transaction.created_at else None,
        "items": [_serialize_item(i) for i in sorted(transaction.items or [], key=lambda i: i.position)],
        "payments": [_serialize_payment(p) for p in (transaction.payments or [])],
        "receipt_text": receipt_text,
    }


def _serialize_drawer(drawer: CashDrawer) -> dict:
    return {
        "id": str(drawer.id),
        "opened_at": drawer.opened_at.isoformat() if drawer.opened_at else None,
        "closed_at": drawer.closed_at.isoformat() if drawer.closed_at else None,
        "opening_amount": float(drawer.opening_amount),
        "closing_amount": float(drawer.closing_amount) if drawer.closing_amount is not None else None,
        "expected_amount": float(drawer.expected_amount) if drawer.expected_amount is not None else None,
        "discrepancy": float(drawer.discrepancy) if drawer.discrepancy is not None else None,
        "is_open": drawer.is_open,
        "closed_by_guard": drawer.closed_by_guard,
        "z_report_id": str(drawer.z_report_id) if drawer.z_report_id else None,
        # PR8/J2 — identites de caisse : qui a ouvert, qui tient la caisse
        # en ce moment (la releve change la seconde, jamais la premiere).
        "opened_by_cashier_id": (
            str(drawer.opened_by_cashier_id) if drawer.opened_by_cashier_id else None
        ),
        "current_cashier_id": (
            str(drawer.current_cashier_id) if drawer.current_cashier_id else None
        ),
    }


def _serialize_movement(movement) -> dict:
    return {
        "id": str(movement.id),
        "drawer_id": str(movement.drawer_id),
        "direction": movement.direction.value,
        "amount": float(movement.amount),
        "reason": movement.reason.value,
        "note": movement.note,
        "created_at": movement.created_at.isoformat() if movement.created_at else None,
    }


def _serialize_z_report(z: ZReport, by_cashier: list[dict] | None = None) -> dict:
    """Serialise un Z. `by_cashier` (PR8/J2) est la ventilation des ventes
    par vendeuse, recalculee a la lecture (voir
    `cashier_service.sales_by_cashier`) : elle n'est pas scellee dans le Z,
    parce qu'elle se rededuit exactement des ventes de la periode, qui sont
    immuables — et qu'ajouter un champ au payload signe serait une
    evolution fiscale majeure."""
    return {
        "id": str(z.id),
        "report_number": z.report_number,
        "cash_drawer_id": str(z.cash_drawer_id),
        "opened_at": z.opened_at.isoformat() if z.opened_at else None,
        "closed_at": z.closed_at.isoformat() if z.closed_at else None,
        "total_sales": float(z.total_sales),
        "total_refunds": float(z.total_refunds),
        "total_net": float(z.total_net),
        "total_ht": float(z.total_ht),
        "total_tva": float(z.total_tva),
        "transaction_count": z.transaction_count,
        "first_transaction_number": z.first_transaction_number,
        "last_transaction_number": z.last_transaction_number,
        "last_transaction_hash": z.last_transaction_hash,
        "payment_totals": z.payment_totals,
        "opening_amount": float(z.opening_amount),
        "closing_amount": float(z.closing_amount),
        "expected_amount": float(z.expected_amount),
        "discrepancy": float(z.discrepancy),
        "cash_in_total": float(z.cash_in_total),
        "cash_out_total": float(z.cash_out_total),
        "cash_movement_count": z.cash_movement_count,
        "counted": z.counted,
        "is_regularization": z.is_regularization,
        "regularization_reason": z.regularization_reason,
        "cumulative_sales": float(z.cumulative_sales),
        "cumulative_refunds": float(z.cumulative_refunds),
        "cumulative_net": float(z.cumulative_net),
        "cumulative_transaction_count": z.cumulative_transaction_count,
        "hash": z.hash,
        "previous_hash": z.previous_hash,
        "created_at": z.created_at.isoformat() if z.created_at else None,
        "cashier_id": str(z.cashier_id) if z.cashier_id else None,
        "by_cashier": by_cashier if by_cashier is not None else [],
    }


async def _load_refund_links(
    db: AsyncSession, transaction_ids: list[uuid.UUID]
) -> tuple[dict[str, str], dict[str, int]]:
    """Charge, en UNE seule requete agregee (auto-jointure), les liens
    annulation <-> vente pour toutes les transactions d'une page.

    Sans ce helper, marquer `cancelled`/`refund_transaction_id` sur une
    liste de N transactions couterait une requete par ligne (N+1) — ici un
    seul `SELECT ... JOIN transactions AS original` couvre a la fois :
    - les VENTES de la page (recherche d'une annulation dont
      `original_transaction_id` pointe vers l'une d'elles) ;
    - les ANNULATIONS de la page (recherche du numero de leur vente d'origine).

    Retourne ``(refund_of_sale, original_number_by_refund_id)`` — voir
    `_serialize_transaction`.
    """
    if not transaction_ids:
        return {}, {}
    refund = aliased(Transaction)
    original = aliased(Transaction)
    rows = (
        await db.execute(
            select(refund.id, refund.original_transaction_id, original.transaction_number)
            .join(original, refund.original_transaction_id == original.id)
            .where(
                refund.transaction_type == TransactionType.refund,
                or_(
                    refund.original_transaction_id.in_(transaction_ids),
                    refund.id.in_(transaction_ids),
                ),
            )
        )
    ).all()
    refund_of_sale: dict[str, str] = {}
    original_number_by_refund_id: dict[str, int] = {}
    for refund_id, original_id, original_number in rows:
        refund_of_sale[str(original_id)] = str(refund_id)
        original_number_by_refund_id[str(refund_id)] = original_number
    return refund_of_sale, original_number_by_refund_id


async def _load_invoice_numbers(
    db: AsyncSession, transaction_ids: list[uuid.UUID]
) -> dict[str, str]:
    """Numeros de facture/avoir d'une page de transactions (PR8/J5), en UNE
    requete — meme principe que `_load_clients`."""
    from app.services.invoice_service import InvoiceService

    return await InvoiceService(db).numbers_by_transaction(transaction_ids)


async def _load_clients(db: AsyncSession, client_ids: list[uuid.UUID | None]) -> dict[str, dict]:
    """Charge, en UNE seule requete, les clients rattaches a une page de
    transactions (PR3) — evite le N+1 que ferait une requete par
    transaction. Utilise par `_serialize_transaction` (``clients_by_id``)."""
    ids = {cid for cid in client_ids if cid is not None}
    if not ids:
        return {}
    from app.models.client import Client

    rows = (await db.execute(select(Client).where(Client.id.in_(ids)))).scalars().all()
    return {
        str(c.id): {
            "id": str(c.id),
            "email": c.email,
            "first_name": c.first_name,
            "last_name": c.last_name,
        }
        for c in rows
    }


# ---------------------------------------------------------------------------
# Caisse espèces (§4.3)
# ---------------------------------------------------------------------------


async def _by_cashier(db: AsyncSession, z: ZReport) -> list[dict]:
    """Ventilation des ventes par vendeuse d'UN Z (PR8/J2)."""
    return (await sales_by_cashier(db, [z])).get(str(z.id), [])


# ---------------------------------------------------------------------------
# Vendeuses en caisse (PR8, docs/ARCHITECTURE_PR8.md J2) — identification
# par code PIN et releve. Ce ne sont PAS des comptes : la route reste
# protegee par le JWT du compte manager unique, le PIN ne fait qu'identifier
# QUI encaisse derriere ce compte.
# ---------------------------------------------------------------------------


class IdentifyCashierRequest(BaseModel):
    cashier_id: uuid.UUID
    # 4 chiffres — la validation reelle (et le refus des suites triviales a
    # la definition) vit dans `cashier_service.validate_pin`. Ici, on borne
    # seulement la taille pour ne pas hacher n'importe quoi.
    pin: str = Field(min_length=1, max_length=16)


@router.get("/cashiers")
async def list_pos_cashiers(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Vendeuses ACTIVES, pour les cartes de l'ecran d'identification.

    `has_pin` permet au front de griser une vendeuse dont le code n'a pas
    encore ete defini par le manager. Le hash, lui, ne sort jamais.
    """
    cashiers = await CashierService(db).list(only_active=True)
    return {
        "cashiers": [
            {
                "id": str(c.id),
                "display_name": c.display_name,
                "has_pin": c.pin_hash is not None,
            }
            for c in cashiers
        ]
    }


@router.post("/cashiers/identify")
async def identify_cashier(
    body: IdentifyCashierRequest,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Identifie une vendeuse par son code PIN (J2).

    Le `db.commit()` du chemin d'ERREUR n'est pas un oubli : le refus vient
    d'ecrire `cashier.pin_rejected` au JET, et le rollback implicite de
    `get_db` effacerait cet evenement de securite (meme discipline que
    `/auth/login`).
    """
    try:
        cashier = await CashierService(db).identify(
            cashier_id=body.cashier_id,
            pin=body.pin,
            ip=_client_ip(request),
            user_id=user.id,
        )
    except PinRateLimited as exc:
        await db.commit()
        # `Retry-After` en plus du corps `{detail, code}` : l'ecran de
        # caisse affiche un compte a rebours plutot qu'un message fige.
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc), "code": exc.code, "retry_after": exc.retry_after},
            headers={"Retry-After": str(exc.retry_after)},
        )
    except PosServiceError as exc:
        await db.commit()
        _raise(exc)
    await db.commit()
    return {"cashier": serialize_cashier(cashier)}


@router.post("/cashiers/release")
async def release_cashier(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Releve : plus personne n'est identifie en caisse (J2).

    Toujours 200, meme si personne ne tenait la caisse : l'etat voulu est
    atteint. `released` dit si une identite a effectivement ete retiree.
    """
    cashier = await CashierService(db).release(user_id=user.id)
    await db.commit()
    return {
        "released": cashier is not None,
        "cashier": serialize_cashier(cashier) if cashier is not None else None,
    }


@router.get("/drawer/current")
async def drawer_current(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    pos = PosService(db)
    drawer = await pos.get_open_drawer()
    if drawer is None:
        # Caisse fermee : il n'existe aucun tiroir ou inscrire l'identite
        # courante (PR8/J2) — le front conserve la vendeuse identifiee
        # avant l'ouverture et la renvoie dans `POST /pos/drawer/open`.
        return {"open": False, "current_cashier": None}
    today = await pos.drawer_snapshot(drawer)
    cashier = await CashierService(db).current_for_drawer(drawer)
    return {
        "open": True,
        "drawer": _serialize_drawer(drawer),
        "today": today,
        "current_cashier": serialize_cashier(cashier) if cashier is not None else None,
    }


@router.post("/drawer/open")
async def drawer_open(
    body: OpenDrawerRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        drawer = await PosService(db).open_drawer(
            user_id=user.id,
            opening_amount=body.opening_amount,
            breakdown=[d.model_dump() for d in body.breakdown] if body.breakdown else None,
            cashier_id=body.cashier_id,
        )
    except PosServiceError as exc:
        _raise(exc)
    await db.commit()
    return _serialize_drawer(drawer)


@router.post("/drawer/close")
async def drawer_close(
    body: CloseDrawerRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        z_report = await PosService(db).close_drawer(
            user_id=user.id,
            closing_amount=body.closing_amount,
            breakdown=[d.model_dump() for d in body.breakdown] if body.breakdown else None,
            note=body.note,
        )
    except PosServiceError as exc:
        _raise(exc)
    await db.commit()
    return _serialize_z_report(z_report, await _by_cashier(db, z_report))


@router.post("/cash-movements")
async def create_cash_movement(
    body: CashMovementIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        movement = await PosService(db).add_cash_movement(
            user_id=user.id,
            direction=CashMovementDirection(body.direction),
            amount=body.amount,
            reason=CashMovementReason(body.reason),
            note=body.note,
        )
    except PosServiceError as exc:
        _raise(exc)
    await db.commit()
    return _serialize_movement(movement)


@router.get("/cash-movements")
async def list_cash_movements(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    drawer_id: uuid.UUID = Query(...),
):
    movements = await PosService(db).list_cash_movements(drawer_id)
    return {"movements": [_serialize_movement(m) for m in movements]}


# ---------------------------------------------------------------------------
# Ventes (§4.1)
# ---------------------------------------------------------------------------


@router.post("/transactions", status_code=201)
async def create_transaction(
    body: CreateTransactionRequest,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        transaction, created = await PosService(db).create_transaction(
            user_id=user.id,
            client_uuid=body.client_uuid,
            items=body.items,
            discount=body.discount,
            payments=body.payments,
            client_id=body.client_id,
        )
    except PosServiceError as exc:
        _raise(exc)
    await db.commit()
    receipt_text = await PosService(db).get_rendered_receipt_text(
        transaction.id, transaction=transaction
    )
    if not created:
        response.status_code = 200
    refund_of_sale, original_number_by_refund_id = await _load_refund_links(db, [transaction.id])
    clients_by_id = await _load_clients(db, [transaction.client_id])
    invoice_numbers = await _load_invoice_numbers(db, [transaction.id])
    return _serialize_transaction(
        transaction,
        receipt_text=receipt_text,
        refund_of_sale=refund_of_sale,
        original_number_by_refund_id=original_number_by_refund_id,
        clients_by_id=clients_by_id,
        invoice_numbers=invoice_numbers,
    )


@router.get("/transactions")
async def list_transactions(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    try:
        transactions = await PosService(db).list_transactions(date=date, limit=limit)
    except ValueError as exc:
        raise PosServiceError(f"Date invalide : {exc}", code="invalid_date", status_code=422)
    # Une seule requete agregee pour toute la page (pas de N+1) — voir
    # `_load_refund_links`/`_load_clients`.
    refund_of_sale, original_number_by_refund_id = await _load_refund_links(
        db, [t.id for t in transactions]
    )
    clients_by_id = await _load_clients(db, [t.client_id for t in transactions])
    invoice_numbers = await _load_invoice_numbers(db, [t.id for t in transactions])
    return {
        "transactions": [
            _serialize_transaction(
                t,
                refund_of_sale=refund_of_sale,
                original_number_by_refund_id=original_number_by_refund_id,
                clients_by_id=clients_by_id,
                invoice_numbers=invoice_numbers,
            )
            for t in transactions
        ]
    }


@router.get("/transactions/{transaction_id}")
async def get_transaction(
    transaction_id: uuid.UUID,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    transaction = await PosService(db).get_transaction(transaction_id)
    if transaction is None:
        raise PosServiceError("Transaction introuvable.", code="not_found", status_code=404)
    receipt_text = await PosService(db).get_rendered_receipt_text(
        transaction_id, transaction=transaction
    )
    refund_of_sale, original_number_by_refund_id = await _load_refund_links(db, [transaction_id])
    clients_by_id = await _load_clients(db, [transaction.client_id])
    invoice_numbers = await _load_invoice_numbers(db, [transaction_id])
    return _serialize_transaction(
        transaction,
        receipt_text=receipt_text,
        refund_of_sale=refund_of_sale,
        original_number_by_refund_id=original_number_by_refund_id,
        clients_by_id=clients_by_id,
        invoice_numbers=invoice_numbers,
    )


@router.post("/transactions/{transaction_id}/cancel", status_code=201)
async def cancel_transaction(
    transaction_id: uuid.UUID,
    body: CancelTransactionRequest,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        refund_tx, created = await RefundService(db).cancel_transaction(
            original_tx_id=transaction_id, user_id=user.id, reason=body.reason
        )
    except PosServiceError as exc:
        _raise(exc)
    await db.commit()
    receipt_text = await PosService(db).get_rendered_receipt_text(
        refund_tx.id, transaction=refund_tx
    )
    if not created:
        response.status_code = 200
    refund_of_sale, original_number_by_refund_id = await _load_refund_links(db, [refund_tx.id])
    clients_by_id = await _load_clients(db, [refund_tx.client_id])
    invoice_numbers = await _load_invoice_numbers(db, [refund_tx.id])
    return _serialize_transaction(
        refund_tx,
        receipt_text=receipt_text,
        refund_of_sale=refund_of_sale,
        original_number_by_refund_id=original_number_by_refund_id,
        clients_by_id=clients_by_id,
        invoice_numbers=invoice_numbers,
    )


@router.get("/transactions/{transaction_id}/receipt")
async def get_receipt(
    transaction_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Texte du ticket + compteur de duplicata.

    Décision d'intégration PR2 (tranchée par l'orchestrateur) : CHAQUE appel
    à cet endpoint est compté comme un duplicata, y compris le tout premier.
    Le texte du ticket est déjà livré au front dans la réponse 201 de
    `POST /pos/transactions` (`receipt_text`) — toute lecture via ce GET est
    donc par construction un renvoi/réimpression (§4.4), jamais "la"
    lecture originale. `duplicate_count` est incrémenté et `receipt.duplicate`
    journalisé au JET à chaque appel, sans exception pour le premier.
    """
    receipt = (
        await db.execute(select(Receipt).where(Receipt.transaction_id == transaction_id))
    ).scalar_one_or_none()
    if receipt is None:
        raise PosServiceError("Ticket introuvable.", code="not_found", status_code=404)
    receipt.duplicate_count += 1
    await JournalService(db).record(
        EVENT_RECEIPT_DUPLICATE,
        user_id=user.id,
        payload={"transaction_id": str(transaction_id), "duplicate_count": receipt.duplicate_count},
    )
    await db.commit()
    # Rendu courant (PR7/I3) : le contenu stocke est immuable (trigger
    # `trg_protect_receipt`), c'est donc la LECTURE qui remet la ligne
    # « Client : … » en accord avec le rattachement d'aujourd'hui — voir
    # `services/receipt.py::apply_client_line`.
    text = await PosService(db).render_receipt_text(receipt)
    return {"text": text, "duplicate_count": receipt.duplicate_count}


# ---------------------------------------------------------------------------
# Impression physique des tickets (PR3b) — décision Julien, contraire au CDC
# initial : la caisse imprime avec le MÊME matériel que l'application
# source : imprimante MUNBYN 047P ESC/POS 80 mm en réseau (TCP 9100) ou en
# USB-OTG via WebUSB depuis la tablette Android, tiroir-caisse Safescan
# SD-4141 branché sur l'imprimante (impulsion `ESC p m`).
#
# Distinct de `GET /transactions/{id}/receipt` ci-dessus (texte du ticket,
# `duplicate_count`, PR2) : ces endpoints impriment/exportent les OCTETS
# ESC/POS et comptent les impressions PHYSIQUES via
# `receipts.printed_count`/`printed_at` (migration 0004).
# ---------------------------------------------------------------------------


class PrintReceiptRequest(BaseModel):
    # Ouvre le tiroir dans le même job d'impression (une seule connexion
    # TCP) — seulement honoré si `hardware.drawer_enabled` est vrai.
    kick: bool = False


class DrawerKickRequest(BaseModel):
    reason: Literal["cash_sale", "manual"] = "manual"


async def _get_receipt_or_404(db: AsyncSession, transaction_id: uuid.UUID) -> Receipt:
    receipt = (
        await db.execute(select(Receipt).where(Receipt.transaction_id == transaction_id))
    ).scalar_one_or_none()
    if receipt is None:
        raise PosServiceError("Ticket introuvable.", code="not_found", status_code=404)
    return receipt


def _drawer_kick_bytes(hardware: dict) -> bytes:
    return escpos_service.build_drawer_kick(
        pin=int(hardware.get("drawer_pin") or 0),
    )


async def _raise_printer_unreachable(
    db: AsyncSession,
    user: User,
    exc: escpos_service.PrinterUnreachable,
    *,
    extra_payload: dict | None = None,
) -> None:
    """Journalise l'échec TCP (host/port seulement, jamais l'errno système)
    et lève l'erreur métier générique attendue côté caisse (persona
    vendeuse : pas d'IP, de port ni d'errno à l'écran). Le détail technique
    complet (``str(exc)``) ne va qu'au log serveur."""
    logger.warning("Imprimante injoignable (%s:%s) : %s", exc.host, exc.port, exc)
    await JournalService(db).record(
        EVENT_PRINTER_UNREACHABLE,
        user_id=user.id,
        payload={"host": exc.host, "port": exc.port, **(extra_payload or {})},
    )
    await db.commit()
    raise PosServiceError(
        escpos_service.PRINTER_UNREACHABLE_MESSAGE,
        code="printer_unreachable",
        status_code=502,
    )


async def _mark_printed(db: AsyncSession, receipt: Receipt) -> bool:
    """Incrémente `printed_count`/`printed_at` et retourne ``True`` si cette
    impression est un duplicata (`printed_count` était déjà > 0)."""
    is_duplicate = receipt.printed_count > 0
    receipt.printed_count += 1
    receipt.printed_at = datetime.now(timezone.utc)
    return is_duplicate


@router.post("/transactions/{transaction_id}/print")
async def print_receipt(
    transaction_id: uuid.UUID,
    body: PrintReceiptRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Imprime le ticket sur la MUNBYN réseau (TCP 9100).

    409 `printer_webusb` si le matériel est configuré en mode tablette (le
    front doit alors appeler `GET .../escpos`) ; 409 `printer_disabled` si
    aucune imprimante n'est activée ; 502 `printer_unreachable` si le TCP
    échoue. Le tiroir est kické dans le MÊME job d'impression quand
    `kick=true` et que `hardware.drawer_enabled` est vrai.
    """
    receipt = await _get_receipt_or_404(db, transaction_id)
    hardware = await SettingsService(db).get("hardware")
    mode = hardware.get("printer_mode", "none")

    if mode == "webusb":
        raise PosServiceError(
            "Imprimante configurée en mode tablette (WebUSB) : utilisez "
            "l'impression depuis la caisse plutôt que ce point d'entrée réseau.",
            code="printer_webusb",
            status_code=409,
        )
    if mode != "network":
        raise PosServiceError(
            "Imprimante ticket désactivée : configurez-la dans Paramètres > Matériel.",
            code="printer_disabled",
            status_code=409,
        )

    host = hardware.get("printer_host") or ""
    port = int(hardware.get("printer_port") or escpos_service.DEFAULT_PORT)
    shop = await SettingsService(db).get("shop")
    kick_bytes = _drawer_kick_bytes(hardware) if body.kick and hardware.get("drawer_enabled") else None
    payload = escpos_service.build_receipt(
        # Rendu courant (PR7/I3) — cf. `GET .../receipt` ci-dessus.
        await PosService(db).render_receipt_text(receipt),
        shop_name=shop.get("name") or "",
        kick=kick_bytes,
    )
    try:
        await escpos_service.send_to_printer(host, port, payload)
    except escpos_service.PrinterUnreachable as exc:
        await _raise_printer_unreachable(
            db, user, exc, extra_payload={"transaction_id": str(transaction_id), "mode": "network"}
        )

    is_duplicate = await _mark_printed(db, receipt)
    transaction_number = receipt.transaction.transaction_number if receipt.transaction else None
    await JournalService(db).record(
        EVENT_RECEIPT_PRINTED,
        user_id=user.id,
        payload={
            "transaction_id": str(transaction_id),
            "number": transaction_number,
            "mode": "network",
            "duplicate": is_duplicate,
        },
    )
    if kick_bytes is not None:
        # L'impulsion tiroir a effectivement ete incluse dans ce job
        # d'impression (kick=true + hardware.drawer_enabled) : journalisee
        # dans la MEME transaction SQL que `receipt.printed`, comme
        # `POST /pos/drawer/kick` le fait pour une impulsion seule.
        await JournalService(db).record(
            EVENT_DRAWER_KICKED,
            user_id=user.id,
            payload={
                "reason": "cash_sale",
                "with_print": True,
                "transaction_number": transaction_number,
            },
        )
    await db.commit()
    return {"printed": True, "printed_count": receipt.printed_count, "duplicate": is_duplicate}


@router.get("/transactions/{transaction_id}/escpos")
async def get_transaction_escpos(
    transaction_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    kick: bool = Query(default=False),
):
    """Octets ESC/POS bruts du ticket, pour l'impression WebUSB (tablette).

    Ne dépend pas du mode matériel configuré : c'est la tablette qui décide
    quand les envoyer à l'imprimante via USB-OTG. Compte comme une
    impression physique au même titre que `POST .../print` ci-dessus.
    """
    receipt = await _get_receipt_or_404(db, transaction_id)
    hardware = await SettingsService(db).get("hardware")
    shop = await SettingsService(db).get("shop")
    kick_bytes = _drawer_kick_bytes(hardware) if kick and hardware.get("drawer_enabled") else None
    payload = escpos_service.build_receipt(
        # Rendu courant (PR7/I3) — cf. `GET .../receipt` ci-dessus.
        await PosService(db).render_receipt_text(receipt),
        shop_name=shop.get("name") or "",
        kick=kick_bytes,
    )

    is_duplicate = await _mark_printed(db, receipt)
    transaction_number = receipt.transaction.transaction_number if receipt.transaction else None
    await JournalService(db).record(
        EVENT_RECEIPT_PRINTED,
        user_id=user.id,
        payload={
            "transaction_id": str(transaction_id),
            "number": transaction_number,
            "mode": "webusb",
            "duplicate": is_duplicate,
        },
    )
    if kick_bytes is not None:
        # Meme regle que POST .../print ci-dessus : l'impulsion tiroir a
        # ete incluse dans les octets renvoyes (kick=1 + drawer_enabled) —
        # journalisee dans la meme transaction SQL que `receipt.printed`.
        await JournalService(db).record(
            EVENT_DRAWER_KICKED,
            user_id=user.id,
            payload={
                "reason": "cash_sale",
                "with_print": True,
                "transaction_number": transaction_number,
            },
        )
    await db.commit()
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/drawer/kick")
async def kick_drawer_network(
    body: DrawerKickRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Impulsion seule du tiroir-caisse via l'imprimante réseau (sans
    imprimer de ticket) — encaissement espèces ou ouverture manuelle."""
    hardware = await SettingsService(db).get("hardware")
    if hardware.get("printer_mode") != "network" or not hardware.get("drawer_enabled"):
        raise PosServiceError(
            "Tiroir-caisse indisponible : imprimante réseau et tiroir activé requis "
            "(Paramètres > Matériel).",
            code="drawer_unavailable",
            status_code=409,
        )
    host = hardware.get("printer_host") or ""
    port = int(hardware.get("printer_port") or escpos_service.DEFAULT_PORT)
    try:
        await escpos_service.send_to_printer(host, port, _drawer_kick_bytes(hardware))
    except escpos_service.PrinterUnreachable as exc:
        await _raise_printer_unreachable(db, user, exc, extra_payload={"reason": body.reason})
    await JournalService(db).record(
        EVENT_DRAWER_KICKED, user_id=user.id, payload={"reason": body.reason}
    )
    await db.commit()
    return {"kicked": True}


@router.get("/drawer/kick-escpos")
async def kick_drawer_escpos(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Octets d'impulsion seuls (`ESC p m`), pour l'ouverture du tiroir
    depuis la tablette en mode WebUSB."""
    hardware = await SettingsService(db).get("hardware")
    if not hardware.get("drawer_enabled"):
        raise PosServiceError(
            "Tiroir-caisse désactivé (Paramètres > Matériel).",
            code="drawer_unavailable",
            status_code=409,
        )
    payload = _drawer_kick_bytes(hardware)
    await JournalService(db).record(
        EVENT_DRAWER_KICKED, user_id=user.id, payload={"reason": "manual"}
    )
    await db.commit()
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Client en caisse (PR7, I3) — recherche, creation, detachement
# ---------------------------------------------------------------------------


def _serialize_pos_client(client, stats: dict[str, dict] | None = None) -> dict:
    """Fiche cliente telle qu'elle apparait EN CAISSE.

    Coordonnees MASQUEES (`mask_email`/`mask_phone`) : la vendeuse a besoin
    de reconnaitre la bonne cliente, pas de lire son adresse ni son numero
    en entier devant la file d'attente. La fiche complete reste en
    back-office (`/admin/clients/{id}`).
    """
    stats = (stats or {}).get(str(client.id), {})
    return {
        "id": str(client.id),
        "first_name": client.first_name,
        "last_name": client.last_name,
        "email_masked": mask_email(client.email),
        "phone_masked": mask_phone(client.phone),
        "newsletter_optin": client.newsletter_optin,
        "last_visit_at": stats.get("last_visit_at"),
        "visits_count": stats.get("visits_count", 0),
    }


@router.get("/clients/search")
async def search_pos_clients(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str = Query(min_length=2),
):
    """Recherche par nom, prenom, e-mail ou telephone (I3).

    Les fiches anonymisees (RGPD) sont exclues : elles ne portent plus
    aucune donnee personnelle et n'ont donc plus rien a identifier.
    """
    service = ClientService(db)
    clients = await service.search(q, limit=20, include_anonymized=False)
    stats = await service.visit_stats([c.id for c in clients])
    return {"clients": [_serialize_pos_client(c, stats) for c in clients]}


@router.get("/clients/duplicates")
async def find_pos_client_duplicates(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    first_name: str | None = Query(default=None),
    last_name: str | None = Query(default=None),
    email: str | None = Query(default=None),
    phone: str | None = Query(default=None),
):
    """« Une fiche existe peut-etre deja » (PR10/L2), appele PENDANT la
    saisie d'un nouveau client en caisse.

    Au moins un critere non vide, sinon 422 `criteria_required` : sans quoi
    la caisse demanderait a la base de comparer une fiche vide a toutes les
    autres. Une saisie encore incomplete (e-mail sans `@`, numero a trois
    chiffres) n'est PAS une erreur ici — le critere est simplement ignore,
    la vendeuse tape encore.
    """
    criteria = (first_name, last_name, email, phone)
    if not any((value or "").strip() for value in criteria):
        raise PosServiceError(
            "Renseignez au moins un critère (nom, prénom, e-mail ou téléphone).",
            code="criteria_required",
            status_code=422,
        )
    service = ClientService(db)
    candidates = await service.find_duplicate_candidates(
        email=email,
        phone=phone,
        first_name=first_name,
        last_name=last_name,
    )
    stats = await service.visit_stats([item["client"].id for item in candidates])
    return {
        "candidates": [
            {
                "id": str(item["client"].id),
                "first_name": item["client"].first_name,
                "last_name": item["client"].last_name,
                "email_masked": mask_email(item["client"].email),
                "phone_masked": mask_phone(item["client"].phone),
                "visits_count": stats.get(str(item["client"].id), {}).get("visits_count", 0),
                "last_visit_at": stats.get(str(item["client"].id), {}).get("last_visit_at"),
                "reason": item["reason"],
            }
            for item in candidates
        ]
    }


@router.post("/clients", status_code=201)
async def create_pos_client(
    body: CreatePosClientRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Cree (ou retrouve) une fiche depuis la caisse.

    Toujours 201, meme quand la fiche existait deja : `created` dit lequel
    des deux cas s'est produit, et la caisse enchaine pareil dans les deux
    cas. Le consentement newsletter n'est enregistre QUE s'il est coche
    (case jamais pre-cochee cote front, E2/E8 de PR3) et porte la source
    `pos`.
    """
    service = ClientService(db)
    try:
        client, created = await service.create_or_get(
            email=body.email,
            phone=body.phone,
            first_name=body.first_name,
            last_name=body.last_name,
            user_id=user.id,
        )
        if body.newsletter_optin:
            await service.record_consent(
                client=client,
                purpose=ConsentPurpose.newsletter,
                granted=True,
                source=ConsentSource.pos,
                user_id=user.id,
            )
            # Sans e-mail, `sync_brevo` est un no-op journalise : Brevo est
            # un carnet d'adresses e-mail (I3).
            await service.sync_brevo(client, user_id=user.id)
    except PosServiceError as exc:
        _raise(exc)
    await db.commit()
    stats = await service.visit_stats([client.id])
    return {"client": _serialize_pos_client(client, stats), "created": created}


@router.delete("/transactions/{transaction_id}/client")
async def detach_client(
    transaction_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Detache la cliente d'une vente (I3).

    `client_id` est la seule colonne mutable hors hash d'une transaction
    signee (E3) : ni les montants, ni les paiements, ni la signature ne
    bougent. Idempotent — detacher une vente qui n'a pas de cliente ne
    journalise rien et renvoie la vente telle quelle.
    """
    transaction = await PosService(db).get_transaction(transaction_id)
    if transaction is None:
        raise PosServiceError("Vente introuvable.", code="not_found", status_code=404)
    try:
        await ClientService(db).unlink_transaction(transaction=transaction, user_id=user.id)
    except PosServiceError as exc:
        _raise(exc)
    await db.commit()
    transaction = await PosService(db).get_transaction(transaction_id)
    receipt_text = await PosService(db).get_rendered_receipt_text(
        transaction_id, transaction=transaction
    )
    refund_of_sale, original_number_by_refund_id = await _load_refund_links(db, [transaction_id])
    invoice_numbers = await _load_invoice_numbers(db, [transaction_id])
    return _serialize_transaction(
        transaction,
        receipt_text=receipt_text,
        refund_of_sale=refund_of_sale,
        original_number_by_refund_id=original_number_by_refund_id,
        clients_by_id={},
        invoice_numbers=invoice_numbers,
    )


# ---------------------------------------------------------------------------
# Client + ticket par e-mail (PR3, §3/§4 ARCHITECTURE_PR3.md)
# ---------------------------------------------------------------------------


@router.post("/transactions/{transaction_id}/client")
async def attach_client(
    transaction_id: uuid.UUID,
    body: AttachClientRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    transaction = await PosService(db).get_transaction(transaction_id)
    if transaction is None:
        raise PosServiceError("Vente introuvable.", code="not_found", status_code=404)
    try:
        result = await PosService(db).attach_client_and_send_receipt(
            transaction=transaction,
            email=body.email,
            first_name=body.first_name,
            last_name=body.last_name,
            newsletter_optin=body.newsletter_optin,
            send_receipt=body.send_receipt,
            user_id=user.id,
        )
    except PosServiceError as exc:
        _raise(exc)
    await db.commit()
    client = result["client"]
    return {
        "client": {
            "id": str(client.id),
            "email": client.email,
            "first_name": client.first_name,
            "last_name": client.last_name,
            "newsletter_optin": client.newsletter_optin,
        },
        "receipt_email": result["receipt_email"],
        "brevo": result["brevo"],
    }


@router.post("/transactions/{transaction_id}/receipt/email")
async def resend_receipt_email(
    transaction_id: uuid.UUID,
    body: ResendReceiptEmailRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    transaction = await PosService(db).get_transaction(transaction_id)
    if transaction is None:
        raise PosServiceError("Vente introuvable.", code="not_found", status_code=404)
    try:
        result = await PosService(db).resend_receipt_email(
            transaction=transaction, email=body.email, user_id=user.id
        )
    except PosServiceError as exc:
        _raise(exc)
    await db.commit()
    return result


# ---------------------------------------------------------------------------
# Rapports Z (§4.3)
# ---------------------------------------------------------------------------


@router.get("/z-reports")
async def list_z_reports(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=30, ge=1, le=200),
):
    reports = (
        await db.execute(select(ZReport).order_by(ZReport.report_number.desc()).limit(limit))
    ).scalars().all()
    grouped = await sales_by_cashier(db, list(reports))
    return {
        "z_reports": [_serialize_z_report(z, grouped.get(str(z.id), [])) for z in reports]
    }


@router.get("/z-reports/regularization/preview")
async def regularization_preview(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period_from: str = Query(...),
    period_to: str = Query(...),
):
    try:
        preview = await FiscalService(db).preview_regularization(
            datetime.fromisoformat(period_from), datetime.fromisoformat(period_to)
        )
    except ValueError as exc:
        raise PosServiceError(f"Dates invalides : {exc}", code="invalid_period", status_code=422)
    return preview


@router.post("/z-reports/regularization")
async def create_regularization(
    body: RegularizationRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        period_from = datetime.fromisoformat(body.period_from)
        period_to = datetime.fromisoformat(body.period_to)
    except ValueError as exc:
        raise PosServiceError(f"Dates invalides : {exc}", code="invalid_period", status_code=422)
    try:
        z_report = await FiscalService(db).create_regularization_z(
            period_from, period_to, body.reason, user.id
        )
        await JournalService(db).record(
            EVENT_Z_REGULARIZATION,
            user_id=user.id,
            payload={"z_number": z_report.report_number, "reason": body.reason},
        )
    except PosServiceError as exc:
        _raise(exc)
    await db.commit()
    return _serialize_z_report(z_report, await _by_cashier(db, z_report))


@router.get("/z-reports/{z_report_id}/pdf")
async def get_z_report_pdf(
    z_report_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PDF deterministe du rapport Z (PR4, F8, docs/ARCHITECTURE_PR4.md).

    Deux appels sur le meme Z renvoient des octets identiques (voir
    `services/z_report_pdf.py`) — le SHA-256 journalise (`export.downloaded`,
    F7) est donc stable, verifiable hors application."""
    z = (await db.execute(select(ZReport).where(ZReport.id == z_report_id))).scalar_one_or_none()
    if z is None:
        raise PosServiceError("Z introuvable.", code="not_found", status_code=404)

    from app.services.z_report_pdf import generate_z_report_pdf

    pdf_bytes = await generate_z_report_pdf(db, z)

    import hashlib

    sha = hashlib.sha256(pdf_bytes).hexdigest()
    await JournalService(db).record(
        EVENT_EXPORT_DOWNLOADED,
        user_id=user.id,
        payload={
            "kind": "z_report_pdf",
            "period_start": None,
            "period_end": None,
            "sha256": sha,
            "rows": 1,
            "z_number": z.report_number,
        },
    )
    await db.commit()
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="Z{z.report_number:04d}.pdf"',
            "X-PDF-SHA256": sha,
        },
    )


@router.get("/z-reports/{z_report_id}")
async def get_z_report(
    z_report_id: uuid.UUID,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    z = (await db.execute(select(ZReport).where(ZReport.id == z_report_id))).scalar_one_or_none()
    if z is None:
        raise PosServiceError("Z introuvable.", code="not_found", status_code=404)
    return _serialize_z_report(z, await _by_cashier(db, z))
