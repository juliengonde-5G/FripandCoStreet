# Nouveau modele (PR8, docs/ARCHITECTURE_PR8.md, contrat J1) — une vendeuse
# est une IDENTITE DE CAISSE, pas un compte utilisateur : elle ne se
# connecte pas a l'application (le compte manager unique reste le seul
# compte, cf. CLAUDE.md), elle s'identifie en caisse par un code PIN a 4
# chiffres pour que chaque vente, mouvement et cloture porte son nom.
#
# Jamais supprimee : les ventes deja signees la referencent (`cashier_id`,
# gele par le trigger d'immuabilite). Une vendeuse qui s'en va est
# DESACTIVEE (`active=False` + `deactivated_at`), ce qui l'empeche de
# s'identifier sans rien effacer de l'historique.
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Cashier(Base):
    __tablename__ = "cashiers"

    # Prenom affiche sur le ticket (« Vendeuse : Lea ») et sur les cartes de
    # l'ecran d'identification. Unique INSENSIBLE A LA CASSE (index
    # `uq_cashiers_display_name_ci`, migration 0008).
    display_name: Mapped[str] = mapped_column(String(length=60), nullable=False)
    # bcrypt (`core/security.hash_password`). NULL = aucun PIN defini : la
    # vendeuse existe mais ne peut pas encore s'identifier. Jamais expose
    # par l'API — seul `has_pin` (booleen) l'est.
    pin_hash: Mapped[str | None] = mapped_column(String(length=100), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
