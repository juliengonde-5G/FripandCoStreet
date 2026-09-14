# Nouveau modele (PR3, docs/ARCHITECTURE_PR3.md §2) — trace chaque envoi
# d'e-mail (E7). Pas de contenu du message : le ticket texte fige vit deja
# dans `receipts.content` (migration 0002) ; cette table ne porte que la
# preuve d'envoi (destinataire, fournisseur, statut).
import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CommunicationKind(str, enum.Enum):
    receipt = "receipt"


class CommunicationChannel(str, enum.Enum):
    email = "email"


class CommunicationProvider(str, enum.Enum):
    brevo = "brevo"
    smtp = "smtp"
    simulated = "simulated"


class CommunicationStatus(str, enum.Enum):
    sent = "sent"
    failed = "failed"
    simulated = "simulated"


class Communication(Base):
    __tablename__ = "communications"

    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=True
    )
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=True
    )
    kind: Mapped[CommunicationKind] = mapped_column(
        Enum(CommunicationKind, name="communication_kind"), nullable=False
    )
    channel: Mapped[CommunicationChannel] = mapped_column(
        Enum(CommunicationChannel, name="communication_channel"), nullable=False
    )
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider: Mapped[CommunicationProvider] = mapped_column(
        Enum(CommunicationProvider, name="communication_provider"), nullable=False
    )
    status: Mapped[CommunicationStatus] = mapped_column(
        Enum(CommunicationStatus, name="communication_status"), nullable=False
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
