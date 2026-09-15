# Nouveau modele (PR4, docs/ARCHITECTURE_PR4.md §2 F5) — clotures periodiques
# (mensuelle/annuelle/manuelle) : chacune scelle un intervalle [period_start,
# period_end) sous la forme d'une archive JSON gzip autoportante (transactions,
# lignes, paiements, Z, mouvements de caisse, JET de la periode, tickets
# texte, reglages boutique), avec un manifeste HMAC chaine a la cloture
# precedente (meme convention que `services/fiscal.py`/`services/jet.py` :
# genesis "0", `FiscalService._hmac`/`_canonical` reutilises tels quels).
# Modelise sur le module equivalent de l'application source
# (`app/models/fiscal_closure.py`), reduit au perimetre Frip & Co Street
# (pas de `cashier_id`, compte unique).
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, LargeBinary, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ClosureType(str, enum.Enum):
    monthly = "monthly"
    annual = "annual"
    manual = "manual"


class FiscalClosure(Base):
    """Une cloture periodique scellee (immuable — trigger migration 0005)."""

    __tablename__ = "fiscal_closures"

    sequence_number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    closure_type: Mapped[ClosureType] = mapped_column(
        Enum(ClosureType, name="fiscal_closure_type"), nullable=False
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    software_version: Mapped[str] = mapped_column(String(32), nullable=False)
    fiscal_version_date: Mapped[str] = mapped_column(String(16), nullable=False)

    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_transaction_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_transaction_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_transaction_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_z_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_z_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_z_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    jet_last_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    jet_last_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    grand_total_sales: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    grand_total_refunds: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    grand_total_net: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)

    perpetual_sales: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    perpetual_refunds: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    perpetual_net: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    perpetual_transaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    archive_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    closed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
