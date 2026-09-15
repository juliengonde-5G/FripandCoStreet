# Extrait de Vintiz (apps/api/app/models/payment_attempt.py), reduit au
# workflow CB SumUp du contrat PR2 (§2) — jamais source de verite fiscale
# (la Transaction/Payment le sont). Table mutable : `status` transitionne
# pending -> paid|failed|cancelled.
from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PaymentAttemptStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    failed = "failed"
    cancelled = "cancelled"


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"

    # Reference de la vente A VENIR (la Transaction n'existe pas encore tant
    # que le paiement n'est pas confirme) — meme UUID que `client_uuid` de la
    # Transaction une fois celle-ci ecrite.
    client_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[PaymentAttemptStatus] = mapped_column(
        Enum(PaymentAttemptStatus, name="payment_attempt_status"),
        nullable=False,
        default=PaymentAttemptStatus.pending,
    )
    checkout_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # Identifiant remis au reader SumUp Solo (D5) — permet de retrouver
    # l'intention de vente cote SumUp meme si le poll est interrompu.
    client_transaction_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reader_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    sumup_transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sumup_transaction_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sumup_auth_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sumup_card_brand: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sumup_card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)

    # Rempli quand la vente est ecrite (attempt reussi -> Transaction).
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
