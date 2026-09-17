# Nouvelle migration (PR11, docs/ARCHITECTURE_PR11.md, contrat M2) — table
# `cahier_days` : le cahier du jour de la boutique (objectif fige, message du
# jour, operation en cours, signatures, instantane meteo).
#
# Rien ici n'est fiscal : `cahier_days` est une table d'EXPLOITATION. Elle ne
# porte aucun montant encaisse, ne reference aucune vente, et n'est protegee
# par aucun trigger d'immuabilite — une vendeuse doit pouvoir corriger le
# message du jour, ce qui serait impossible sur une table scellee. Les
# chiffres affiches par le cahier sont RECALCULES a la lecture depuis
# `transactions` (source de verite signee), jamais recopies ici.
#
# `day` est la cle primaire naturelle : au plus une ligne par journee civile
# Europe/Paris. Pas de colonne `id` de surcharge (meme choix que
# `app_settings`, migration 0002) — un UUID n'apporterait aucune unicite que
# la date ne porte deja.
#
# `frozen_daily_target` est FIGE a la premiere lecture du jour : l'objectif
# mensuel peut etre revu en cours de mois (et il l'est), mais un jour deja
# consulte et signe ne doit pas voir son objectif se reecrire a posteriori —
# sinon la progression affichee hier ne serait plus celle relue demain.
# Nullable : un jour peut tres bien n'avoir aucun objectif.
#
# Les deux cles etrangeres de signature sont volontairement SANS `ON DELETE` :
# ni `users` ni `cashiers` ne connaissent la suppression de ligne (une
# vendeuse qui part est desactivee, cf. `app/models/cashier.py`).
#
# Migration REVERSIBLE (comme 0006-0010) : le downgrade supprime la table.
# Chaque commande SQL est executee separement (contrainte asyncpg — cf.
# migrations 0001-0010).
"""Cahier du jour — PR11

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cahier_days",
        sa.Column("day", sa.Date(), primary_key=True, nullable=False),
        sa.Column("frozen_daily_target", sa.Numeric(10, 2), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("operation", sa.Text(), nullable=True),
        sa.Column("manager_signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "manager_signed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("team_signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("team_signed_by_name", sa.String(length=60), nullable=True),
        sa.Column(
            "team_signed_by_cashier_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        # Instantane meteo (M3) fige au premier appel du jour meme : la
        # meteo d'hier n'est plus interrogeable chez le fournisseur, donc si
        # on ne la garde pas au moment ou on la lit, elle est perdue.
        sa.Column("weather_snapshot", postgresql.JSONB(), nullable=True),
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
    )
    op.create_foreign_key(
        "fk_cahier_days_manager_signed_by_user_id",
        "cahier_days",
        "users",
        ["manager_signed_by_user_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_cahier_days_team_signed_by_cashier_id",
        "cahier_days",
        "cashiers",
        ["team_signed_by_cashier_id"],
        ["id"],
    )


def downgrade() -> None:
    # Reversible sans perte fiscale : la table ne porte que du confort de
    # pilotage (objectif fige, textes libres, signatures, meteo). Aucune
    # vente, aucun Z, aucun evenement du JET n'y est stocke.
    op.drop_table("cahier_days")
