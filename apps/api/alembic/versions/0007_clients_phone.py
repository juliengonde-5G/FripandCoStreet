# Nouvelle migration (PR7, docs/ARCHITECTURE_PR7.md, contrat I3) — le client
# en caisse peut n'avoir qu'un telephone : `clients.phone` arrive, et
# `clients.email` devient nullable. L'unicite passe donc d'une contrainte
# UNIQUE classique (qui accepterait plusieurs lignes a NULL, mais surtout
# qui empeche `email IS NULL`) a deux index uniques PARTIELS
# `WHERE ... IS NOT NULL` : deux fiches sans e-mail cohabitent, deux fiches
# avec le meme e-mail restent impossibles. Une contrainte CHECK garantit
# qu'aucune fiche ne peut exister sans au moins un moyen de contact.
#
# Table d'EXPLOITATION (donnees personnelles, pas fiscales) : aucun trigger
# d'immuabilite n'est touche ici. Les protections existantes restent en
# place telles quelles :
#   - `trg_protect_consent` sur `consents` (registre append-only, 0003) ;
#   - `trg_protect_signed_transaction` sur `transactions` (0002 reecrit par
#     0003 pour exempter `client_id`) — `clients.phone` n'entre ni dans le
#     payload signe (`services/fiscal.py::_transaction_payload`) ni dans la
#     liste de colonnes protegees.
# Cette migration est donc REVERSIBLE (comme 0006, contrairement aux
# migrations fiscales 0001-0005). Chaque commande SQL est executee
# separement (contrainte asyncpg — cf. migrations 0001-0006).
"""Telephone client + e-mail facultatif — PR7

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Telephone normalise cote application (chiffres + `+` initial
    # conserve) — `services/client_service.py::normalize_phone`.
    op.add_column("clients", sa.Column("phone", sa.String(length=32), nullable=True))

    # L'unicite de l'e-mail passe de la contrainte UNIQUE (0003) a un index
    # unique partiel : indispensable puisque la colonne devient nullable.
    op.drop_constraint("uq_clients_email", "clients", type_="unique")
    op.alter_column("clients", "email", existing_type=sa.String(length=255), nullable=True)
    op.create_index(
        "uq_clients_email_present",
        "clients",
        ["email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    op.create_index(
        "uq_clients_phone_present",
        "clients",
        ["phone"],
        unique=True,
        postgresql_where=sa.text("phone IS NOT NULL"),
    )
    # Aucune fiche sans moyen de contact (I3) — le garde-fou vit en base,
    # pas seulement dans `create_or_get` (erreur metier `contact_required`).
    op.create_check_constraint(
        "ck_clients_contact_required",
        "clients",
        "email IS NOT NULL OR phone IS NOT NULL",
    )
    # `ix_clients_email` (index simple, 0003) est conserve tel quel : il
    # sert les recherches admin, l'index unique partiel ne le remplace pas
    # pour les lignes a NULL.


def downgrade() -> None:
    # Reversible (table d'exploitation) — a une condition : l'etat
    # d'arrivee (0006) exige `email NOT NULL UNIQUE`. Des fiches creees en
    # caisse avec le seul telephone ne peuvent donc pas etre retro-portees
    # sans perte de donnees ; on refuse explicitement plutot que de les
    # effacer ou d'inventer une adresse.
    conn = op.get_bind()
    orphans = conn.execute(
        sa.text("SELECT count(*) FROM clients WHERE email IS NULL")
    ).scalar_one()
    if orphans:
        raise RuntimeError(
            f"Downgrade 0007 -> 0006 impossible : {orphans} fiche(s) client sans e-mail "
            "(creees en caisse avec le seul telephone). La revision 0006 impose "
            "`clients.email NOT NULL` : renseignez une adresse ou anonymisez ces fiches "
            "avant de redescendre."
        )

    op.drop_constraint("ck_clients_contact_required", "clients", type_="check")
    op.drop_index("uq_clients_phone_present", table_name="clients")
    op.drop_index("uq_clients_email_present", table_name="clients")
    op.alter_column("clients", "email", existing_type=sa.String(length=255), nullable=False)
    op.create_unique_constraint("uq_clients_email", "clients", ["email"])
    op.drop_column("clients", "phone")
