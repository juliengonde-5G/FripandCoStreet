# Nouvelles tables (PR3, docs/ARCHITECTURE_PR3.md §2) : `clients`, `consents`
# (registre append-only), `communications` (traçabilité des envois de
# ticket par e-mail), et `transactions.client_id` (E3 — seule colonne
# mutable hors hash sur une transaction déjà signée). La fonction du
# trigger d'immuabilité `fripco_protect_signed_transaction` (migration 0002)
# est réécrite (`CREATE OR REPLACE FUNCTION`) pour ignorer `client_id` dans
# sa comparaison OLD/NEW — c'est cette omission, et elle seule, qui autorise
# `UPDATE transactions SET client_id = …` sur une vente signée tout en
# continuant à refuser toute autre colonne. Chaque commande SQL est exécutée
# séparément (contrainte asyncpg — cf. migrations 0001/0002).
"""Clients, e-mail (Brevo), newsletter, consentement, RGPD — PR3

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
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
    # ------------------------------------------------------------------
    # clients — fiche minimale, une ligne par e-mail (minuscules, trim :
    # normalisation applicative dans `services/client_service.py`).
    # ------------------------------------------------------------------
    op.create_table(
        "clients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column(
            "newsletter_optin", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("brevo_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("brevo_last_error", sa.Text(), nullable=True),
        # RGPD (E4) — posé par l'anonymisation, jamais par une suppression
        # de ligne : NULL = fiche active.
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.UniqueConstraint("email", name="uq_clients_email"),
    )
    op.create_index("ix_clients_email", "clients", ["email"])

    # ------------------------------------------------------------------
    # consents — registre append-only (E5). L'état courant d'un purpose
    # est la ligne la plus récente pour (client_id, purpose) — voir
    # `services/client_service.py::record_consent`.
    # ------------------------------------------------------------------
    op.create_table(
        "consents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id"),
            nullable=False,
        ),
        # Enum extensible (PR3 n'utilise que "newsletter") — un seul purpose
        # aujourd'hui, d'autres pourront s'ajouter sans migration destructive.
        sa.Column(
            "purpose",
            sa.Enum("newsletter", name="consent_purpose"),
            nullable=False,
        ),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column(
            "source",
            sa.Enum("pos", "webhook", "admin", "rgpd", name="consent_source"),
            nullable=False,
        ),
        sa.Column("policy_version", sa.String(length=16), nullable=False),
        sa.Column(
            "recorded_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index("ix_consents_client_id", "consents", ["client_id"])

    # ------------------------------------------------------------------
    # communications — traçabilité de chaque envoi (E7). Pas de contenu du
    # message : le ticket texte figé vit déjà dans `receipts.content`.
    # ------------------------------------------------------------------
    op.create_table(
        "communications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id"),
            nullable=True,
        ),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id"),
            nullable=True,
        ),
        sa.Column("kind", sa.Enum("receipt", name="communication_kind"), nullable=False),
        sa.Column("channel", sa.Enum("email", name="communication_channel"), nullable=False),
        sa.Column("recipient", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column(
            "provider",
            sa.Enum("brevo", "smtp", "simulated", name="communication_provider"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("sent", "failed", "simulated", name="communication_status"),
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_communications_client_id", "communications", ["client_id"])
    op.create_index("ix_communications_transaction_id", "communications", ["transaction_id"])

    # ------------------------------------------------------------------
    # transactions.client_id (E3) — nullable, hors payload signé
    # (`fiscal.py::_transaction_payload` ne le référence pas : le rattacher
    # après coup ne casse donc jamais `verify_chain_integrity`).
    # ------------------------------------------------------------------
    op.add_column(
        "transactions",
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_transactions_client_id", "transactions", ["client_id"])

    conn = op.get_bind()
    statements = (
        # -- consents : append-only, UPDATE/DELETE interdits (E5) ----------
        """
        CREATE OR REPLACE FUNCTION fripco_protect_consent()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'NF525: registre des consentements append-only (modification/suppression interdite)';
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_protect_consent ON consents",
        """
        CREATE TRIGGER trg_protect_consent
        BEFORE UPDATE OR DELETE ON consents
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_consent()
        """,
        # -- transactions : réécriture de la fonction du trigger (migration
        # 0002) pour ajouter l'exception `client_id` (E3). Le reste de la
        # liste de colonnes protégées est IDENTIQUE à 0002 — `client_id`
        # n'y apparaît intentionnellement pas : c'est cette absence qui
        # autorise son UPDATE sur une ligne déjà signée. `updated_at`
        # reste, comme en 0002, hors de la liste (toujours mutable).
        """
        CREATE OR REPLACE FUNCTION fripco_protect_signed_transaction()
        RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF COALESCE(OLD.hash_chain, '') <> '' THEN
              RAISE EXCEPTION 'NF525: suppression transaction signee interdite';
            END IF;
            RETURN OLD;
          END IF;
          IF COALESCE(OLD.hash_chain, '') <> '' AND (
            OLD.transaction_number IS DISTINCT FROM NEW.transaction_number OR
            OLD.transaction_type IS DISTINCT FROM NEW.transaction_type OR
            OLD.user_id IS DISTINCT FROM NEW.user_id OR
            OLD.client_uuid IS DISTINCT FROM NEW.client_uuid OR
            OLD.original_transaction_id IS DISTINCT FROM NEW.original_transaction_id OR
            OLD.refund_reason IS DISTINCT FROM NEW.refund_reason OR
            OLD.discount_type IS DISTINCT FROM NEW.discount_type OR
            OLD.discount_value IS DISTINCT FROM NEW.discount_value OR
            OLD.discount_amount IS DISTINCT FROM NEW.discount_amount OR
            OLD.tva_rate IS DISTINCT FROM NEW.tva_rate OR
            OLD.total_ht IS DISTINCT FROM NEW.total_ht OR
            OLD.total_tva IS DISTINCT FROM NEW.total_tva OR
            OLD.total_ttc IS DISTINCT FROM NEW.total_ttc OR
            OLD.created_at IS DISTINCT FROM NEW.created_at OR
            OLD.previous_hash IS DISTINCT FROM NEW.previous_hash OR
            OLD.fiscal_signature_version IS DISTINCT FROM NEW.fiscal_signature_version OR
            OLD.receipt_number IS DISTINCT FROM NEW.receipt_number OR
            OLD.hash_chain IS DISTINCT FROM NEW.hash_chain
            -- client_id VOLONTAIREMENT ABSENT (E3/PR3 §2) : seule colonne
            -- mutable hors hash sur une transaction déjà signée.
          ) THEN
            RAISE EXCEPTION 'NF525: modification transaction signee interdite';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        # Le trigger existant pointe déjà vers cette fonction (même nom) —
        # CREATE OR REPLACE suffit, pas besoin de DROP/CREATE TRIGGER.
    )
    for statement in statements:
        conn.execute(sa.text(statement))


def downgrade() -> None:
    # Migration fiscale irréversible (cf. 0001/0002) — la reprise passe par
    # la restauration d'une sauvegarde antérieure.
    raise NotImplementedError("Migration fiscale irreversible")
