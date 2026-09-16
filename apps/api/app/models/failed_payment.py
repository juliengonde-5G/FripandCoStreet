# Nouveau modele (PR9, docs/ARCHITECTURE_PR9.md, contrat K3) — file des
# paiements carte echoues pour une cause RECUPERABLE (terminal injoignable,
# delai depasse, 5xx SumUp). Un refus de carte n'entre jamais dans la file :
# la cliente change de carte ou de moyen de paiement, il n'y a rien a
# rejouer.
#
# Table d'EXPLOITATION, hors perimetre fiscal : la vente n'est JAMAIS creee
# tant que le paiement n'est pas constate `paid` (regle PR2 inchangee). Une
# ligne ici n'est qu'un pense-bete pour la vendeuse : « ce panier n'est pas
# perdu, on peut relancer le terminal ».
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FailedPaymentStatus(str, enum.Enum):
    pending = "pending"
    # Un reessai a abouti : la vente a ete encaissee (`transaction_id`).
    succeeded = "succeeded"
    # `retry_count` a atteint `max_retries` — la vendeuse doit encaisser
    # autrement.
    exhausted = "exhausted"
    # Abandon explicite depuis l'admin (motif libre journalise au JET).
    abandoned = "abandoned"


class FailedPayment(Base):
    __tablename__ = "failed_payments"

    # UNIQUE : un essai de paiement ne donne qu'une seule ligne en file, les
    # reessais incrementent `retry_count` sur cette meme ligne.
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_attempts.id"), nullable=False, unique=True
    )
    # Reference de la vente A VENIR, reprise de l'essai de paiement.
    client_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[FailedPaymentStatus] = mapped_column(
        Enum(FailedPaymentStatus, name="failed_payment_status"),
        nullable=False,
        default=FailedPaymentStatus.pending,
    )
    # Memes valeurs que `sumup_exchanges.error_type`, plus `declined`.
    error_type: Mapped[str | None] = mapped_column(String(length=20), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Vente finalement encaissee apres un reessai reussi.
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=True
    )
    cashier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cashiers.id"), nullable=True
    )
