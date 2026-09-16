# Nouvelle migration (PR10, docs/ARCHITECTURE_PR10.md, contrat L1) — deux
# sujets sur la MEME table `clients`, d'ou une seule revision :
#   - fusion de fiches en double : `merged_into_client_id` (la fiche
#     absorbee pointe vers la fiche conservee) et `merged_at` ;
#   - suppression RGPD DIFFEREE : `deletion_requested_at`,
#     `deletion_scheduled_for` et `deletion_requested_by_user_id`.
#
# Rien ici n'est fiscal. `clients` est une table d'EXPLOITATION (donnees
# personnelles) : aucun trigger d'immuabilite n'est pose ni touche, et les
# protections existantes restent entieres :
#   - `trg_protect_consent` sur `consents` (registre append-only, 0003) ;
#   - `trg_protect_signed_transaction` sur `transactions` (0002/0003) — la
#     fusion se contente de repointer `transactions.client_id`, seule
#     colonne exemptee du trigger et absente du payload signe
#     (`services/fiscal.py::_transaction_payload`). Aucune vente n'est donc
#     modifiee au sens fiscal, et aucune n'est jamais supprimee.
#
# La cle etrangere `merged_into_client_id -> clients.id` est auto-referente
# et volontairement SANS `ON DELETE` : on ne supprime jamais une ligne
# `clients` (RGPD = anonymisation, cf. `client_service.anonymize`).
#
# L'index sur `deletion_scheduled_for` est PARTIEL
# (`WHERE deletion_scheduled_for IS NOT NULL`) : le cron quotidien ne
# cherche que les rares fiches en attente, il n'a aucune raison de faire
# grossir l'index avec l'immense majorite des fiches a NULL.
#
# Migration REVERSIBLE (comme 0006-0009) : le downgrade retire les index
# puis les colonnes. Chaque commande SQL est executee separement
# (contrainte asyncpg — cf. migrations 0001-0009).
"""Fusion de fiches clientes et suppression RGPD differee — PR10

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Fusion (L3) — la fiche ABSORBEE porte le pointeur vers la fiche
    # conservee. Sens choisi a dessein : une fiche conservee peut absorber
    # plusieurs doublons, l'inverse n'a pas de sens.
    # ------------------------------------------------------------------
    op.add_column(
        "clients",
        sa.Column("merged_into_client_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_clients_merged_into_client_id",
        "clients",
        "clients",
        ["merged_into_client_id"],
        ["id"],
    )
    op.add_column(
        "clients",
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Sert deux lectures : « quelles fiches ont ete absorbees par celle-ci »
    # (redirection depuis le front) et le filtre `IS NULL` pose partout
    # ailleurs (recherche, statistiques de visites, `create_or_get`).
    op.create_index(
        "ix_clients_merged_into_client_id",
        "clients",
        ["merged_into_client_id"],
    )

    # ------------------------------------------------------------------
    # Suppression RGPD differee (L5) — la demande est enregistree, la
    # fiche reste utilisable en caisse jusqu'a la date d'effet, et un cron
    # quotidien anonymise a echeance.
    # ------------------------------------------------------------------
    op.add_column(
        "clients",
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("deletion_scheduled_for", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column(
            "deletion_requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_clients_deletion_requested_by_user_id",
        "clients",
        "users",
        ["deletion_requested_by_user_id"],
        ["id"],
    )
    op.create_index(
        "ix_clients_deletion_scheduled_for",
        "clients",
        ["deletion_scheduled_for"],
        postgresql_where=sa.text("deletion_scheduled_for IS NOT NULL"),
    )


def downgrade() -> None:
    # Reversible sans perte fiscale : ces colonnes ne portent que des
    # metadonnees d'exploitation. Les fiches absorbees redeviennent
    # simplement des fiches ordinaires (leurs ventes restent rattachees a
    # la fiche conservee — le rattachement, lui, n'est pas annule : c'est
    # une donnee, pas un artefact de schema).
    op.drop_index("ix_clients_deletion_scheduled_for", table_name="clients")
    op.drop_constraint(
        "fk_clients_deletion_requested_by_user_id", "clients", type_="foreignkey"
    )
    op.drop_column("clients", "deletion_requested_by_user_id")
    op.drop_column("clients", "deletion_scheduled_for")
    op.drop_column("clients", "deletion_requested_at")

    op.drop_index("ix_clients_merged_into_client_id", table_name="clients")
    op.drop_constraint("fk_clients_merged_into_client_id", "clients", type_="foreignkey")
    op.drop_column("clients", "merged_at")
    op.drop_column("clients", "merged_into_client_id")
