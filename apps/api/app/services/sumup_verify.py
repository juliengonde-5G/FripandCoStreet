# Nouveau service (PR2, §4.5 + §7 ARCHITECTURE_PR2.md) — vérification serveur
# d'un paiement CB avant qu'une vente ne soit écrite (D5 : « aucune donnée CB
# envoyée par le navigateur n'est crue »).
#
# Interface opposable pour l'agent A (`app/services/pos.py::create_transaction`,
# import local paresseux) : ``CardTenderInput``, ``VerifiedCardTender``,
# ``CardNotConfirmed``, ``verify_card_tender(db, tender, client_uuid)``.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment_attempt import PaymentAttempt, PaymentAttemptStatus
from app.models.pos import Transaction, TransactionType
from app.services import failed_payment_service
from app.services.sumup_service import SumUpService

_CENTS = Decimal("0.01")


@dataclass(frozen=True)
class CardTenderInput:
    """Ce que le POS envoie pour une ligne de paiement CB d'une vente."""

    checkout_id: str
    amount: Decimal


@dataclass(frozen=True)
class VerifiedCardTender:
    """Les 6 champs SumUp à écrire sur ``Payment`` une fois la carte confirmée."""

    sumup_checkout_id: str | None
    sumup_transaction_id: str | None
    sumup_transaction_code: str | None
    sumup_auth_code: str | None
    sumup_card_brand: str | None
    sumup_card_last4: str | None


class CardNotConfirmed(Exception):
    """Le paiement CB ne peut pas être confirmé — la vente doit être refusée (409)."""


async def verify_card_tender(
    db: AsyncSession,
    tender: CardTenderInput,
    client_uuid: uuid.UUID,
) -> VerifiedCardTender:
    """Relit SumUp et confirme qu'un paiement CB peut couvrir cette vente.

    Étapes (D5, §4.5) :
    1. L'``attempt`` référencé par ``tender.checkout_id`` doit exister.
    2. Son ``client_uuid`` doit être EXACTEMENT celui de la vente en cours
       (empêche de réutiliser le paiement d'une autre vente).
    3. Relecture SumUp (``get_checkout_status``) : le statut doit être
       ``PAID``.
    4. Le montant payé côté SumUp doit être identique au centime au montant
       de la ligne de paiement.
    5. Complète les identifiants de traçabilité via ``get_transaction``
       (transaction_code / auth_code / card_brand / last4).

    En cas de succès, met à jour l'``attempt`` (``status=paid`` + champs
    SumUp) dans la MÊME session que l'appelant — pas de commit ici, c'est la
    responsabilité de ``PosService.create_transaction`` (une seule écriture
    SQL pour la vente + l'attempt, §3). Lève ``CardNotConfirmed`` (→ 409)
    dans tous les autres cas ; rien n'est modifié en base dans ce cas.
    """
    attempt = (
        await db.execute(
            select(PaymentAttempt).where(PaymentAttempt.checkout_id == tender.checkout_id)
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise CardNotConfirmed(f"Paiement CB introuvable : {tender.checkout_id}")

    if attempt.client_uuid != client_uuid:
        raise CardNotConfirmed(
            "Le paiement CB ne correspond pas à cette vente (client_uuid différent)."
        )

    svc = SumUpService()
    status_result = await svc.get_checkout_status(tender.checkout_id)
    sumup_status = str(status_result.get("status") or "").upper()
    if sumup_status != "PAID":
        raise CardNotConfirmed(
            f"Paiement CB non confirmé par SumUp (statut : {sumup_status or 'inconnu'})."
        )

    reported_amount = status_result.get("amount")
    if reported_amount is None:
        raise CardNotConfirmed("Montant du paiement CB indisponible côté SumUp.")
    reported = Decimal(str(reported_amount)).quantize(_CENTS)
    expected = Decimal(tender.amount).quantize(_CENTS)
    if reported != expected:
        raise CardNotConfirmed(
            f"Montant CB différent : attendu {expected} €, SumUp a débité {reported} €."
        )

    # Complète les identifiants de traçabilité (auth_code, marque, 4 derniers
    # chiffres) via la Transactions API — la réponse de statut du push reader
    # les inclut déjà quand PAID, mais get_transaction() reste la source
    # canonique (§4.5 : « get_checkout_status puis get_transaction »).
    sumup_transaction_id = status_result.get("sumup_transaction_id")
    sumup_transaction_code = status_result.get("sumup_transaction_code")
    sumup_auth_code = status_result.get("sumup_auth_code")
    sumup_card_brand = status_result.get("sumup_card_brand")
    sumup_card_last4 = status_result.get("sumup_card_last4")

    txn = None
    if sumup_transaction_id:
        txn = await svc.get_transaction(transaction_id=sumup_transaction_id)
    if txn:
        card = txn.get("card") or {}
        sumup_transaction_id = txn.get("id") or sumup_transaction_id
        sumup_transaction_code = txn.get("transaction_code") or sumup_transaction_code
        sumup_auth_code = txn.get("auth_code") or sumup_auth_code
        sumup_card_brand = card.get("type") or card.get("scheme") or sumup_card_brand
        sumup_card_last4 = card.get("last_4_digits") or sumup_card_last4

    attempt.status = PaymentAttemptStatus.paid
    attempt.sumup_transaction_id = sumup_transaction_id
    attempt.sumup_transaction_code = sumup_transaction_code
    attempt.sumup_auth_code = sumup_auth_code
    attempt.sumup_card_brand = sumup_card_brand
    attempt.sumup_card_last4 = sumup_card_last4
    await db.flush()

    # PR9/K3 — point de constat n° 2 du `paid`, le seul ou la vente existe :
    # si ce panier avait un incident en file (terminal muet, puis reessai),
    # la ligne se referme ICI et porte enfin la vente encaissee. On lit la
    # vente en cours d'ecriture par son `client_uuid` (deja `flush`ee par
    # `PosService.create_transaction` avant la boucle des paiements) plutot
    # que de changer la signature de cette fonction, qui est l'interface
    # opposable a `pos.py`. Aucune vente n'est creee ici : on ne fait que
    # rattacher celle que l'appelant est en train d'ecrire.
    transaction_id = (
        await db.execute(
            select(Transaction.id)
            .where(
                Transaction.client_uuid == client_uuid,
                Transaction.transaction_type == TransactionType.sale,
            )
            .order_by(Transaction.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    await failed_payment_service.resolve_if_queued(db, client_uuid, transaction_id)

    return VerifiedCardTender(
        sumup_checkout_id=tender.checkout_id,
        sumup_transaction_id=sumup_transaction_id,
        sumup_transaction_code=sumup_transaction_code,
        sumup_auth_code=sumup_auth_code,
        sumup_card_brand=sumup_card_brand,
        sumup_card_last4=sumup_card_last4,
    )
