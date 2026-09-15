# Extrait de l'application source (apps/api/app/models/base.py)
# Pas de portabilite SQLite necessaire ici : les tests tournent contre
# PostgreSQL (les triggers d'immuabilite NF525 doivent etre testes pour de
# vrai), donc les types PostgreSQL natifs (UUID) sont utilises directement.
import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Classe de base pour tous les modeles. Fournit id, created_at, updated_at."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
