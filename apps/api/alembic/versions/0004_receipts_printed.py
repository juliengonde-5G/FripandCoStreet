# Extrait de l'application source (structure de migration inspiree des
# migrations 0002/0003) — decision d'integration (Julien, contraire au CDC
# initial) : la caisse imprime les tickets avec le meme materiel que
# l'application source (MUNBYN 047P ESC/POS reseau/WebUSB, tiroir Safescan
# SD-4141). Ajoute le compteur d'impression PHYSIQUE des tickets,
# distinct de `duplicate_count` (compteur de LECTURE du texte via
# `GET /pos/transactions/{id}/receipt`, deja existant depuis la migration
# 0002 — non modifie ici). Chaque commande SQL est executee separement
# (contrainte asyncpg — cf. migrations 0001/0002/0003).
"""Compteur d'impression physique des tickets (MUNBYN ESC/POS) — printed_count

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "receipts",
        sa.Column("printed_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "receipts",
        sa.Column("printed_at", sa.DateTime(timezone=True), nullable=True),
    )

    conn = op.get_bind()
    # Reecriture (CREATE OR REPLACE) de la fonction du trigger d'immuabilite
    # des tickets (migration 0002, `fripco_protect_receipt`) — logique
    # INCHANGEE (liste de refus sur `content`/`transaction_id`/`created_at`),
    # desormais commentee explicitement pour couvrir `printed_count` et
    # `printed_at`, en plus de `duplicate_count`, comme colonnes mutables
    # apres signature d'un ticket. Le trigger existant pointe deja vers
    # cette fonction (meme nom) — CREATE OR REPLACE suffit, pas besoin de
    # DROP/CREATE TRIGGER.
    conn.execute(
        sa.text(
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
                RAISE EXCEPTION 'NF525: contenu du ticket immuable (seuls duplicate_count, printed_count et printed_at sont modifiables)';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )


def downgrade() -> None:
    # Migration fiscale irreversible (cf. 0001/0002/0003) — la reprise passe
    # par la restauration d'une sauvegarde anterieure.
    raise NotImplementedError("Migration fiscale irreversible")
