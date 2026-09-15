# Extrait de l'application source (apps/api/app/services/refund.py), reduit a l'annulation
# TOTALE exposee par l'UI (D4 du contrat PR2 — le service accepte aussi un
# sous-ensemble de lignes, capacite non exposee par le router PR2).
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pos import (
    Payment,
    PaymentMethod,
    Transaction,
    TransactionItem,
    TransactionType,
)
from app.services.fiscal import FiscalService, PosServiceError, acquire_fiscal_write_lock
from app.services.jet import EVENT_SALE_CANCELLED, JournalService
from app.services.receipt import ReceiptService
from app.services.settings_service import SettingsService


class RefundError(PosServiceError):
    status_code = 422
    code = "refund_error"


class AlreadyRefunded(RefundError):
    status_code = 409
    code = "already_refunded"

    def __init__(self):
        super().__init__("Cette vente a déjà été annulée.")


class NotASale(RefundError):
    status_code = 409
    code = "not_a_sale"

    def __init__(self):
        super().__init__("Seule une vente peut être annulée.")


class SumUpRefundFailed(RefundError):
    status_code = 502
    code = "sumup_refund_failed"


# ---------------------------------------------------------------------------
# Interface avec l'agent B, sur le meme modele que
# `app.services.pos.verify_card_tender` (contrat §7, etendu ici pour le flux
# d'annulation D6 — non explicitement nomme au contrat mais structurellement
# requis : RefundService doit rembourser via SumUp AVANT toute ecriture
# locale). Attribut module-level reassigne paresseusement ; les tests
# injectent un faux via
# `monkeypatch.setattr("app.services.refund.refund_card_payment", fake)`
# AVANT tout appel — une fois pose, il n'est plus reimporte.
refund_card_payment = None


def _resolve_refund_card_payment():
    global refund_card_payment
    if refund_card_payment is None:

        async def _default(sumup_transaction_id: str, amount: Decimal) -> dict:
            from app.services.sumup_service import SumUpService

            return await SumUpService().refund_transaction(sumup_transaction_id, amount=amount)

        refund_card_payment = _default
    return refund_card_payment


class RefundService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def cancel_transaction(
        self,
        *,
        original_tx_id: uuid.UUID,
        user_id: uuid.UUID,
        reason: str,
        client_uuid: uuid.UUID | None = None,
    ) -> tuple[Transaction, bool]:
        """Annulation totale d'une vente (D4) — transaction inverse referencee.

        Retourne ``(refund_transaction, created)`` ; ``created=False`` sur un
        replay idempotent (``client_uuid`` deja utilise pour un refund).
        """
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise RefundError("Le motif d'annulation doit contenir au moins 3 caractères.")

        if client_uuid is not None:
            existing = await self._find_refund_by_client_uuid(client_uuid)
            if existing is not None:
                return existing, False

        await acquire_fiscal_write_lock(self.db)
        if client_uuid is not None:
            existing = await self._find_refund_by_client_uuid(client_uuid)
            if existing is not None:
                return existing, False

        from app.services.pos import DrawerClosed, PosService

        drawer = await PosService(self.db).get_open_drawer()
        if drawer is None:
            raise DrawerClosed()

        original = (
            await self.db.execute(select(Transaction).where(Transaction.id == original_tx_id))
        ).scalar_one_or_none()
        if original is None:
            raise RefundError("Transaction introuvable.", code="not_found", status_code=404)
        if original.transaction_type != TransactionType.sale:
            raise NotASale()

        already = (
            await self.db.execute(
                select(Transaction.id).where(
                    Transaction.original_transaction_id == original.id,
                    Transaction.transaction_type == TransactionType.refund,
                )
            )
        ).scalar_one_or_none()
        if already is not None:
            raise AlreadyRefunded()

        # D6 : remboursement SumUp AVANT toute ecriture locale. Le
        # vrai `SumUpService.refund_transaction` ne leve pas d'exception sur
        # un refus (reseau/HTTP) — il retourne ``{"ok": False, ...}`` — donc
        # on traite aussi bien une exception (faux de test) qu'un ok=False
        # comme un echec bloquant.
        for payment in original.payments or []:
            if payment.method == PaymentMethod.card and payment.sumup_transaction_id:
                refunder = _resolve_refund_card_payment()
                try:
                    result = await refunder(payment.sumup_transaction_id, Decimal(str(payment.amount)))
                except Exception as exc:  # noqa: BLE001 — cf. commentaire d'interface ci-dessus
                    raise SumUpRefundFailed(f"Le remboursement SumUp a échoué : {exc}") from exc
                if isinstance(result, dict) and not result.get("ok", True):
                    raise SumUpRefundFailed(
                        "Le remboursement SumUp a échoué : "
                        f"{result.get('message') or result.get('status') or 'erreur inconnue'}"
                    )

        next_number = (
            await self.db.execute(select(func.coalesce(func.max(Transaction.transaction_number), 0)))
        ).scalar_one() + 1

        refund_tx = Transaction(
            transaction_number=next_number,
            transaction_type=TransactionType.refund,
            user_id=user_id,
            client_uuid=client_uuid,
            original_transaction_id=original.id,
            refund_reason=reason,
            discount_type=original.discount_type,
            discount_value=original.discount_value,
            discount_amount=original.discount_amount,
            tva_rate=original.tva_rate,
            total_ht=original.total_ht,
            total_tva=original.total_tva,
            total_ttc=original.total_ttc,
            hash_chain="",
            previous_hash="",
            receipt_number=next_number,
        )
        self.db.add(refund_tx)
        await self.db.flush()

        for item in sorted(original.items or [], key=lambda i: i.position):
            self.db.add(
                TransactionItem(
                    transaction_id=refund_tx.id,
                    label=item.label,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    discount_amount=item.discount_amount,
                    line_total=item.line_total,
                    tva_rate=item.tva_rate,
                    line_ht=item.line_ht,
                    line_tva=item.line_tva,
                    original_transaction_item_id=item.id,
                    position=item.position,
                )
            )

        for payment in original.payments or []:
            self.db.add(
                Payment(
                    transaction_id=refund_tx.id,
                    method=payment.method,
                    amount=payment.amount,
                    sumup_checkout_id=payment.sumup_checkout_id,
                    sumup_transaction_id=payment.sumup_transaction_id,
                    sumup_transaction_code=payment.sumup_transaction_code,
                    sumup_auth_code=payment.sumup_auth_code,
                    sumup_card_brand=payment.sumup_card_brand,
                    sumup_card_last4=payment.sumup_card_last4,
                    sumup_refunded_amount=(payment.amount if payment.method == PaymentMethod.card else None),
                )
            )

        await FiscalService(self.db).sign_transaction(refund_tx)

        from app.models.receipt import Receipt

        receipt_text = ReceiptService().generate_refund_text(
            refund_tx,
            shop=await SettingsService(self.db).get("shop"),
            original_number=original.transaction_number,
        )
        self.db.add(Receipt(transaction_id=refund_tx.id, content=receipt_text))

        await JournalService(self.db).record(
            EVENT_SALE_CANCELLED,
            user_id=user_id,
            payload={
                "number": refund_tx.transaction_number,
                "original_number": original.transaction_number,
                "reason": reason,
            },
        )

        await self.db.flush()
        # Cf. commentaire equivalent dans PosService.create_transaction :
        # recharge via `select()` pour que `items`/`payments` (lazy=selectin)
        # soient effectivement charges avant un premier acces synchrone.
        refund_tx = (
            await self.db.execute(select(Transaction).where(Transaction.id == refund_tx.id))
        ).scalar_one()
        return refund_tx, True

    async def _find_refund_by_client_uuid(self, client_uuid: uuid.UUID) -> Transaction | None:
        return (
            await self.db.execute(
                select(Transaction).where(
                    Transaction.client_uuid == client_uuid,
                    Transaction.transaction_type == TransactionType.refund,
                )
            )
        ).scalar_one_or_none()
