# Nouveau modele (PR11, docs/ARCHITECTURE_PR11.md, contrat M2) — une ligne
# par journee civile Europe/Paris du cahier du jour.
#
# Table d'EXPLOITATION, pas de trace fiscale : les chiffres du cahier
# (realise, panier moyen, ventilation horaire) sont recalcules a chaque
# lecture depuis `transactions`. Ne vivent ici que les elements qu'aucun
# recalcul ne pourrait retrouver : l'objectif fige au moment ou la journee a
# ete ouverte, ce que la boutique a ecrit ce jour-la, qui a signe, et la
# meteo constatee (le fournisseur ne sait pas rejouer le passe).
import uuid
from datetime import date as date_cls
from datetime import datetime

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CahierDay(Base):
    __tablename__ = "cahier_days"

    id = None  # `day` est la cle primaire naturelle — pas de surrogate id.

    day: Mapped[date_cls] = mapped_column(Date, primary_key=True)
    # Fige a la PREMIERE lecture du jour (M2) : revoir l'objectif mensuel en
    # cours de mois ne doit pas reecrire la progression des jours deja
    # consultes. NULL = aucun objectif fixe ce jour-la.
    frozen_daily_target: Mapped[float | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation: Mapped[str | None] = mapped_column(Text, nullable=True)

    manager_signed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    manager_signed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    team_signed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Nom libre : l'equipe n'a pas toujours de vendeuse identifiee sur le
    # tiroir (reglage `pos.cashier_required` a false).
    team_signed_by_name: Mapped[str | None] = mapped_column(
        String(length=60), nullable=True
    )
    team_signed_by_cashier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cashiers.id"), nullable=True
    )

    weather_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
