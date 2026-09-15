# Extrait de l'application source (apps/api/app/models/pos.py::Receipt),
# reduit — pas de `receipt_number` distinct : le numero de ticket EST le
# numero de transaction (contrat §2, `Transaction.receipt_number`).
#
# PR3b (impression physique, migration 0004) : `printed_count`/`printed_at`
# comptent les impressions REELLES sur l'imprimante MUNBYN (reseau ou
# WebUSB) — distinct de `duplicate_count`, qui compte les LECTURES du texte
# via `GET /pos/transactions/{id}/receipt` (PR2). Le premier appel a
# `POST /pos/transactions/{id}/print` ou `GET .../escpos` fait passer
# `printed_count` de 0 a 1 (l'original) ; tout appel suivant est un
# duplicata (voir `app/api/pos/router.py`).
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Receipt(Base):
    __tablename__ = "receipts"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), unique=True, nullable=False
    )
    # Ticket texte fige a la vente (§4.4) — jamais regenere depuis les
    # donnees courantes de la boutique (un changement d'adresse ne doit pas
    # modifier retroactivement un ticket deja emis).
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Incremente a chaque reimpression/renvoi du TEXTE (JET `receipt.duplicate`).
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Incremente a chaque impression PHYSIQUE reussie (MUNBYN reseau ou
    # WebUSB) — JET `receipt.printed`. Migration 0004.
    printed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    printed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    transaction: Mapped["Transaction"] = relationship(  # noqa: F821
        "Transaction", back_populates="receipt", lazy="selectin"
    )
