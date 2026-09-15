# Nouveau modele (PR4, docs/ARCHITECTURE_PR4.md §2 F2/F3) — une ecriture
# comptable par cloture Z (`accounting_exports` + `accounting_export_lines`),
# immuables (trigger migration 0005). Reduit du module equivalent de
# l'application source (`app/models/accounting.py`) : pas de config Pennylane
# persistee ici (F1 — la config comptable vit dans `app_settings.accounting`,
# comme les autres parametres boutique, cf. `services/settings_service.py`),
# pas d'integration Pennylane (decision Julien, §1 du contrat : le CSV/FEC
# s'importe par fichier).
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class AccountingExport(Base):
    """Une ecriture comptable = une cloture Z (D2)."""

    __tablename__ = "accounting_exports"

    z_report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("z_reports.id"), nullable=False, unique=True
    )
    export_date: Mapped[date] = mapped_column(Date, nullable=False)

    total_sales_ht: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total_tva: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total_ttc: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total_refunds_ttc: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total_cash: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total_card: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total_debit: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total_credit: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    rounding_adjustment: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)

    fec_content: Mapped[str | None] = mapped_column(Text, nullable=True)

    lines: Mapped[list["AccountingExportLine"]] = relationship(
        "AccountingExportLine", back_populates="export", lazy="selectin",
        order_by="AccountingExportLine.line_number",
    )


class AccountingExportLine(Base):
    """Ligne d'ecriture comptable (journal des ventes, D2)."""

    __tablename__ = "accounting_export_lines"

    export_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounting_exports.id"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    account_number: Mapped[str] = mapped_column(String(20), nullable=False)
    account_label: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    debit: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    credit: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    piece_reference: Mapped[str] = mapped_column(String(32), nullable=False)

    export: Mapped["AccountingExport"] = relationship(
        "AccountingExport", back_populates="lines"
    )
