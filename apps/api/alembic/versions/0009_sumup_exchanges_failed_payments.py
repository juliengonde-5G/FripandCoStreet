# Nouvelle migration (PR9, docs/ARCHITECTURE_PR9.md, contrats K1 et K3) —
# journal des echanges HTTP avec SumUp (`sumup_exchanges`) et file des
# paiements carte echoues recuperables (`failed_payments`).
#
# Les DEUX tables sont des tables d'EXPLOITATION, hors perimetre fiscal :
#   - aucune vente n'y nait (la regle PR2 reste entiere : la Transaction
#     n'est ecrite qu'une fois le paiement constate `paid`) ;
#   - donc aucun trigger d'immuabilite, aucun chainage HMAC, et
#     `sumup_exchanges` est explicitement PURGEABLE (retention reglable,
#     purge nocturne + bouton admin).
# C'est la difference de nature avec `invoices` (0008), table fiscale
# protegee par trigger : ici on journalise pour DEBOGUER un terminal, pas
# pour prouver une recette.
#
# `sumup_exchanges` ne contient JAMAIS de donnee personnelle (ni nom, ni
# e-mail), jamais la cle API ni l'en-tete Authorization, jamais de PAN : la
# redaction est faite en amont par `redact_sumup_error`, la base n'est que
# le dernier maillon. Le `response_payload` est tronque a 4 Ko pour qu'une
# reponse SumUp anormalement volumineuse ne fasse pas enfler la table.
#
# Migration REVERSIBLE (comme 0006/0007/0008) : le downgrade retire les deux
# tables puis leurs types enum. Rien a restaurer par ailleurs — aucune
# fonction de protection ni colonne existante n'est touchee.
"""Journal des echanges SumUp et file des paiements echoues — PR9

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
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
    # sumup_exchanges (K1) — un enregistrement par appel HTTP sortant vers
    # SumUp, rejeux transport compris (`retry_count` = nombre d'essais
    # avant la reponse finale, 1 quand il n'y a pas eu de rejeu).
    # ------------------------------------------------------------------
    op.create_table(
        "sumup_exchanges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # `updated_at` n'est pas dans le contrat mais vient de `models.Base`
        # (toutes les tables de l'application en heritent) ; une ligne de
        # journal n'est de toute facon jamais mise a jour.
        *_timestamps(),
        # ping_reader, push_to_reader, checkout_status,
        # reader_checkout_status, cancel_checkout, terminate_reader,
        # refund, get_transaction. Volontairement un VARCHAR et pas un
        # enum : le vocabulaire des operations suivra l'API SumUp, et on ne
        # veut pas d'une migration a chaque nouvelle route appelee.
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        # CHEMIN SEUL (jamais la query : elle pourrait porter un secret).
        sa.Column("url_path", sa.String(length=300), nullable=False),
        sa.Column(
            "request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("response_status", sa.Integer(), nullable=True),
        # Redige et tronque a 4 Ko cote service.
        sa.Column(
            "response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_error", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # transport, timeout, http_4xx, http_5xx, decode.
        sa.Column("error_type", sa.String(length=20), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("checkout_id", sa.String(length=100), nullable=True),
        sa.Column("client_transaction_id", sa.String(length=120), nullable=True),
        # Correlation avec la ligne de log serveur de la requete HTTP
        # entrante qui a declenche l'echange.
        sa.Column("request_id", sa.String(length=64), nullable=True),
    )
    # Les trois acces de l'ecran admin : « les echanges de CE paiement »,
    # « les plus recents » et la purge par retention (`created_at`).
    op.create_index("ix_sumup_exchanges_checkout_id", "sumup_exchanges", ["checkout_id"])
    op.create_index("ix_sumup_exchanges_created_at", "sumup_exchanges", ["created_at"])
    op.create_index("ix_sumup_exchanges_operation", "sumup_exchanges", ["operation"])

    # ------------------------------------------------------------------
    # failed_payments (K3) — file des paiements carte echoues pour une
    # cause RECUPERABLE (TPE injoignable, timeout, 5xx). Un refus de carte
    # n'y entre jamais : la cliente change de moyen de paiement, il n'y a
    # rien a rejouer.
    #
    # `attempt_id` est UNIQUE : un essai de paiement (`payment_attempts`)
    # ne peut donner qu'une seule ligne en file, les reessais incrementent
    # `retry_count` sur cette meme ligne plutot que d'en creer d'autres.
    # ------------------------------------------------------------------
    op.create_table(
        "failed_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_attempts.id"),
            nullable=False,
        ),
        # Reference de la vente A VENIR (elle n'existe pas tant que le
        # paiement n'est pas `paid`), reprise de `payment_attempts`.
        sa.Column("client_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "succeeded",
                "exhausted",
                "abandoned",
                name="failed_payment_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        # Memes valeurs que `sumup_exchanges.error_type`, plus `declined`.
        sa.Column("error_type", sa.String(length=20), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        # Vente finalement encaissee apres un reessai reussi.
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id"),
            nullable=True,
        ),
        sa.Column(
            "cashier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cashiers.id"),
            nullable=True,
        ),
        sa.UniqueConstraint("attempt_id", name="uq_failed_payments_attempt_id"),
    )
    # L'ecran admin liste par defaut les `pending`, du plus recent au plus
    # ancien.
    op.create_index("ix_failed_payments_status", "failed_payments", ["status"])
    op.create_index("ix_failed_payments_created_at", "failed_payments", ["created_at"])


def downgrade() -> None:
    conn = op.get_bind()

    op.drop_index("ix_failed_payments_created_at", table_name="failed_payments")
    op.drop_index("ix_failed_payments_status", table_name="failed_payments")
    op.drop_table("failed_payments")
    sa.Enum(name="failed_payment_status").drop(conn, checkfirst=True)

    op.drop_index("ix_sumup_exchanges_operation", table_name="sumup_exchanges")
    op.drop_index("ix_sumup_exchanges_created_at", table_name="sumup_exchanges")
    op.drop_index("ix_sumup_exchanges_checkout_id", table_name="sumup_exchanges")
    op.drop_table("sumup_exchanges")
