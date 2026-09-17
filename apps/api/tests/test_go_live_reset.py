# Nouveau test (PR12, docs/ARCHITECTURE_PR12.md, contrat N3) — remise a zero
# pre-ouverture (`scripts/go_live_reset.py`).
#
# Ce test tourne sur SA PROPRE base (`<base de test>_reset`), creee et
# supprimee ici : le script tronque tout ce qu'il trouve, le lancer sur la
# base de la suite vaporiserait les donnees du test en cours dans un autre
# processus. Le script est lance en SOUS-PROCESSUS, comme en production
# (`docker exec ... python scripts/go_live_reset.py`) : c'est la vraie ligne
# de commande, le vrai `pg_dump`, les vrais codes de retour.
import asyncio
import os
import subprocess
import sys

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services.jet import EVENT_SYSTEM_GO_LIVE_RESET, JournalService
from tests.conftest import API_DIR, TEST_DATABASE_URL

pytestmark = pytest.mark.anyio

# Base dediee, DERIVEE de celle de la suite plutot qu'ecrite en dur : deux
# executions en parallele (deux postes, deux branches de CI) pointant sur des
# bases de test differentes gardent chacune la sienne.
RESET_DATABASE_URL = TEST_DATABASE_URL + "_reset"
RESET_DATABASE_NAME = RESET_DATABASE_URL.rsplit("/", 1)[-1]
MAINTENANCE_DSN = (
    TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
    + "/postgres"
)

SCRIPT = "scripts/go_live_reset.py"


# ---------------------------------------------------------------------------
# Base de test dediee
# ---------------------------------------------------------------------------


async def _recreate_database() -> None:
    conn = await asyncpg.connect(MAINTENANCE_DSN)
    try:
        # FORCE : une connexion oubliee d'un run precedent ne doit pas faire
        # echouer la preparation du test.
        await conn.execute(f'DROP DATABASE IF EXISTS "{RESET_DATABASE_NAME}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{RESET_DATABASE_NAME}"')
    finally:
        await conn.close()


async def _drop_database() -> None:
    conn = await asyncpg.connect(MAINTENANCE_DSN)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{RESET_DATABASE_NAME}" WITH (FORCE)')
    finally:
        await conn.close()


def _migrate() -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = RESET_DATABASE_URL
    env.pop("MIGRATION_DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(API_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# Un jeu d'essai minimal mais REEL : une ligne dans chacune des tables que le
# script promet de vider, et une ligne dans chacune de celles qu'il promet de
# conserver. Les numeros fiscaux sont volontairement eleves (vente n° 7, Z
# n° 3) pour que « repart a 1 » se verifie vraiment.
#
# `transactions.hash_chain` reste vide : le trigger `fripco_protect_fiscal_child`
# refuse l'INSERT d'une ligne ou d'un paiement rattache a une vente DEJA
# signee. On peuple donc la vente non encore scellee — ce qui suffit ici :
# le script tronque, il ne relit aucune signature.
SEED_SQL = (
    "INSERT INTO users (id, username, email, hashed_password) "
    "VALUES ('11111111-1111-1111-1111-111111111111', 'manager-essai', "
    "'manager@exemple.test', 'hash')",
    "INSERT INTO cashiers (id, display_name) "
    "VALUES ('22222222-2222-2222-2222-222222222222', 'Vendeuse d''essai')",
    "INSERT INTO app_settings (key, value) "
    "VALUES ('shop', '{\"name\": \"Boutique d''essai\"}'::jsonb)",
    "INSERT INTO database_backups (id, trigger, status, filename) "
    "VALUES ('33333333-3333-3333-3333-333333333333', 'nightly', 'success', "
    "'fripco_20260101_030000.sql.gz')",
    "INSERT INTO clients (id, email, newsletter_optin) "
    "VALUES ('44444444-4444-4444-4444-444444444444', 'cliente@exemple.test', true)",
    "INSERT INTO consents (id, client_id, purpose, granted, source, policy_version) "
    "VALUES (gen_random_uuid(), '44444444-4444-4444-4444-444444444444', "
    "'newsletter', true, 'pos', '2026-09')",
    "INSERT INTO communications (id, kind, channel, recipient, provider, status) "
    "VALUES (gen_random_uuid(), 'receipt', 'email', 'cliente@exemple.test', "
    "'simulated', 'simulated')",
    "INSERT INTO transactions (id, transaction_number, transaction_type, user_id, "
    "tva_rate, total_ttc, hash_chain, previous_hash) "
    "VALUES ('55555555-5555-5555-5555-555555555555', 7, 'sale', "
    "'11111111-1111-1111-1111-111111111111', 20.00, 12.00, '', '0')",
    "INSERT INTO transaction_items (id, transaction_id, unit_price, line_total, "
    "tva_rate, line_ht, line_tva) "
    "VALUES (gen_random_uuid(), '55555555-5555-5555-5555-555555555555', 12.00, "
    "12.00, 20.00, 10.00, 2.00)",
    "INSERT INTO payments (id, transaction_id, method, amount) "
    "VALUES (gen_random_uuid(), '55555555-5555-5555-5555-555555555555', 'cash', 12.00)",
    "INSERT INTO receipts (id, transaction_id, content) "
    "VALUES (gen_random_uuid(), '55555555-5555-5555-5555-555555555555', 'ticket')",
    "INSERT INTO payment_attempts (id, client_uuid, amount, checkout_id) "
    "VALUES ('66666666-6666-6666-6666-666666666666', gen_random_uuid(), 12.00, 'chk-1')",
    "INSERT INTO failed_payments (id, attempt_id, client_uuid, amount) "
    "VALUES (gen_random_uuid(), '66666666-6666-6666-6666-666666666666', "
    "gen_random_uuid(), 12.00)",
    "INSERT INTO sumup_exchanges (id, operation, method, url_path) "
    "VALUES (gen_random_uuid(), 'checkout', 'POST', '/v0.1/checkouts')",
    "INSERT INTO cash_drawers (id, user_id, opened_at, opening_amount) "
    "VALUES ('77777777-7777-7777-7777-777777777777', "
    "'11111111-1111-1111-1111-111111111111', now(), 100.00)",
    "INSERT INTO cash_movements (id, drawer_id, direction, amount, user_id) "
    "VALUES (gen_random_uuid(), '77777777-7777-7777-7777-777777777777', 'out', "
    "20.00, '11111111-1111-1111-1111-111111111111')",
    "INSERT INTO z_reports (id, report_number, user_id, cash_drawer_id, opened_at, "
    "closed_at, last_transaction_hash, hash, previous_hash) "
    "VALUES ('88888888-8888-8888-8888-888888888888', 3, "
    "'11111111-1111-1111-1111-111111111111', "
    "'77777777-7777-7777-7777-777777777777', now(), now(), '0', 'aa', '0')",
    "INSERT INTO accounting_exports (id, z_report_id, export_date) "
    "VALUES ('99999999-9999-9999-9999-999999999999', "
    "'88888888-8888-8888-8888-888888888888', current_date)",
    "INSERT INTO accounting_export_lines (id, export_id, line_number, account_number, "
    "account_label, label, piece_reference) "
    "VALUES (gen_random_uuid(), '99999999-9999-9999-9999-999999999999', 1, '707100', "
    "'Ventes marchandises', 'Ventes du Z 3', 'Z3')",
    "INSERT INTO fiscal_closures (id, sequence_number, closure_type, period_start, "
    "period_end, software_version, fiscal_version_date, archive_sha256, "
    "archive_content, previous_hash, hash) "
    "VALUES (gen_random_uuid(), 1, 'monthly', now(), now(), '0.12.0', '2026-09-15', "
    "'bb', '\\x00'::bytea, '0', 'cc')",
    "INSERT INTO invoices (id, transaction_id, invoice_number, company_name, siret, "
    "address_line1, postal_code, city, issued_at, seller_snapshot) "
    "VALUES (gen_random_uuid(), '55555555-5555-5555-5555-555555555555', 'F-2026-0001', "
    "'Societe d''essai', '12345678901234', '1 rue du Test', '27200', 'Ville', now(), "
    "'{}'::jsonb)",
    "INSERT INTO journal_events (id, seq, event_type, previous_hash, hash) "
    "VALUES (gen_random_uuid(), 1, 'system.startup', '0', 'dd')",
    "INSERT INTO cahier_days (day) VALUES (current_date)",
)


async def _seed() -> None:
    engine = create_async_engine(RESET_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for statement in SEED_SQL:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture
def reset_database():
    """Base dediee, migree au head et peuplee — supprimee a la fin du test."""
    asyncio.run(_recreate_database())
    _migrate()
    asyncio.run(_seed())
    yield RESET_DATABASE_URL
    asyncio.run(_drop_database())


def _run_script(*args: str, backup_dir, stdin: str | None = None):
    env = os.environ.copy()
    env["DATABASE_URL"] = RESET_DATABASE_URL
    env["MIGRATION_DATABASE_URL"] = RESET_DATABASE_URL
    env["BACKUP_DIR"] = str(backup_dir)
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        cwd=str(API_DIR),
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
    )


async def _fetch(sql: str):
    engine = create_async_engine(RESET_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).all()
    finally:
        await engine.dispose()


async def _scalar(sql: str):
    rows = await _fetch(sql)
    return rows[0][0]


# ---------------------------------------------------------------------------
# Scenario complet
# ---------------------------------------------------------------------------


async def test_dry_run_then_confirm_then_refused(reset_database, tmp_path):
    backup_dir = tmp_path / "backups"

    # --- 1. essai a blanc : aucun effet ------------------------------------
    dry = _run_script("--dry-run", backup_dir=backup_dir)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "Rien n'a ete ecrit" in dry.stdout
    assert "transactions" in dry.stdout
    assert await _scalar("SELECT count(*) FROM transactions") == 1
    assert await _scalar("SELECT count(*) FROM journal_events") == 1
    assert await _scalar("SELECT count(*) FROM database_backups") == 1
    # Pas de sauvegarde declenchee non plus : un essai a blanc n'ecrit rien,
    # pas meme sur le disque.
    assert not backup_dir.exists() or not list(backup_dir.glob("*.sql.gz"))

    # --- 2. execution reelle ------------------------------------------------
    run = _run_script("--confirm", backup_dir=backup_dir)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "Remise a zero terminee" in run.stdout

    # Sauvegarde prealable reellement produite (pg_dump | gzip verifie).
    dumps = list(backup_dir.glob("*.sql.gz"))
    assert len(dumps) == 1
    assert dumps[0].stat().st_size > 0

    # Tables videes.
    for table in (
        "transactions",
        "transaction_items",
        "payments",
        "receipts",
        "payment_attempts",
        "failed_payments",
        "sumup_exchanges",
        "cash_drawers",
        "cash_movements",
        "z_reports",
        "accounting_exports",
        "accounting_export_lines",
        "fiscal_closures",
        "invoices",
        "clients",
        "consents",
        "communications",
        "cahier_days",
    ):
        assert await _scalar(f"SELECT count(*) FROM {table}") == 0, table

    # Tables conservees — dont la ligne de sauvegarde d'origine ET celle que
    # le script vient d'ecrire.
    assert await _scalar("SELECT count(*) FROM users") == 1
    assert await _scalar("SELECT count(*) FROM cashiers") == 1
    assert await _scalar("SELECT count(*) FROM database_backups") == 2
    assert (
        await _scalar("SELECT value->>'name' FROM app_settings WHERE key = 'shop'")
        == "Boutique d'essai"
    )

    # Compteurs fiscaux : la prochaine vente prend le n° 1, le prochain Z aussi.
    assert await _scalar("SELECT coalesce(max(transaction_number), 0) FROM transactions") == 0
    assert await _scalar("SELECT coalesce(max(report_number), 0) FROM z_reports") == 0

    # Journal : chaine repartie du genesis, premier evenement = la remise a
    # zero, second = le reglage pose (`config.changed`).
    events = await _fetch(
        "SELECT seq, event_type, previous_hash FROM journal_events ORDER BY seq"
    )
    assert [e.seq for e in events] == [1, 2]
    assert events[0].event_type == EVENT_SYSTEM_GO_LIVE_RESET
    assert events[0].previous_hash == "0"
    assert events[1].event_type == "config.changed"

    payload = await _scalar(
        "SELECT payload FROM journal_events WHERE seq = 1"
    )
    assert payload["by"] == "script"
    assert payload["previous_counts"]["transactions"] == 1
    assert payload["previous_counts"]["z_reports"] == 1
    assert payload["backup_id"]

    # Chaine verifiee par le meme code que l'application (HMAC + chainage).
    engine = create_async_engine(RESET_DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            chain = await JournalService(db).verify_chain()
    finally:
        await engine.dispose()
    assert chain["valid"], chain["errors"]
    assert chain["count"] == 2

    # Verrou pose.
    done_at = await _scalar(
        "SELECT value->>'go_live_done_at' FROM app_settings WHERE key = 'system'"
    )
    assert done_at

    # --- 3. second lancement refuse ----------------------------------------
    again = _run_script("--confirm", backup_dir=backup_dir)
    assert again.returncode == 1
    assert "deja ete faite" in again.stdout
    assert await _scalar("SELECT count(*) FROM journal_events") == 2

    # --- 4. --force : mauvais nom de boutique -> rien ne se passe -----------
    wrong = _run_script(
        "--confirm", "--force", backup_dir=backup_dir, stdin="Mauvais nom\n"
    )
    assert wrong.returncode == 1
    assert "Nom de boutique incorrect" in wrong.stdout
    assert await _scalar("SELECT count(*) FROM journal_events") == 2

    # --- 5. --force : bon nom de boutique -> nouvelle remise a zero ---------
    forced = _run_script(
        "--confirm", "--force", backup_dir=backup_dir, stdin="Boutique d'essai\n"
    )
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert await _scalar("SELECT count(*) FROM journal_events") == 2
    assert (
        await _scalar("SELECT event_type FROM journal_events ORDER BY seq LIMIT 1")
        == EVENT_SYSTEM_GO_LIVE_RESET
    )


# ---------------------------------------------------------------------------
# Garde-fou : une table non classee fait echouer AVANT toute ecriture
# ---------------------------------------------------------------------------


async def test_unclassified_table_fails_before_writing(reset_database, tmp_path):
    """Simule une future migration dont on aurait oublie de classer la table."""
    engine = create_async_engine(RESET_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE future_feature (id integer)"))
    finally:
        await engine.dispose()

    for mode in ("--dry-run", "--confirm"):
        result = _run_script(mode, backup_dir=tmp_path / "backups")
        assert result.returncode == 1, result.stdout
        assert "table non classee : future_feature" in result.stdout
        # Rien n'a bouge : le refus est prononce avant la sauvegarde et avant
        # le moindre TRUNCATE.
        assert await _scalar("SELECT count(*) FROM transactions") == 1
        assert await _scalar("SELECT count(*) FROM journal_events") == 1
