# Nouveau modele (PR8, docs/ARCHITECTURE_PR8.md, contrat J5) — facture pour
# un client PROFESSIONNEL et avoir correspondant.
#
# Le client B2B n'est volontairement PAS une fiche `clients` : une raison
# sociale et un SIRET ne sont pas des donnees personnelles de personne
# physique, elles n'ont rien a faire dans le registre de consentement ni
# dans le perimetre de l'anonymisation RGPD. Les coordonnees sont donc
# FIGEES dans la facture au moment de son emission, et conservees 10 ans.
#
# Immuabilite : trigger `fripco_protect_invoice` (migration 0008) — DELETE
# refuse, et le seul UPDATE tolere est celui qui pose `pdf_sha256` alors
# qu'il valait NULL (empreinte du PDF, calculee au premier rendu).
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InvoiceKind(str, enum.Enum):
    invoice = "invoice"
    # Avoir : emis automatiquement a l'annulation d'une vente facturee,
    # numerote `A-AAAA-NNNN` et rattache a la facture d'origine
    # (`original_invoice_id`).
    credit_note = "credit_note"


class Invoice(Base):
    __tablename__ = "invoices"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=False
    )
    kind: Mapped[InvoiceKind] = mapped_column(
        Enum(InvoiceKind, name="invoice_kind"), nullable=False, default=InvoiceKind.invoice
    )
    # `F-AAAA-NNNN` (facture) ou `A-AAAA-NNNN` (avoir) — compteur par annee,
    # attribue sous le verrou fiscal partage avec les ventes, donc sans trou
    # ni doublon meme sur deux emissions concurrentes.
    invoice_number: Mapped[str] = mapped_column(String(length=20), nullable=False, unique=True)
    original_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=True
    )

    company_name: Mapped[str] = mapped_column(String(length=120), nullable=False)
    # 14 chiffres, validation Luhn cote service (pas de CHECK en base : on
    # ne fige pas un algorithme dans le schema).
    siret: Mapped[str] = mapped_column(String(length=14), nullable=False)
    vat_number: Mapped[str | None] = mapped_column(String(length=20), nullable=True)
    address_line1: Mapped[str] = mapped_column(String(length=120), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(length=120), nullable=True)
    postal_code: Mapped[str] = mapped_column(String(length=10), nullable=False)
    city: Mapped[str] = mapped_column(String(length=80), nullable=False)

    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Posee au premier rendu du PDF (deterministe : deux rendus du meme
    # document donnent la meme empreinte). Une fois posee, la ligne est
    # totalement figee.
    pdf_sha256: Mapped[str | None] = mapped_column(String(length=64), nullable=True)
    # Manager connecte au moment de l'emission (tracabilite ; la vendeuse,
    # elle, est portee par la vente via `transactions.cashier_id`).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
