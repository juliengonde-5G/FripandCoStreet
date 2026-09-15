# Nouveaux modeles (PR3, docs/ARCHITECTURE_PR3.md §2) : `clients` + registre
# de consentement append-only `consents`. Perimetre reduit a un seul purpose
# (`newsletter`) — le ticket par e-mail est un envoi transactionnel qui ne
# cree pas de contact et n'est donc pas un consentement (E2).
import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ConsentPurpose(str, enum.Enum):
    newsletter = "newsletter"


class ConsentSource(str, enum.Enum):
    pos = "pos"
    webhook = "webhook"
    admin = "admin"
    rgpd = "rgpd"


class Client(Base):
    __tablename__ = "clients"

    # Normalise (minuscules, trim) par `services/client_service.py` avant
    # toute ecriture — jamais fait cote base.
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Cache de l'etat courant du consentement `newsletter` (la source de
    # verite reste le registre append-only `consents`).
    newsletter_optin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    brevo_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    brevo_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # RGPD (E4) — pose par l'anonymisation. NULL = fiche active. Jamais de
    # suppression de ligne : les ventes gardent leur `client_id`.
    anonymized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )


class Consent(Base):
    """Ligne immuable du registre de consentement (E5).

    L'immuabilite (UPDATE/DELETE interdits) est appliquee cote base par le
    trigger `trg_protect_consent` (migration 0003) — pas seulement par
    convention applicative. L'etat courant d'un purpose pour un client est
    la ligne la plus recente (`created_at` decroissant).
    """

    __tablename__ = "consents"

    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False
    )
    purpose: Mapped[ConsentPurpose] = mapped_column(
        Enum(ConsentPurpose, name="consent_purpose"), nullable=False
    )
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[ConsentSource] = mapped_column(
        Enum(ConsentSource, name="consent_source"), nullable=False
    )
    policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
