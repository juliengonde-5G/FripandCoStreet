# Extrait de l'application source (structure inspiree de apps/api/app/services/app_config.py
# et de la table `app_settings` du meme depot) — remplace le fichier JSON
# `data/app_config.json` de l'application source : ici les parametres boutique vivent en
# base (D13 du contrat PR2) et toute ecriture est journalisee au JET par
# `SettingsService.set` (evenement `config.changed`), jamais silencieuse.
#
# `key` est la cle primaire naturelle (pas de colonne `id` de surcharge) : au
# plus une ligne par cle (`shop`, `fiscal`, `receipt`), donc `key` porte deja
# l'unicite dont un `id` UUID n'apporterait rien. Assigner `id = None` retire
# la colonne heritee de `Base` — technique documentee de SQLAlchemy pour
# exclure un attribut de mixin sur une sous-classe.
import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AppSetting(Base):
    """Un parametre boutique versionnable (`shop`, `fiscal`, `receipt`).

    L'immuabilite ne s'applique PAS a cette table (a la difference des
    tables fiscales) : c'est un parametrage, pas une preuve. Seule la trace
    de chaque changement (JET `config.changed`) est immuable.
    """

    __tablename__ = "app_settings"

    id = None  # 'key' est la cle primaire naturelle — pas de surrogate id.

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
