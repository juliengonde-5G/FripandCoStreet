# Nouvelle migration (PR5, docs/ARCHITECTURE_PR5.md §1, G2) — sauvegardes
# applicatives planifiees de la base (`database_backups`). Chaque commande SQL
# est executee separement (contrainte asyncpg — cf. migrations 0001-0005).
#
# Table d'EXPLOITATION, pas fiscale : contrairement a `fiscal_closures`/
# `accounting_exports` (migration 0005), AUCUN trigger d'immuabilite —
# purgeable par retention (G1/G3). La migration est donc REVERSIBLE
# (downgrade droppe simplement la table + les types enum), a la difference
# des migrations 0001-0005.
"""Sauvegardes applicatives de la base — PR5

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "database_backups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "trigger",
            sa.Enum("nightly", "manual", name="database_backup_trigger"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("success", "failed", "missing", name="database_backup_status"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "triggered_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.UniqueConstraint("filename", name="uq_database_backups_filename"),
    )
    op.create_index("ix_database_backups_created_at", "database_backups", ["created_at"])


def downgrade() -> None:
    # Table d'exploitation (pas de trigger d'immuabilite NF525) : downgrade
    # reel, contrairement aux migrations fiscales 0001-0005.
    op.drop_index("ix_database_backups_created_at", table_name="database_backups")
    op.drop_table("database_backups")
    sa.Enum(name="database_backup_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="database_backup_trigger").drop(op.get_bind(), checkfirst=True)
