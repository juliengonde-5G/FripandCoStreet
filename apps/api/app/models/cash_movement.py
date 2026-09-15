# Extrait de l'application source (apps/api/app/models/cash_movement.py) — reason
# `personal_withdrawal` retire (hors perimetre CDC Frip & Co Street, §2 du
# contrat PR2).
from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CashMovementDirection(str, enum.Enum):
    inflow = "in"
    outflow = "out"


class CashMovementReason(str, enum.Enum):
    bank_deposit = "bank_deposit"
    supplier_payment = "supplier_payment"
    float_top_up = "float_top_up"
    other = "other"


class CashMovement(Base):
    """Entree/sortie de caisse en cours de journee (ni vente, ni remboursement).

    Utilise par `PosService.close_drawer` pour calculer le fond attendu
    (D11) : opening + ventes especes - remboursements especes + in - out.
    """

    __tablename__ = "cash_movements"

    drawer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cash_drawers.id"), nullable=False
    )
    direction: Mapped[CashMovementDirection] = mapped_column(
        # Les LABELS de l'enum PG sont "in"/"out" — les VALEURS de l'enum
        # Python, pas ses noms (inflow/outflow). Sans values_callable,
        # SQLAlchemy persisterait le NOM, que le type PG rejetterait.
        Enum(
            CashMovementDirection,
            name="cash_movement_direction",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    reason: Mapped[CashMovementReason] = mapped_column(
        Enum(CashMovementReason, name="cash_movement_reason"),
        nullable=False,
        default=CashMovementReason.other,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # PR8 (J1) — vendeuse qui a fait le mouvement, recopiee depuis
    # `cash_drawers.current_cashier_id`. La table est append-only
    # (`fripco_protect_cash_movement`) : la colonne est donc gelee par
    # construction, comme toutes les autres.
    cashier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cashiers.id"), nullable=True
    )
