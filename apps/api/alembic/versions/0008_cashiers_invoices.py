# Nouvelle migration (PR8, docs/ARCHITECTURE_PR8.md, contrats J1 et J5) —
# vendeuses identifiees par code PIN (`cashiers` + colonnes `cashier_id`) et
# factures B2B numerotees (`invoices`). Chaque commande SQL est executee
# separement (contrainte asyncpg — cf. migrations 0001-0007).
#
# Deux tables de nature differente dans UNE seule migration (une PR = une
# revision) :
#   - `cashiers` est une table d'EXPLOITATION (identites de caisse, pas des
#     comptes utilisateurs) : aucun trigger d'immuabilite, mais une vendeuse
#     ne se supprime jamais (desactivation seulement), parce que les ventes
#     la referencent.
#   - `invoices` est une table FISCALE : numerotation sequentielle par annee,
#     immuable une fois emise (trigger `fripco_protect_invoice` : DELETE
#     refuse, un seul UPDATE tolere, celui qui pose `pdf_sha256` depuis NULL).
#
# `cashier_id` est HORS SIGNATURE (comme `client_id`, cf. 0003) : il
# n'apparait nulle part dans `services/fiscal.py::_transaction_payload`, donc
# le `hash_chain` d'une vente est rigoureusement le meme avec ou sans
# vendeuse. Mais, contrairement a `client_id`, il est POSE A L'INSERT et
# GELE : les fonctions de protection de `transactions` et `cash_movements`
# sont reecrites ici pour l'inclure dans les colonnes figees.
#
# Migration REVERSIBLE (comme 0006/0007, contrairement aux migrations
# fiscales 0001-0005) : le downgrade restaure les fonctions de protection
# dans leur version 0003/0002 AVANT de retirer les colonnes qu'elles
# referencent, puis supprime les tables introduites ici.
"""Vendeuses par code PIN et factures B2B — PR8

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
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


# ---------------------------------------------------------------------------
# Fonctions de protection — versions AVANT (0002/0003) et APRES (0008).
# Ecrites ici en toutes lettres plutot que generees : une fonction de trigger
# fiscal doit se relire integralement dans la migration qui la pose (aucune
# construction dynamique, aucune interpolation).
# ---------------------------------------------------------------------------

_PROTECT_SIGNED_TRANSACTION_0003 = """
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
    -- client_id VOLONTAIREMENT ABSENT (E3/PR3) : seule colonne mutable hors
    -- hash sur une transaction deja signee.
  ) THEN
    RAISE EXCEPTION 'NF525: modification transaction signee interdite';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

# Identique a la version 0003, + `cashier_id` dans la liste des colonnes
# gelees (J1). La vendeuse qui a encaisse est une donnee d'exploitation
# (hors signature), mais elle est posee a l'INSERT et ne doit plus jamais
# changer : une vente ne peut pas etre reattribuee a une autre vendeuse
# apres coup. C'est exactement l'inverse du traitement de `client_id`.
_PROTECT_SIGNED_TRANSACTION_0008 = """
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
    OLD.cashier_id IS DISTINCT FROM NEW.cashier_id OR
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
    -- client_id VOLONTAIREMENT ABSENT (E3/PR3) : seule colonne mutable hors
    -- hash sur une transaction deja signee. `cashier_id` (PR8/J1), lui, est
    -- gele ci-dessus.
  ) THEN
    RAISE EXCEPTION 'NF525: modification transaction signee interdite';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_PROTECT_CASH_MOVEMENT_0002 = """
CREATE OR REPLACE FUNCTION fripco_protect_cash_movement()
RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'NF525: mouvement de caisse immuable';
END;
$$ LANGUAGE plpgsql
"""

# `cash_movements` est append-only depuis 0002 : la fonction refuse TOUT
# UPDATE et TOUT DELETE, `cashier_id` (PR8/J1) est donc gele par
# construction — il n'y a pas de liste de colonnes a completer comme sur
# `transactions`. La fonction est malgre tout reecrite ici pour que la
# migration qui introduit la colonne porte, noir sur blanc, la protection
# qui s'y applique (et pour que le downgrade la restaure symetriquement).
_PROTECT_CASH_MOVEMENT_0008 = """
CREATE OR REPLACE FUNCTION fripco_protect_cash_movement()
RETURNS trigger AS $$
BEGIN
  -- Append-only : aucune colonne n'est modifiable, `cashier_id` (PR8/J1)
  -- comme les autres.
  RAISE EXCEPTION 'NF525: mouvement de caisse immuable';
END;
$$ LANGUAGE plpgsql
"""

# Factures/avoirs (J5) : immuables une fois emis. Le SEUL UPDATE tolere est
# celui qui pose `pdf_sha256` alors qu'il valait NULL — l'empreinte du PDF
# est calculee apres coup (premier rendu du document) et scelle la facture
# definitivement. Toute autre modification, et tout DELETE, sont refuses.
_PROTECT_INVOICE_0008 = """
CREATE OR REPLACE FUNCTION fripco_protect_invoice()
RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'NF525: suppression facture interdite';
  END IF;
  IF OLD.pdf_sha256 IS NOT NULL OR NEW.pdf_sha256 IS NULL THEN
    RAISE EXCEPTION 'NF525: facture immuable (seul pdf_sha256 peut etre pose une fois)';
  END IF;
  IF OLD.id IS DISTINCT FROM NEW.id OR
     OLD.created_at IS DISTINCT FROM NEW.created_at OR
     OLD.transaction_id IS DISTINCT FROM NEW.transaction_id OR
     OLD.kind IS DISTINCT FROM NEW.kind OR
     OLD.invoice_number IS DISTINCT FROM NEW.invoice_number OR
     OLD.original_invoice_id IS DISTINCT FROM NEW.original_invoice_id OR
     OLD.company_name IS DISTINCT FROM NEW.company_name OR
     OLD.siret IS DISTINCT FROM NEW.siret OR
     OLD.vat_number IS DISTINCT FROM NEW.vat_number OR
     OLD.address_line1 IS DISTINCT FROM NEW.address_line1 OR
     OLD.address_line2 IS DISTINCT FROM NEW.address_line2 OR
     OLD.postal_code IS DISTINCT FROM NEW.postal_code OR
     OLD.city IS DISTINCT FROM NEW.city OR
     OLD.issued_at IS DISTINCT FROM NEW.issued_at OR
     OLD.user_id IS DISTINCT FROM NEW.user_id THEN
    RAISE EXCEPTION 'NF525: facture immuable (seul pdf_sha256 peut etre pose une fois)';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    conn = op.get_bind()

    # ------------------------------------------------------------------
    # cashiers (J1) — identites de caisse, PAS des comptes utilisateurs :
    # aucune connexion, aucun role, un simple code PIN a 4 chiffres hache
    # en bcrypt (`core/security.hash_password`). Jamais supprimee : les
    # ventes la referencent, on desactive (`active=false` + `deactivated_at`).
    # ------------------------------------------------------------------
    op.create_table(
        "cashiers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column("display_name", sa.String(length=60), nullable=False),
        # NULL = aucun PIN defini (la vendeuse ne peut pas encore
        # s'identifier). Jamais le PIN en clair, evidemment.
        sa.Column("pin_hash", sa.String(length=100), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Unicite INSENSIBLE A LA CASSE : « Lea » et « lea » sont la meme
    # vendeuse (le nom est le seul repere a l'ecran de caisse, deux cartes
    # qui se ressemblent seraient un piege).
    op.create_index(
        "uq_cashiers_display_name_ci",
        "cashiers",
        [sa.text("lower(display_name)")],
        unique=True,
    )

    # ------------------------------------------------------------------
    # cashier_id — nullable partout (historique anterieur a PR8, et reglage
    # `pos.cashier_required` a false par defaut).
    # ------------------------------------------------------------------
    op.add_column(
        "transactions",
        sa.Column(
            "cashier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cashiers.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_transactions_cashier_id", "transactions", ["cashier_id"])

    op.add_column(
        "cash_movements",
        sa.Column(
            "cashier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cashiers.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_cash_movements_cashier_id", "cash_movements", ["cashier_id"])

    # Z : la vendeuse qui a cloture (le Z lui-meme est scelle a la creation,
    # cette colonne est donc posee a l'INSERT, jamais ensuite).
    op.add_column(
        "z_reports",
        sa.Column(
            "cashier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cashiers.id"),
            nullable=True,
        ),
    )

    # Tiroir : qui a ouvert, qui a cloture, et qui tient la caisse en ce
    # moment. `current_cashier_id` est un etat COURANT (mutable tant que le
    # tiroir est ouvert — la releve le change en cours de journee), pas une
    # donnee fiscale ; les deux autres sont poses une fois.
    op.add_column(
        "cash_drawers",
        sa.Column(
            "opened_by_cashier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cashiers.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "cash_drawers",
        sa.Column(
            "closed_by_cashier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cashiers.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "cash_drawers",
        sa.Column(
            "current_cashier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cashiers.id"),
            nullable=True,
        ),
    )

    # ------------------------------------------------------------------
    # invoices (J5) — facture B2B et avoir. Le client professionnel n'est
    # PAS une fiche client (`clients`) : ses coordonnees sont figees dans la
    # facture, hors du perimetre de l'anonymisation RGPD (conservation 10
    # ans). `transaction_id` + `kind` uniques : une vente porte au plus une
    # facture et au plus un avoir.
    # ------------------------------------------------------------------
    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        *_timestamps(),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum("invoice", "credit_note", name="invoice_kind"),
            nullable=False,
            server_default="invoice",
        ),
        # `F-AAAA-NNNN` / `A-AAAA-NNNN` — compteur par annee, attribue sous
        # le verrou fiscal partage avec les ventes (services, PR8/J5).
        sa.Column("invoice_number", sa.String(length=20), nullable=False),
        # Avoir : la facture qu'il annule.
        sa.Column(
            "original_invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id"),
            nullable=True,
        ),
        sa.Column("company_name", sa.String(length=120), nullable=False),
        # 14 chiffres, validation Luhn cote service (jamais en base : une
        # contrainte CHECK figerait l'algorithme dans le schema).
        sa.Column("siret", sa.String(length=14), nullable=False),
        # `FR` + cle 2 caracteres + SIREN 9 chiffres = 13 ; 20 laisse la
        # place a un numero intracommunautaire etranger.
        sa.Column("vat_number", sa.String(length=20), nullable=True),
        sa.Column("address_line1", sa.String(length=120), nullable=False),
        sa.Column("address_line2", sa.String(length=120), nullable=True),
        sa.Column("postal_code", sa.String(length=10), nullable=False),
        sa.Column("city", sa.String(length=80), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        # Empreinte du PDF, posee au premier rendu — seule colonne que le
        # trigger d'immuabilite laisse passer, et une seule fois.
        sa.Column("pdf_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.UniqueConstraint("invoice_number", name="uq_invoices_invoice_number"),
        sa.UniqueConstraint("transaction_id", "kind", name="uq_invoices_transaction_kind"),
    )
    op.create_index("ix_invoices_issued_at", "invoices", ["issued_at"])
    op.create_index("ix_invoices_transaction_id", "invoices", ["transaction_id"])

    # ------------------------------------------------------------------
    # Triggers (un CREATE/DROP par execute() — contrainte asyncpg).
    # ------------------------------------------------------------------
    statements = (
        _PROTECT_SIGNED_TRANSACTION_0008,
        _PROTECT_CASH_MOVEMENT_0008,
        _PROTECT_INVOICE_0008,
        "DROP TRIGGER IF EXISTS trg_protect_invoice ON invoices",
        """
        CREATE TRIGGER trg_protect_invoice
        BEFORE UPDATE OR DELETE ON invoices
        FOR EACH ROW EXECUTE FUNCTION fripco_protect_invoice()
        """,
    )
    for statement in statements:
        conn.execute(sa.text(statement))


def downgrade() -> None:
    conn = op.get_bind()

    # 1. Facture : trigger puis table puis type enum.
    conn.execute(sa.text("DROP TRIGGER IF EXISTS trg_protect_invoice ON invoices"))
    op.drop_index("ix_invoices_transaction_id", table_name="invoices")
    op.drop_index("ix_invoices_issued_at", table_name="invoices")
    op.drop_table("invoices")
    conn.execute(sa.text("DROP FUNCTION IF EXISTS fripco_protect_invoice()"))
    sa.Enum(name="invoice_kind").drop(conn, checkfirst=True)

    # 2. Restaurer les fonctions de protection AVANT de retirer les colonnes
    # qu'elles referencent (`OLD.cashier_id` n'existerait plus).
    for statement in (_PROTECT_SIGNED_TRANSACTION_0003, _PROTECT_CASH_MOVEMENT_0002):
        conn.execute(sa.text(statement))

    # 3. Colonnes (les contraintes de cle etrangere tombent avec elles).
    op.drop_column("cash_drawers", "current_cashier_id")
    op.drop_column("cash_drawers", "closed_by_cashier_id")
    op.drop_column("cash_drawers", "opened_by_cashier_id")
    op.drop_column("z_reports", "cashier_id")
    op.drop_index("ix_cash_movements_cashier_id", table_name="cash_movements")
    op.drop_column("cash_movements", "cashier_id")
    op.drop_index("ix_transactions_cashier_id", table_name="transactions")
    op.drop_column("transactions", "cashier_id")

    # 4. Vendeuses.
    op.drop_index("uq_cashiers_display_name_ci", table_name="cashiers")
    op.drop_table("cashiers")
