# Extrait de l'application source (structure inspiree de apps/api/alembic/versions/0072_security_loyalty_nf525.py)
"""Revision initiale — users + journal_events (JET) + trigger d'immuabilite

Revision ID: 0001
Revises:
Create Date: 2026-09-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "journal_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("username", sa.String(length=150), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("previous_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column(
            "signature_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.UniqueConstraint("seq", name="uq_journal_events_seq"),
    )
    op.create_index("ix_journal_events_seq", "journal_events", ["seq"])
    op.create_index("ix_journal_events_event_type", "journal_events", ["event_type"])
    op.create_index("ix_journal_events_created_at", "journal_events", ["created_at"])

    # Trigger d'immuabilite : toute UPDATE ou DELETE sur journal_events est
    # rejetee (pilier "securisation" de l'auto-attestation NF525). asyncpg
    # prepare un statement a la fois et refuse les chaines contenant
    # plusieurs commandes top-level ; le corps de la fonction peut contenir
    # des points-virgules, mais chaque CREATE/DROP est execute separement
    # (meme contrainte que le module équivalent de l'application source
    # alembic/versions/0072_security_loyalty_nf525.py:225-230).
    conn = op.get_bind()
    trigger_statements = (
        """
        CREATE OR REPLACE FUNCTION fripco_protect_journal_event()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'JET: journal des evenements immuable';
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_protect_journal_event ON journal_events",
        """
        CREATE TRIGGER trg_protect_journal_event
        BEFORE UPDATE OR DELETE ON journal_events
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_journal_event()
        """,
    )
    for statement in trigger_statements:
        conn.execute(sa.text(statement))


def downgrade() -> None:
    # Cette revision cree la chaine de preuve fiscale (JET). Supprimer son
    # trigger d'immuabilite violerait la garantie meme qu'elle introduit. La
    # reprise doit passer par la restauration d'une sauvegarde anterieure,
    # jamais par un downgrade Alembic en place.
    raise NotImplementedError("Migration fiscale irreversible")
