# Nouvelle migration (PR4, docs/ARCHITECTURE_PR4.md §2) — ecritures comptables
# par cloture Z (`accounting_exports`/`accounting_export_lines`, F2/F3) et
# clotures periodiques scellees (`fiscal_closures`, F5). Chaque commande SQL
# est executee separement (contrainte asyncpg — cf. migrations 0001-0004).
"""Ecritures comptables + clotures fiscales periodiques — PR4

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
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
    # accounting_exports — une ecriture comptable par cloture Z (F2).
    # ------------------------------------------------------------------
    op.create_table(
        "accounting_exports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column(
            "z_report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("z_reports.id"),
            nullable=False,
        ),
        sa.Column("export_date", sa.Date(), nullable=False),
        sa.Column("total_sales_ht", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total_tva", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total_ttc", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total_refunds_ttc", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total_cash", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total_card", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total_debit", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total_credit", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("rounding_adjustment", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("fec_content", sa.Text(), nullable=True),
        sa.UniqueConstraint("z_report_id", name="uq_accounting_exports_z_report_id"),
    )
    op.create_index("ix_accounting_exports_export_date", "accounting_exports", ["export_date"])

    # ------------------------------------------------------------------
    # accounting_export_lines — lignes d'ecriture (journal des ventes, F2).
    # ------------------------------------------------------------------
    op.create_table(
        "accounting_export_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column(
            "export_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounting_exports.id"),
            nullable=False,
        ),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("account_number", sa.String(length=20), nullable=False),
        sa.Column("account_label", sa.String(length=100), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("debit", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("credit", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("piece_reference", sa.String(length=32), nullable=False),
    )
    op.create_index(
        "ix_accounting_export_lines_export_id", "accounting_export_lines", ["export_id"]
    )

    # ------------------------------------------------------------------
    # fiscal_closures — clotures periodiques scellees (F5).
    # ------------------------------------------------------------------
    op.create_table(
        "fiscal_closures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column(
            "closure_type",
            sa.Enum("monthly", "annual", "manual", name="fiscal_closure_type"),
            nullable=False,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("software_version", sa.String(length=32), nullable=False),
        sa.Column("fiscal_version_date", sa.String(length=16), nullable=False),
        sa.Column("transaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_transaction_number", sa.Integer(), nullable=True),
        sa.Column("last_transaction_number", sa.Integer(), nullable=True),
        sa.Column("last_transaction_hash", sa.String(length=64), nullable=True),
        sa.Column("first_z_number", sa.Integer(), nullable=True),
        sa.Column("last_z_number", sa.Integer(), nullable=True),
        sa.Column("last_z_hash", sa.String(length=64), nullable=True),
        sa.Column("jet_last_seq", sa.Integer(), nullable=True),
        sa.Column("jet_last_hash", sa.String(length=64), nullable=True),
        sa.Column("grand_total_sales", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("grand_total_refunds", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("grand_total_net", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("perpetual_sales", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("perpetual_refunds", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("perpetual_net", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column(
            "perpetual_transaction_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("archive_content", sa.LargeBinary(), nullable=False),
        sa.Column("archive_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "manifest", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("previous_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("signature_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "closed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.UniqueConstraint("sequence_number", name="uq_fiscal_closures_sequence_number"),
    )
    op.create_index(
        "ix_fiscal_closures_closure_type", "fiscal_closures", ["closure_type"]
    )
    op.create_index(
        "ix_fiscal_closures_period_start", "fiscal_closures", ["period_start"]
    )

    # ------------------------------------------------------------------
    # Triggers d'immuabilite (NF525) — un CREATE/DROP par execute() (meme
    # contrainte asyncpg que les migrations precedentes).
    # ------------------------------------------------------------------
    conn = op.get_bind()
    trigger_statements = (
        # -- accounting_exports : figee des la creation --------------------
        """
        CREATE OR REPLACE FUNCTION fripco_protect_accounting_export()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'NF525: ecriture comptable immuable';
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_protect_accounting_export ON accounting_exports",
        """
        CREATE TRIGGER trg_protect_accounting_export
        BEFORE UPDATE OR DELETE ON accounting_exports
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_accounting_export()
        """,
        # -- accounting_export_lines : figees des la creation ---------------
        """
        CREATE OR REPLACE FUNCTION fripco_protect_accounting_export_line()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'NF525: ligne d ecriture comptable immuable';
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_protect_accounting_export_line ON accounting_export_lines",
        """
        CREATE TRIGGER trg_protect_accounting_export_line
        BEFORE UPDATE OR DELETE ON accounting_export_lines
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_accounting_export_line()
        """,
        # -- fiscal_closures : figee des la creation -------------------------
        """
        CREATE OR REPLACE FUNCTION fripco_protect_fiscal_closure()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'NF525: cloture fiscale immuable';
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_protect_fiscal_closure ON fiscal_closures",
        """
        CREATE TRIGGER trg_protect_fiscal_closure
        BEFORE UPDATE OR DELETE ON fiscal_closures
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_fiscal_closure()
        """,
    )
    for statement in trigger_statements:
        conn.execute(sa.text(statement))


def downgrade() -> None:
    # Migration fiscale irreversible (cf. 0001-0004) — la reprise passe par
    # la restauration d'une sauvegarde anterieure.
    raise NotImplementedError("Migration fiscale irreversible")
