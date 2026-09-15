# Extrait de l'application source (apps/api/app/models/pos.py), reduit au perimetre PR2
# (vente espece/CB, ticket, Z) — voir docs/ARCHITECTURE_PR2.md §2 pour le
# detail des colonnes retenues/ecartees. Signature fiscale v3 (D3) : pas de
# branche legacy v1/v2 a porter, nouvelle installation.
import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class TransactionType(str, enum.Enum):
    sale = "sale"
    refund = "refund"


class DiscountType(str, enum.Enum):
    percent = "percent"
    amount = "amount"


class PaymentMethod(str, enum.Enum):
    cash = "cash"
    card = "card"


class Transaction(Base):
    __tablename__ = "transactions"

    transaction_number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    transaction_type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, name="transaction_type"),
        nullable=False,
        default=TransactionType.sale,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # Idempotence (genere par le front avant l'envoi) — un replay reseau /
    # double-clic renvoie la transaction existante au lieu d'en creer une
    # seconde (cf. PosService.create_transaction, extrait du test équivalent
    # de l'application source test_pos_idempotence.py).
    client_uuid: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=True
    )
    # Refund : reference la vente d'origine (annulation totale, D4).
    original_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=True
    )
    refund_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # PR3 (E3, migration 0003) — SEULE colonne mutable hors hash sur une
    # transaction deja signee (avec `updated_at`) : le trigger
    # `fripco_protect_signed_transaction` l'exclut explicitement de sa
    # comparaison OLD/NEW. Hors payload signe (`fiscal.py::_transaction_payload`
    # ne la reference pas) : rattacher un client apres coup ne casse jamais
    # `verify_chain_integrity`.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=True
    )

    # PR8 (J1, migration 0008) — vendeuse qui a encaisse. HORS SIGNATURE
    # (comme `client_id` : absente de `fiscal.py::_transaction_payload`,
    # donc le `hash_chain` est le meme avec ou sans vendeuse) mais, a la
    # difference de `client_id`, posee a l'INSERT et GELEE : le trigger
    # `fripco_protect_signed_transaction` (reecrit en 0008) refuse toute
    # reattribution apres coup. NULL = vente encaissee sans identification
    # (reglage `pos.cashier_required` a false, ou historique anterieur).
    cashier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cashiers.id"), nullable=True
    )

    # Remise globale (D2) — ventilee au prorata sur les lignes, tracee dans
    # le payload signe (`fiscal.py::_transaction_payload`).
    discount_type: Mapped[DiscountType | None] = mapped_column(
        Enum(DiscountType, name="discount_type"), nullable=True
    )
    discount_value: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    discount_amount: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, default=0
    )

    # TVA regime normal (D8) — taux fige au moment de la vente, meme si le
    # parametre `app_settings.fiscal.tva_rate` change ensuite.
    tva_rate: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False)

    total_ht: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_tva: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_ttc: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)

    hash_chain: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fiscal_signature_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )
    # = transaction_number (colonne conservee pour la lisibilite du ticket,
    # cf. §2 du contrat).
    receipt_number: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)

    items: Mapped[list["TransactionItem"]] = relationship(
        "TransactionItem", back_populates="transaction", lazy="selectin"
    )
    payments: Mapped[list["Payment"]] = relationship(
        "Payment", back_populates="transaction", lazy="selectin"
    )
    receipt: Mapped["Receipt | None"] = relationship(  # noqa: F821
        "Receipt", back_populates="transaction", uselist=False, lazy="selectin"
    )


class TransactionItem(Base):
    __tablename__ = "transaction_items"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=False
    )
    # Fige a la vente — pas de reference produit (pas d'inventaire en PR2).
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="Article")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    discount_amount: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, default=0
    )
    line_total: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    tva_rate: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False)
    line_ht: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    line_tva: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # Refund : pointe vers la ligne de la vente d'origine (mirroir).
    original_transaction_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transaction_items.id"), nullable=True
    )
    # Ordre d'affichage sur le ticket / dans le payload signe.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    transaction: Mapped["Transaction"] = relationship(
        "Transaction", back_populates="items", lazy="selectin"
    )


class Payment(Base):
    __tablename__ = "payments"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=False
    )
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod, name="payment_method"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # Espece uniquement : montant physiquement remis / rendu.
    tendered_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    change_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)

    # Tracabilite SumUp (CB uniquement) — ecrites par `sumup_verify` (agent
    # B) apres relecture serveur de l'etat du checkout (D5). NULL si methode
    # != card. `sumup_environment` retire (production uniquement, D12/CDC).
    sumup_checkout_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sumup_transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sumup_transaction_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sumup_auth_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sumup_card_brand: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sumup_card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    sumup_refunded_amount: Mapped[float | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    transaction: Mapped["Transaction"] = relationship(
        "Transaction", back_populates="payments", lazy="selectin"
    )


class CashDrawer(Base):
    __tablename__ = "cash_drawers"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opening_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    closing_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    expected_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    # = closing - expected (D9).
    discrepancy: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    opening_breakdown: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    closing_breakdown: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    closing_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Fermee par la garde 23:59 (fiscal.close_open_drawers), pas par la
    # caissiere (D9/§4.3).
    closed_by_guard: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    z_report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("z_reports.id"), nullable=True
    )

    # PR8 (J1/J2) — vendeuses. `opened_by`/`closed_by` sont poses une fois
    # (ouverture, cloture) ; `current_cashier_id` est l'etat COURANT de la
    # caisse, mutable tant que le tiroir est ouvert : c'est lui que la
    # releve change en cours de journee, et c'est lui qui est recopie sur
    # chaque vente/mouvement. Rien de fiscal ici — le tiroir n'est fige
    # qu'une fois cloture (`fripco_protect_cash_drawer`).
    opened_by_cashier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cashiers.id"), nullable=True
    )
    closed_by_cashier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cashiers.id"), nullable=True
    )
    current_cashier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cashiers.id"), nullable=True
    )


class ZReport(Base):
    __tablename__ = "z_reports"

    report_number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    cash_drawer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cash_drawers.id"), unique=True, nullable=False
    )
    # PR8 (J1) — vendeuse qui tenait la caisse a la cloture. Hors payload
    # signe du Z (`fiscal.py::generate_z_report`) : posee a l'INSERT, et le
    # Z est scelle des sa creation (`fripco_protect_z_report` refuse tout
    # UPDATE), donc immuable de fait.
    cashier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cashiers.id"), nullable=True
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    total_sales: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_refunds: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_net: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_ht: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_tva: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_transaction_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_transaction_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "0" (genesis) si aucune vente sur la periode.
    last_transaction_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payment_totals: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Montants de caisse scelles dans le Z (D9 — corrige C-1/C-4/C-5 de
    # l'application source).
    opening_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    closing_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    expected_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    discrepancy: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    cash_in_total: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    cash_out_total: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    cash_movement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # False quand le Z a ete scelle par la garde 23:59 (pas de recomptage
    # espece reel — closing_amount = expected_amount).
    counted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    is_regularization: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    regularization_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Cumuls perpetuels (= valeurs du Z precedent + celles de ce Z), D9.
    cumulative_sales: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    cumulative_refunds: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False, default=0
    )
    cumulative_net: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    cumulative_transaction_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fiscal_signature_version: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
