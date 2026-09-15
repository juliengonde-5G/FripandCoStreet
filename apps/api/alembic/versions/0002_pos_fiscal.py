# Extrait de Vintiz (structure des triggers d'immuabilite inspiree de
# apps/api/alembic/versions/0072_security_loyalty_nf525.py L82-223) — noms de
# fonctions/tables adaptes au schema reduit du contrat PR2
# (docs/ARCHITECTURE_PR2.md §2). Chaque commande SQL est executee separement
# (asyncpg prepare un statement a la fois et refuse les chaines contenant
# plusieurs commandes top-level — meme contrainte que la migration 0001).
"""Vente, especes, tickets, Z — tables fiscales PR2

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
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
    # app_settings — parametrage boutique (D13). Cle primaire naturelle
    # (`key`), pas de colonne `id` de surcharge.
    # ------------------------------------------------------------------
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        *_timestamps(),
        sa.Column("value", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "updated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )

    # ------------------------------------------------------------------
    # cash_drawers — cree AVANT z_reports (dependance croisee : z_reports
    # reference cash_drawers.id, cash_drawers.z_report_id reference
    # z_reports.id). La FK sur z_report_id est ajoutee par ALTER TABLE une
    # fois z_reports cree, plus bas.
    # ------------------------------------------------------------------
    op.create_table(
        "cash_drawers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opening_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("closing_amount", sa.Numeric(10, 2), nullable=True),
        sa.Column("expected_amount", sa.Numeric(10, 2), nullable=True),
        sa.Column("discrepancy", sa.Numeric(10, 2), nullable=True),
        sa.Column("opening_breakdown", postgresql.JSONB(), nullable=True),
        sa.Column("closing_breakdown", postgresql.JSONB(), nullable=True),
        sa.Column("closing_note", sa.Text(), nullable=True),
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("closed_by_guard", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("z_report_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_cash_drawers_is_open", "cash_drawers", ["is_open"])

    # ------------------------------------------------------------------
    # transactions
    # ------------------------------------------------------------------
    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column("transaction_number", sa.Integer(), nullable=False),
        sa.Column(
            "transaction_type",
            sa.Enum("sale", "refund", name="transaction_type"),
            nullable=False,
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("client_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "original_transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id"),
            nullable=True,
        ),
        sa.Column("refund_reason", sa.Text(), nullable=True),
        sa.Column(
            "discount_type",
            sa.Enum("percent", "amount", name="discount_type"),
            nullable=True,
        ),
        sa.Column("discount_value", sa.Numeric(10, 2), nullable=True),
        sa.Column("discount_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("tva_rate", sa.Numeric(4, 2), nullable=False),
        sa.Column("total_ht", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("total_tva", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("total_ttc", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("hash_chain", sa.String(length=64), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "fiscal_signature_version", sa.Integer(), nullable=False, server_default="3"
        ),
        sa.Column("receipt_number", sa.Integer(), nullable=True),
        sa.UniqueConstraint("transaction_number", name="uq_transactions_transaction_number"),
        sa.UniqueConstraint("client_uuid", name="uq_transactions_client_uuid"),
        sa.UniqueConstraint("receipt_number", name="uq_transactions_receipt_number"),
    )
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"])
    op.create_index("ix_transactions_transaction_type", "transactions", ["transaction_type"])
    op.create_index(
        "ix_transactions_original_transaction_id", "transactions", ["original_transaction_id"]
    )

    # ------------------------------------------------------------------
    # transaction_items
    # ------------------------------------------------------------------
    op.create_table(
        "transaction_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=255), nullable=False, server_default="Article"),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("discount_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("line_total", sa.Numeric(10, 2), nullable=False),
        sa.Column("tva_rate", sa.Numeric(4, 2), nullable=False),
        sa.Column("line_ht", sa.Numeric(10, 2), nullable=False),
        sa.Column("line_tva", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "original_transaction_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transaction_items.id"),
            nullable=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_transaction_items_transaction_id", "transaction_items", ["transaction_id"])

    # ------------------------------------------------------------------
    # payments
    # ------------------------------------------------------------------
    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id"),
            nullable=False,
        ),
        sa.Column("method", sa.Enum("cash", "card", name="payment_method"), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("tendered_amount", sa.Numeric(10, 2), nullable=True),
        sa.Column("change_amount", sa.Numeric(10, 2), nullable=True),
        sa.Column("sumup_checkout_id", sa.String(length=64), nullable=True),
        sa.Column("sumup_transaction_id", sa.String(length=64), nullable=True),
        sa.Column("sumup_transaction_code", sa.String(length=32), nullable=True),
        sa.Column("sumup_auth_code", sa.String(length=16), nullable=True),
        sa.Column("sumup_card_brand", sa.String(length=32), nullable=True),
        sa.Column("sumup_card_last4", sa.String(length=4), nullable=True),
        sa.Column("sumup_refunded_amount", sa.Numeric(10, 2), nullable=True),
    )
    op.create_index("ix_payments_transaction_id", "payments", ["transaction_id"])

    # ------------------------------------------------------------------
    # z_reports — ferme la dependance croisee avec cash_drawers.
    # ------------------------------------------------------------------
    op.create_table(
        "z_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column("report_number", sa.Integer(), nullable=False),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "cash_drawer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cash_drawers.id"),
            nullable=False,
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_sales", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("total_refunds", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("total_net", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("total_ht", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("total_tva", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("transaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_transaction_number", sa.Integer(), nullable=True),
        sa.Column("last_transaction_number", sa.Integer(), nullable=True),
        sa.Column("last_transaction_hash", sa.String(length=64), nullable=False),
        sa.Column("payment_totals", postgresql.JSONB(), nullable=True),
        sa.Column("opening_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("closing_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("expected_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("discrepancy", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("cash_in_total", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("cash_out_total", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("cash_movement_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("counted", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_regularization", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("regularization_reason", sa.Text(), nullable=True),
        sa.Column("cumulative_sales", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("cumulative_refunds", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("cumulative_net", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("cumulative_transaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=False),
        sa.Column("fiscal_signature_version", sa.Integer(), nullable=False, server_default="3"),
        sa.UniqueConstraint("report_number", name="uq_z_reports_report_number"),
        sa.UniqueConstraint("cash_drawer_id", name="uq_z_reports_cash_drawer_id"),
    )

    op.create_foreign_key(
        "fk_cash_drawers_z_report_id", "cash_drawers", "z_reports", ["z_report_id"], ["id"]
    )

    # ------------------------------------------------------------------
    # payment_attempts (SumUp) — hors hash, mutable.
    # ------------------------------------------------------------------
    op.create_table(
        "payment_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column("client_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "paid", "failed", "cancelled", name="payment_attempt_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("checkout_id", sa.String(length=100), nullable=False),
        sa.Column("client_transaction_id", sa.String(length=120), nullable=True),
        sa.Column("reader_id", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("sumup_transaction_id", sa.String(length=64), nullable=True),
        sa.Column("sumup_transaction_code", sa.String(length=32), nullable=True),
        sa.Column("sumup_auth_code", sa.String(length=16), nullable=True),
        sa.Column("sumup_card_brand", sa.String(length=32), nullable=True),
        sa.Column("sumup_card_last4", sa.String(length=4), nullable=True),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("checkout_id", name="uq_payment_attempts_checkout_id"),
    )
    op.create_index("ix_payment_attempts_client_uuid", "payment_attempts", ["client_uuid"])
    op.create_index("ix_payment_attempts_status", "payment_attempts", ["status"])

    # ------------------------------------------------------------------
    # receipts
    # ------------------------------------------------------------------
    op.create_table(
        "receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("transaction_id", name="uq_receipts_transaction_id"),
    )

    # ------------------------------------------------------------------
    # cash_movements
    # ------------------------------------------------------------------
    op.create_table(
        "cash_movements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column(
            "drawer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cash_drawers.id"),
            nullable=False,
        ),
        sa.Column(
            "direction",
            sa.Enum("in", "out", name="cash_movement_direction"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "reason",
            sa.Enum(
                "bank_deposit", "supplier_payment", "float_top_up", "other",
                name="cash_movement_reason",
            ),
            nullable=False,
            server_default="other",
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
    )
    op.create_index("ix_cash_movements_drawer_id", "cash_movements", ["drawer_id"])

    # ------------------------------------------------------------------
    # Triggers d'immuabilite fiscale (NF525) — un CREATE/DROP par execute()
    # (contrainte asyncpg, cf. migration 0001).
    # ------------------------------------------------------------------
    conn = op.get_bind()
    trigger_statements = (
        # -- transactions : figee des que hash_chain != '' -----------------
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
          ) THEN
            RAISE EXCEPTION 'NF525: modification transaction signee interdite';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_protect_signed_transaction ON transactions",
        """
        CREATE TRIGGER trg_protect_signed_transaction
        BEFORE UPDATE OR DELETE ON transactions
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_signed_transaction()
        """,
        # -- transaction_items / payments : figees des que la transaction
        # parente est signee (hash_chain != ''). Insertion normale pendant la
        # fenetre ou la transaction parente est encore hash_chain='' (avant
        # signature), cf. services/fiscal.py::sign_transaction.
        """
        CREATE OR REPLACE FUNCTION fripco_protect_fiscal_child()
        RETURNS trigger AS $$
        DECLARE tx_id UUID; tx_hash VARCHAR(64);
        BEGIN
          tx_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.transaction_id ELSE NEW.transaction_id END;
          SELECT hash_chain INTO tx_hash FROM transactions WHERE id = tx_id;
          IF COALESCE(tx_hash, '') <> '' THEN
            RAISE EXCEPTION 'NF525: lignes/paiements d une transaction signee immuables';
          END IF;
          RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_protect_transaction_items ON transaction_items",
        """
        CREATE TRIGGER trg_protect_transaction_items
        BEFORE INSERT OR UPDATE OR DELETE ON transaction_items
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_fiscal_child()
        """,
        "DROP TRIGGER IF EXISTS trg_protect_payments ON payments",
        """
        CREATE TRIGGER trg_protect_payments
        BEFORE INSERT OR UPDATE OR DELETE ON payments
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_fiscal_child()
        """,
        # -- z_reports : scelle a la creation, sans exception (D9) ---------
        """
        CREATE OR REPLACE FUNCTION fripco_protect_z_report()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'NF525: rapport Z immuable';
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_protect_z_report ON z_reports",
        """
        CREATE TRIGGER trg_protect_z_report
        BEFORE UPDATE OR DELETE ON z_reports
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_z_report()
        """,
        # -- cash_drawers : figes une fois clotures (closed_at + z_report_id)
        """
        CREATE OR REPLACE FUNCTION fripco_protect_cash_drawer()
        RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'NF525: suppression tiroir de caisse interdite';
          END IF;
          IF OLD.closed_at IS NOT NULL AND OLD.z_report_id IS NOT NULL THEN
            RAISE EXCEPTION 'NF525: tiroir de caisse cloture immuable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_protect_cash_drawer ON cash_drawers",
        """
        CREATE TRIGGER trg_protect_cash_drawer
        BEFORE UPDATE OR DELETE ON cash_drawers
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_cash_drawer()
        """,
        # -- cash_movements : append-only ----------------------------------
        """
        CREATE OR REPLACE FUNCTION fripco_protect_cash_movement()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'NF525: mouvement de caisse immuable';
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_protect_cash_movement ON cash_movements",
        """
        CREATE TRIGGER trg_protect_cash_movement
        BEFORE UPDATE OR DELETE ON cash_movements
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_cash_movement()
        """,
        # -- receipts : DELETE interdit, UPDATE limite a duplicate_count ---
        """
        CREATE OR REPLACE FUNCTION fripco_protect_receipt()
        RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'NF525: suppression ticket interdite';
          END IF;
          IF OLD.transaction_id IS DISTINCT FROM NEW.transaction_id OR
             OLD.content IS DISTINCT FROM NEW.content OR
             OLD.created_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION 'NF525: contenu du ticket immuable (seul duplicate_count est modifiable)';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_protect_receipt ON receipts",
        """
        CREATE TRIGGER trg_protect_receipt
        BEFORE UPDATE OR DELETE ON receipts
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_receipt()
        """,
    )
    for statement in trigger_statements:
        conn.execute(sa.text(statement))


def downgrade() -> None:
    # Migration fiscale irreversible (chaine de preuve vente/Z, cf. 0001) —
    # la reprise passe par la restauration d'une sauvegarde anterieure.
    raise NotImplementedError("Migration fiscale irreversible")
