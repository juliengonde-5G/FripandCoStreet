# Nouveau modele (PR5, docs/ARCHITECTURE_PR5.md §1, G2) — une ligne par
# tentative de sauvegarde applicative de la base (succes, echec ou disparue
# du disque), ecrite par `app/services/database_backup.py`. Table
# d'EXPLOITATION, PAS fiscale : contrairement a `fiscal_closures`/
# `accounting_exports` (migration 0005), AUCUN trigger d'immuabilite —
# purgeable par retention (§ G1/G3), a la difference des tables sous regime
# NF525.
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BackupTrigger(str, enum.Enum):
    nightly = "nightly"
    manual = "manual"


class BackupStatus(str, enum.Enum):
    success = "success"
    failed = "failed"
    # Ligne dont le fichier a disparu du disque (purge manuelle, disque
    # externe deconnecte...) — jamais recree automatiquement (G1).
    missing = "missing"


class DatabaseBackup(Base):
    __tablename__ = "database_backups"

    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trigger: Mapped[BackupTrigger] = mapped_column(
        Enum(BackupTrigger, name="database_backup_trigger"), nullable=False
    )
    status: Mapped[BackupStatus] = mapped_column(
        Enum(BackupStatus, name="database_backup_status"), nullable=False
    )
    # Nom du fichier dans `settings.BACKUP_DIR` (ex. fripco_20260915_030000.sql.gz)
    # — jamais un chemin absolu ici (evite de fuiter l'arborescence serveur
    # dans une reponse API), reconstitue via `database_backup.backup_dir()`.
    filename: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Message d'erreur tronque a 500 caracteres, jamais de mot de passe
    # (G1) — la trace complete reste dans les logs serveur uniquement.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
