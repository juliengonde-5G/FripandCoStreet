# Extrait de Vintiz (apps/api/app/models/pos.py::Receipt), reduit — pas de
# `receipt_number`/`printed` distincts : le numero de ticket EST le numero de
# transaction (contrat §2, `Transaction.receipt_number`), et l'impression
# elle-meme est hors perimetre PR2 (generation de texte seulement).
import uuid

from sqlalchemy import ForeignKey, Integer, Text
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
    # Incremente a chaque reimpression/renvoi (JET `receipt.duplicate`).
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    transaction: Mapped["Transaction"] = relationship(  # noqa: F821
        "Transaction", back_populates="receipt", lazy="selectin"
    )
