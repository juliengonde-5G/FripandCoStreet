# Nouveau routeur (PR2, §4.1/§4.3/§5 ARCHITECTURE_PR2.md) — vente, caisse
# especes, tickets, Z. Le bloc CB (`/pos/payments/cb/*`) vit dans
# `cb_router.py` (agent B) : voir le montage protege dans `app/main.py`.
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.cash_movement import CashMovementDirection, CashMovementReason
from app.models.pos import CashDrawer, Transaction, ZReport
from app.models.receipt import Receipt
from app.models.user import User
from app.services.fiscal import FiscalService, PosServiceError
from app.services.jet import EVENT_RECEIPT_DUPLICATE, EVENT_Z_REGULARIZATION, JournalService
from app.services.pos import PosService
from app.services.refund import RefundService

from .schemas import (
    CancelTransactionRequest,
    CashMovementIn,
    CloseDrawerRequest,
    CreateTransactionRequest,
    OpenDrawerRequest,
    RegularizationRequest,
)

router = APIRouter(prefix="/pos", tags=["pos"])


def _raise(exc: PosServiceError):
    raise HTTPException(status_code=exc.status_code, detail={"detail": str(exc), "code": exc.code})


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


def _serialize_transaction(transaction: Transaction, *, receipt_text: str | None = None) -> dict:
    return {
        "id": str(transaction.id),
        "transaction_number": transaction.transaction_number,
        "transaction_type": transaction.transaction_type.value,
        "user_id": str(transaction.user_id),
        "client_uuid": str(transaction.client_uuid) if transaction.client_uuid else None,
        "original_transaction_id": (
            str(transaction.original_transaction_id) if transaction.original_transaction_id else None
        ),
        "refund_reason": transaction.refund_reason,
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


def _serialize_z_report(z: ZReport) -> dict:
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
    }


# ---------------------------------------------------------------------------
# Caisse espèces (§4.3)
# ---------------------------------------------------------------------------


@router.get("/drawer/current")
async def drawer_current(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    pos = PosService(db)
    drawer = await pos.get_open_drawer()
    if drawer is None:
        return {"open": False}
    today = await pos.drawer_snapshot(drawer)
    return {"open": True, "drawer": _serialize_drawer(drawer), "today": today}


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
    return _serialize_z_report(z_report)


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
        )
    except PosServiceError as exc:
        _raise(exc)
    await db.commit()
    receipt = await PosService(db).get_receipt(transaction.id)
    if not created:
        response.status_code = 200
    return _serialize_transaction(transaction, receipt_text=receipt.content if receipt else None)


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
        raise HTTPException(
            status_code=422, detail={"detail": f"Date invalide : {exc}", "code": "invalid_date"}
        )
    return {"transactions": [_serialize_transaction(t) for t in transactions]}


@router.get("/transactions/{transaction_id}")
async def get_transaction(
    transaction_id: uuid.UUID,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    transaction = await PosService(db).get_transaction(transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail={"detail": "Transaction introuvable.", "code": "not_found"})
    receipt = await PosService(db).get_receipt(transaction_id)
    return _serialize_transaction(transaction, receipt_text=receipt.content if receipt else None)


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
    receipt = await PosService(db).get_receipt(refund_tx.id)
    if not created:
        response.status_code = 200
    return _serialize_transaction(refund_tx, receipt_text=receipt.content if receipt else None)


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
        raise HTTPException(status_code=404, detail={"detail": "Ticket introuvable.", "code": "not_found"})
    receipt.duplicate_count += 1
    await JournalService(db).record(
        EVENT_RECEIPT_DUPLICATE,
        user_id=user.id,
        payload={"transaction_id": str(transaction_id), "duplicate_count": receipt.duplicate_count},
    )
    await db.commit()
    return {"text": receipt.content, "duplicate_count": receipt.duplicate_count}


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
    return {"z_reports": [_serialize_z_report(z) for z in reports]}


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
        raise HTTPException(
            status_code=422, detail={"detail": f"Dates invalides : {exc}", "code": "invalid_period"}
        )
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
        raise HTTPException(
            status_code=422, detail={"detail": f"Dates invalides : {exc}", "code": "invalid_period"}
        )
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
    return _serialize_z_report(z_report)


@router.get("/z-reports/{z_report_id}")
async def get_z_report(
    z_report_id: uuid.UUID,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    z = (await db.execute(select(ZReport).where(ZReport.id == z_report_id))).scalar_one_or_none()
    if z is None:
        raise HTTPException(status_code=404, detail={"detail": "Z introuvable.", "code": "not_found"})
    return _serialize_z_report(z)
