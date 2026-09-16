#!/usr/bin/env python3
# Nouveau script (PR12, docs/ARCHITECTURE_PR12.md, contrat N3) — remise a
# zero PRE-OUVERTURE de la caisse.
#
# Pourquoi ce script existe : la boutique est saisie et essayee pendant des
# semaines avant d'ouvrir (ventes d'essai, tickets, Z, clientes fictives).
# Le jour J, rien de tout cela ne doit subsister : la premiere vente reelle
# doit porter le numero 1, le premier Z le numero 1, et le journal des
# evenements techniques doit repartir de son genesis. C'est une operation
# D'AVANT-OUVERTURE, jamais un outil d'exploitation : elle pose un verrou
# (`system.go_live_done_at`) qui la rend refusee au second lancement.
#
# Trois principes non negociables :
#
# 1. Les listes de tables sont EXPLICITES (jamais « toutes les tables sauf »)
#    et verifiees contre `information_schema` : une table presente en base
#    mais absente des deux listes fait echouer le script AVANT toute
#    ecriture. Une future migration ne pourra donc jamais voir sa table
#    videe — ou conservee — par accident.
# 2. Les triggers d'immuabilite ne sont JAMAIS desactives. `TRUNCATE` ne
#    declenche pas les triggers de ligne (BEFORE INSERT/UPDATE/DELETE
#    FOR EACH ROW) : on vide sans jamais toucher aux protections, qui
#    restent opposables a l'application avant comme apres.
# 3. Tout se fait dans UNE transaction : la moindre erreur annule tout
#    (ROLLBACK), on ne laisse pas la base a moitie videe.
#
# Usage (dans le conteneur API) :
#   docker exec -it fripco-api python scripts/go_live_reset.py --dry-run
#   docker exec -it fripco-api python scripts/go_live_reset.py --confirm
#   docker exec -it fripco-api python scripts/go_live_reset.py --confirm --force
#
# La connexion passe par MIGRATION_DATABASE_URL (role PROPRIETAIRE) : le role
# applicatif (`DATABASE_URL`) n'est pas proprietaire des tables et ne peut
# donc pas les tronquer — c'est voulu (cf. docs/DEPLOIEMENT.md §4).
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.services.database_backup import BackupError, run_backup
from app.services.jet import (
    EVENT_SYSTEM_GO_LIVE_RESET,
    GENESIS_HASH,
    JournalService,
)
from app.services.settings_service import SettingsService

# ---------------------------------------------------------------------------
# Classement EXPLICITE des tables (contrat N3)
# ---------------------------------------------------------------------------

# CONSERVEES — le parametrage et les comptes survivent a la remise a zero :
#   users            le compte manager unique (on ne veut pas le recreer le
#                    matin de l'ouverture, cf. scripts/create_manager.py) ;
#   app_settings     coordonnees boutique, TVA, mentions du ticket, materiel,
#                    plan de comptes, objectifs, sauvegarde... tout le
#                    parametrage patiemment saisi avant l'ouverture ;
#   cashiers         les vendeuses et leurs codes PIN ;
#   database_backups les lignes de sauvegarde ET leurs fichiers — y compris
#                    celui produit juste avant la remise a zero, qui est la
#                    SEULE trace de ce qui existait avant. Le vider reviendrait
#                    a effacer son propre filet.
KEPT_TABLES: tuple[str, ...] = (
    "app_settings",
    "cashiers",
    "database_backups",
    "users",
)

# VIDEES — tout ce qui est ne d'une vente d'essai ou d'une cliente d'essai.
# `TRUNCATE ... RESTART IDENTITY CASCADE`, en une seule commande, pour que
# les cles etrangeres entre ces tables ne s'opposent pas a l'ordre choisi.
# Les tables filles (`transaction_items`, `payments`, `accounting_export_lines`
# ...) tomberaient de toute facon par CASCADE : elles sont nommees quand meme,
# parce qu'une liste implicite est exactement ce que ce script refuse.
EMPTIED_TABLES: tuple[str, ...] = (
    # Chaine fiscale : ventes, lignes, encaissements, tickets.
    "transactions",
    "transaction_items",
    "payments",
    "receipts",
    # Terminal de paiement : tentatives, file des echecs, journal de debogage.
    "payment_attempts",
    "failed_payments",
    "sumup_exchanges",
    # Caisse especes : tiroirs, mouvements, rapports Z.
    "cash_drawers",
    "cash_movements",
    "z_reports",
    # Comptabilite et clotures periodiques (archives d'essai comprises).
    "accounting_exports",
    "accounting_export_lines",
    "fiscal_closures",
    # Factures et avoirs professionnels d'essai (numerotation F-/A- repart a 1).
    "invoices",
    # Journal des evenements techniques : la chaine repart sur son genesis "0",
    # et son PREMIER evenement sera justement cette remise a zero.
    "journal_events",
    # Clientes d'essai, consentements et e-mails envoyes.
    "clients",
    "consents",
    "communications",
    # Cahier du jour (objectifs figes, messages, signatures des essais).
    "cahier_days",
)

# NI CONSERVEE NI VIDEE — la table de version d'Alembic n'est pas une donnee :
# c'est l'etat du schema. La toucher ferait refuser le demarrage de l'API
# (app/main.py verifie `alembic_version` contre EXPECTED_DB_REVISION).
SCHEMA_TABLES: tuple[str, ...] = ("alembic_version",)

# Verrou : cle et champ du reglage qui marque la remise a zero comme faite.
GO_LIVE_SETTINGS_KEY = "system"
GO_LIVE_FIELD = "go_live_done_at"

# Compteurs fiscaux attribues par MAX()+1 (et non par une sequence Postgres,
# cf. app/services/pos.py et app/services/fiscal.py) : vider la table suffit
# a les faire repartir a 1. On le VERIFIE quand meme apres coup — c'est la
# promesse la plus visible du script, elle ne doit pas reposer sur une
# lecture de code.
FISCAL_COUNTERS: tuple[tuple[str, str], ...] = (
    ("transactions", "transaction_number"),
    ("z_reports", "report_number"),
)

# Tables CONSERVEES dans lesquelles le script ecrit lui-meme : elles peuvent
# donc legitimement gagner une ligne. Toutes les autres doivent retrouver
# exactement le meme compte qu'avant.
KEPT_TABLES_WRITTEN_BY_SCRIPT = frozenset(
    {
        "database_backups",  # la sauvegarde prealable
        "app_settings",      # le reglage `system.go_live_done_at`
    }
)


class ResetError(Exception):
    """Refus ou echec de la remise a zero — message deja lisible par l'operateur."""


# ---------------------------------------------------------------------------
# Affichage
# ---------------------------------------------------------------------------


def _title(text_: str) -> None:
    print(f"\n=== {text_} ===")


def _line(label: str, value) -> None:
    print(f"  {label:<34} {value}")


# ---------------------------------------------------------------------------
# Inventaire et classement des tables
# ---------------------------------------------------------------------------


async def list_database_tables(db: AsyncSession) -> set[str]:
    """Tables reelles du schema `public` (vues et tables systeme exclues)."""
    rows = (
        await db.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
    ).scalars().all()
    return set(rows)


def classify_tables(actual: set[str]) -> tuple[list[str], list[str]]:
    """Confronte la base au classement du script.

    Retourne ``(non_classees, declarees_absentes)``. Les deux doivent etre
    vides : une table non classee signifie qu'une migration a ete ajoutee
    sans decider de son sort ; une table declaree mais absente signifie que
    le script parle d'un schema qui n'est plus celui de la base.
    """
    declared = set(KEPT_TABLES) | set(EMPTIED_TABLES) | set(SCHEMA_TABLES)
    unclassified = sorted(actual - declared)
    missing = sorted((set(KEPT_TABLES) | set(EMPTIED_TABLES)) - actual)
    return unclassified, missing


async def count_rows(db: AsyncSession, tables: tuple[str, ...]) -> dict[str, int]:
    """Comptage exact table par table.

    `COUNT(*)` et non `pg_stat_user_tables.n_live_tup` : ce chiffre est
    recopie dans un journal immuable, il doit etre juste et pas estime.
    Les noms viennent des constantes du module (jamais d'une entree
    exterieure), et sont quand meme passes en identifiants quotes.
    """
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = int(
            (await db.execute(text(f'SELECT count(*) FROM "{table}"'))).scalar_one()
        )
    return counts


# ---------------------------------------------------------------------------
# Remise a zero proprement dite
# ---------------------------------------------------------------------------


async def _truncate(db: AsyncSession) -> None:
    """Vide les tables listees, en UNE commande.

    `RESTART IDENTITY` remet a leur valeur de depart les sequences d'identite
    eventuelles ; `CASCADE` autorise l'ordre alphabetique/thematique choisi
    ci-dessus malgre les cles etrangeres. Aucun trigger n'est desactive :
    `TRUNCATE` ne declenche pas les triggers FOR EACH ROW qui protegent les
    tables fiscales.
    """
    tables = ", ".join(f'"{t}"' for t in EMPTIED_TABLES)
    await db.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


async def _restart_sequences(db: AsyncSession) -> list[str]:
    """Remet a 1 toute sequence Postgres rattachee a une table videe.

    Aujourd'hui il n'y en a aucune (cles primaires en UUID, numeros fiscaux
    attribues par MAX()+1 sous verrou consultatif). Le geste est neanmoins
    fait explicitement : le jour ou une migration introduira une colonne
    `serial`/`identity`, son compteur repartira de 1 sans qu'il faille y
    repenser.
    """
    rows = (
        await db.execute(
            text(
                "SELECT s.relname AS sequence_name "
                "FROM pg_class s "
                "JOIN pg_depend d ON d.objid = s.oid "
                "  AND d.classid = 'pg_class'::regclass AND d.deptype IN ('a', 'i') "
                "JOIN pg_class t ON t.oid = d.refobjid "
                "JOIN pg_namespace n ON n.oid = s.relnamespace "
                "WHERE s.relkind = 'S' AND n.nspname = 'public' "
                "  AND t.relname = ANY(:tables)"
            ),
            {"tables": list(EMPTIED_TABLES)},
        )
    ).scalars().all()
    for name in rows:
        await db.execute(text(f'ALTER SEQUENCE "{name}" RESTART'))
    return sorted(rows)


async def _check_counters_restarted(db: AsyncSession) -> list[str]:
    """Verifie que les compteurs fiscaux repartent bien a 1.

    Les tables etant vides, le MAX courant doit valoir 0 : la prochaine vente
    prendra le numero 1, le prochain Z aussi. Le journal, lui, n'est pas vide
    (il porte la trace de cette remise a zero) : sa verification est le
    `min(seq) = 1` du bloc de verification, pas un MAX attendu.
    """
    problems: list[str] = []
    for table, column in FISCAL_COUNTERS:
        value = int(
            (
                await db.execute(
                    text(f'SELECT coalesce(max("{column}"), 0) FROM "{table}"')
                )
            ).scalar_one()
        )
        if value != 0:
            problems.append(f"{table}.{column} = {value} (attendu 0)")
    return problems


# ---------------------------------------------------------------------------
# Verrou de non-rejouabilite
# ---------------------------------------------------------------------------


async def read_go_live_done_at(db: AsyncSession) -> str | None:
    value = await SettingsService(db).get(GO_LIVE_SETTINGS_KEY)
    done_at = value.get(GO_LIVE_FIELD)
    return str(done_at) if done_at else None


def _prompt_shop_name(expected: str) -> bool:
    """`--force` : ressaisie du nom de la boutique.

    Seule protection contre un `--force` tape par habitude sur une base de
    production deja ouverte : il faut ecrire le nom de la boutique, ce qu'on
    ne fait pas par reflexe.
    """
    print(
        "\nLa remise a zero a DEJA ete faite sur cette base. Relancer avec "
        "--force effacera des ventes\npotentiellement REELLES.\n"
        f"Pour confirmer, retapez exactement le nom de la boutique : {expected}"
    )
    try:
        typed = input("Nom de la boutique : ").strip()
    except EOFError:
        return False
    return typed == expected


async def _shop_name(db: AsyncSession) -> str:
    shop = await SettingsService(db).get("shop")
    return (shop.get("name") or "").strip() or settings.SHOP_NAME


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def database_url() -> str:
    """URL de connexion — role proprietaire d'abord (seul a pouvoir TRUNCATE)."""
    url = (settings.MIGRATION_DATABASE_URL or "").strip()
    if url:
        return url
    fallback = (settings.DATABASE_URL or "").strip()
    if not fallback:
        raise ResetError(
            "Aucune URL de base de donnees : renseignez MIGRATION_DATABASE_URL."
        )
    print(
        "[!] MIGRATION_DATABASE_URL absente — repli sur DATABASE_URL. Le role "
        "applicatif n'est\n    pas proprietaire des tables en production : le "
        "TRUNCATE y sera refuse."
    )
    return fallback


async def run(args: argparse.Namespace) -> int:
    engine = create_async_engine(database_url(), poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await _run_with(session_factory, args)
    finally:
        await engine.dispose()


async def _run_with(session_factory, args: argparse.Namespace) -> int:
    print("\n" + "=" * 62)
    print("  Frip & Co Street — remise a zero pre-ouverture")
    print("=" * 62)

    # --- 0. le classement des tables doit coller a la base ------------------
    async with session_factory() as db:
        actual = await list_database_tables(db)
        unclassified, missing = classify_tables(actual)
        if unclassified or missing:
            _title("Classement des tables : INCOHERENT")
            for name in unclassified:
                print(
                    f"  [X] table non classee : {name} — ajoutez-la a "
                    "KEPT_TABLES ou EMPTIED_TABLES"
                )
            for name in missing:
                print(f"  [X] table declaree mais absente de la base : {name}")
            print(
                "\nRien n'a ete ecrit. Une migration a ete ajoutee sans decider "
                "du sort de sa table :\nc'est exactement ce que ce garde-fou "
                "sert a empecher."
            )
            return 1
        _line("Tables conservees", f"{len(KEPT_TABLES)}")
        _line("Tables videes", f"{len(EMPTIED_TABLES)}")

        # --- 1. verrou de non-rejouabilite ---------------------------------
        done_at = await read_go_live_done_at(db)
        shop_name = await _shop_name(db)
        _line("Boutique", shop_name)
        _line("Remise a zero deja faite", done_at or "non")
        if done_at and not args.force:
            print(
                f"\n[X] Refus : la remise a zero a deja ete faite le {done_at}.\n"
                "    Ce script est une operation D'AVANT-OUVERTURE. Si la "
                "boutique tourne, ses ventes\n    sont des donnees fiscales : "
                "elles ne s'effacent pas.\n"
                "    (--force existe pour une reouverture preparee, et exige "
                "de retaper le nom de la boutique.)"
            )
            return 1

        # --- 2. comptages avant --------------------------------------------
        emptied_counts = await count_rows(db, EMPTIED_TABLES)
        kept_counts = await count_rows(db, KEPT_TABLES)

    _title("Sera VIDE")
    for table in EMPTIED_TABLES:
        _line(table, f"{emptied_counts[table]:>8} ligne(s)")
    _title("Sera CONSERVE")
    for table in KEPT_TABLES:
        _line(table, f"{kept_counts[table]:>8} ligne(s)")

    if args.dry_run:
        _title("Essai a blanc (--dry-run)")
        print("  Rien n'a ete ecrit. Auraient ete faits, dans cet ordre :")
        print("   1. sauvegarde applicative manuelle (pg_dump | gzip, verifiee)")
        print("   2. TRUNCATE ... RESTART IDENTITY CASCADE (une transaction)")
        print("   3. remise a 1 des compteurs (vente n° 1, Z n° 1, genesis JET)")
        print(f"   4. premier evenement du journal : {EVENT_SYSTEM_GO_LIVE_RESET}")
        print(f"   5. reglage {GO_LIVE_SETTINGS_KEY}.{GO_LIVE_FIELD}")
        print("\n  Pour executer reellement : --confirm")
        return 0

    # --- 3. ressaisie du nom de la boutique si --force ---------------------
    if done_at and args.force and not _prompt_shop_name(shop_name):
        print("\n[X] Nom de boutique incorrect — rien n'a ete fait.")
        return 1

    # --- 4. sauvegarde manuelle prealable, obligatoire ---------------------
    _title("Sauvegarde prealable")
    async with session_factory() as db:
        try:
            backup = await run_backup(db, trigger="manual")
        except BackupError as exc:
            print(f"  [X] Sauvegarde echouee : {exc}")
            print(
                "\nRien n'a ete efface. On ne vide pas une base dont on n'a pas "
                "reussi a faire une copie."
            )
            return 1
        backup_id = str(backup.id)
        _line("Fichier", backup.filename)
        _line("Taille", f"{backup.size_bytes} octets")
        _line("Empreinte SHA-256", backup.sha256)

    # --- 5. la remise a zero, en UNE transaction ---------------------------
    _title("Remise a zero")
    reset_at = datetime.now(timezone.utc)
    async with session_factory() as db:
        try:
            await _truncate(db)
            restarted = await _restart_sequences(db)
            await JournalService(db).record(
                EVENT_SYSTEM_GO_LIVE_RESET,
                payload={
                    "at": reset_at.isoformat(timespec="seconds"),
                    "by": "script",
                    "previous_counts": emptied_counts,
                    "backup_id": backup_id,
                },
            )
            await SettingsService(db).set(
                GO_LIVE_SETTINGS_KEY,
                {GO_LIVE_FIELD: reset_at.isoformat(timespec="seconds")},
                user_id=None,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    _line("Tables videes", len(EMPTIED_TABLES))
    _line("Sequences remises a 1", ", ".join(restarted) or "aucune (compteurs MAX+1)")

    # --- 6. verification apres coup ----------------------------------------
    _title("Verification")
    async with session_factory() as db:
        after_emptied = await count_rows(db, EMPTIED_TABLES)
        after_kept = await count_rows(db, KEPT_TABLES)
        counter_problems = await _check_counters_restarted(db)
        chain = await JournalService(db).verify_chain()
        first = (
            await db.execute(
                text(
                    "SELECT seq, event_type, previous_hash FROM journal_events "
                    "ORDER BY seq ASC LIMIT 1"
                )
            )
        ).first()

    problems = list(counter_problems)
    # `journal_events` n'est evidemment plus vide : elle porte desormais la
    # trace de cette remise a zero (et le `config.changed` du reglage pose).
    for table, count in after_emptied.items():
        if table != "journal_events" and count:
            problems.append(f"{table} contient encore {count} ligne(s)")
    for table, count in after_kept.items():
        before = kept_counts[table]
        # Les tables conservees ne doivent RIEN perdre. Deux d'entre elles
        # gagnent une ligne, ecrite par ce script lui-meme (la sauvegarde et
        # le reglage) : c'est la seule croissance admise.
        if count < before or (
            count != before and table not in KEPT_TABLES_WRITTEN_BY_SCRIPT
        ):
            problems.append(
                f"{table} est passee de {before} a {count} ligne(s)"
            )
    first_event = first.event_type if first is not None else None
    if first is None:
        problems.append("le journal est vide apres la remise a zero")
    else:
        if first.event_type != EVENT_SYSTEM_GO_LIVE_RESET:
            problems.append(
                f"premier evenement du journal = {first.event_type!r} "
                f"(attendu {EVENT_SYSTEM_GO_LIVE_RESET!r})"
            )
        if first.seq != 1:
            problems.append(f"le journal repart a la sequence {first.seq} (attendu 1)")
        if first.previous_hash != GENESIS_HASH:
            problems.append(
                "le premier evenement n'est pas chaine sur le genesis "
                f"(previous_hash={first.previous_hash!r})"
            )
    if not chain["valid"]:
        problems.append("chaine du journal invalide : " + "; ".join(chain["errors"]))

    _line("Prochaine vente", "n° 1")
    _line("Prochain rapport Z", "n° 1")
    _line("Premier evenement du journal", first_event)
    _line("Chaine du journal", "valide" if chain["valid"] else "INVALIDE")
    _line("Reglage pose", f"{GO_LIVE_SETTINGS_KEY}.{GO_LIVE_FIELD}")

    if problems:
        print("\n[X] Anomalies apres remise a zero :")
        for problem in problems:
            print(f"    - {problem}")
        print(
            "\n    La base a ete modifiee. Restaurez la sauvegarde ci-dessus "
            "avant toute vente."
        )
        return 1

    print("\n[OK] Remise a zero terminee. La boutique peut ouvrir.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Remise a zero pre-ouverture : efface les ventes, tickets, Z, "
            "clientes et le journal d'essai, conserve le parametrage, les "
            "comptes et les sauvegardes."
        )
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="N'ecrit rien : affiche le plan (tables, comptages, sauvegarde prevue).",
    )
    group.add_argument(
        "--confirm",
        action="store_true",
        help="Execute reellement la remise a zero.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Passe outre le verrou d'une remise a zero deja faite "
            "(exige de retaper le nom de la boutique)."
        ),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except ResetError as exc:
        print(f"[X] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
