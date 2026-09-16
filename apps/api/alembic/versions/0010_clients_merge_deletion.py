# Nouvelle migration (PR10, docs/ARCHITECTURE_PR10.md, contrat L1) — deux
# sujets sur la MEME table `clients`, d'ou une seule revision :
#   - fusion de fiches en double : `merged_into_client_id` (la fiche
#     absorbee pointe vers la fiche conservee) et `merged_at` ;
#   - suppression RGPD DIFFEREE : `deletion_requested_at`,
#     `deletion_scheduled_for` et `deletion_requested_by_user_id`.
#
# Rien ici n'est fiscal. `clients` est une table d'EXPLOITATION (donnees
# personnelles) :
#   - `trg_protect_signed_transaction` sur `transactions` (0002/0003) n'est
#     PAS touche — la fusion se contente de repointer
#     `transactions.client_id`, seule colonne exemptee du trigger et absente
#     du payload signe (`services/fiscal.py::_transaction_payload`). Aucune
#     vente n'est donc modifiee au sens fiscal, et aucune n'est jamais
#     supprimee ;
#   - `trg_protect_consent` sur `consents` (registre append-only, 0003) voit
#     sa fonction reecrite pour la meme raison et sous les memes garanties :
#     voir le bloc dedie dans `upgrade()`.
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
# Une exemption cible est posee sur le trigger append-only `consents` : la
# fusion doit repointer `consents.client_id` (le RATTACHEMENT) sans jamais
# toucher au contenu du consentement — meme raisonnement, et meme forme,
# que l'exemption obtenue par PR3 sur `transactions.client_id`.
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

    # ------------------------------------------------------------------
    # `consents` : exemption de `client_id`, strictement calquee sur celle
    # obtenue par PR3 sur `transactions`.
    #
    # Le registre est append-only (0003) : le trigger refuse AUJOURD'HUI
    # tout UPDATE, quelle que soit la colonne. Or la fusion doit repointer
    # les consentements de la fiche absorbee vers la fiche conservee, sans
    # quoi l'historique serait perdu ou duplique. Le rattachement n'est pas
    # le CONTENU du consentement : la finalite, le sens (accorde/retire),
    # la source, la version de politique, l'auteur, la note et la date
    # restent rigoureusement immuables — c'est ce que verifie la nouvelle
    # fonction, colonne par colonne. Seuls `client_id` (le rattachement) et
    # `updated_at` (horodatage technique, deja hors perimetre sur
    # `transactions`) peuvent bouger. La SUPPRESSION reste interdite sans
    # condition.
    #
    # Autrement dit : on ne reecrit jamais un consentement, on constate que
    # les deux fiches n'en faisaient qu'une.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fripco_protect_consent()
        RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'NF525: registre des consentements append-only (suppression interdite)';
          END IF;
          IF OLD.id IS DISTINCT FROM NEW.id OR
             OLD.purpose IS DISTINCT FROM NEW.purpose OR
             OLD.granted IS DISTINCT FROM NEW.granted OR
             OLD.source IS DISTINCT FROM NEW.source OR
             OLD.policy_version IS DISTINCT FROM NEW.policy_version OR
             OLD.recorded_by_user_id IS DISTINCT FROM NEW.recorded_by_user_id OR
             OLD.note IS DISTINCT FROM NEW.note OR
             OLD.created_at IS DISTINCT FROM NEW.created_at
          THEN
            RAISE EXCEPTION 'NF525: registre des consentements append-only (modification interdite)';
          END IF;
          -- Seul le rattachement (`client_id`) a pu changer : fusion de
          -- deux fiches en double (PR10/L3).
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    # Retour a la fonction de 0003 : aucun UPDATE, aucun DELETE.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fripco_protect_consent()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'NF525: registre des consentements append-only (modification/suppression interdite)';
        END;
        $$ LANGUAGE plpgsql
        """
    )

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
