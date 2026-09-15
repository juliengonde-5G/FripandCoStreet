# Inspire de Vintiz (apps/api/tests/conftest.py) — adapte a un schema
# migration-owned et a PostgreSQL uniquement (pas de SQLite : le trigger
# d'immuabilite du JET doit etre exerce pour de vrai, cf. test_jet.py).
import os

import pytest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://fripco:fripco@localhost:5432/fripco_test"
)
if not TEST_DATABASE_URL.startswith("postgresql"):
    pytest.exit(
        "TEST_DATABASE_URL doit pointer vers PostgreSQL — SQLite est interdit ici : "
        "les triggers d'immuabilite (JET, NF525) doivent etre exerces pour de vrai, "
        "pas simules sur un moteur qui ne les supporte pas.",
        returncode=1,
    )

# Ces variables DOIVENT etre posees avant tout `from app...` — y compris plus
# bas dans ce module — car `Settings()` (app.core.config) et le moteur async
# (app.core.database) sont construits une seule fois, a l'import.
os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("FISCAL_SIGNING_KEY", "test-fiscal-signing-key-0123456789abcdef")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdefghijklmnop")

import asyncio  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.core import rate_limit as _rate_limit_module  # noqa: E402
from app.core.database import async_session, engine  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models.user import User  # noqa: E402

API_DIR = Path(__file__).resolve().parents[1]

MANAGER_USERNAME = "julien"
MANAGER_EMAIL = "julien@fripco-street.fr"
MANAGER_PASSWORD = "un-mot-de-passe-tres-solide"


def _run_alembic_upgrade() -> None:
    """Lance `alembic upgrade head` dans un sous-processus.

    `alembic/env.py` pilote sa propre boucle asyncio (`asyncio.run`) : appeler
    `alembic.command.upgrade` directement depuis ce fixture, deja sous
    pytest-asyncio, leverait "asyncio.run() cannot be called from a running
    event loop". Le sous-processus isole entierement ce probleme.
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = TEST_DATABASE_URL
    # `alembic/env.py` utilise MIGRATION_DATABASE_URL en priorite quand elle
    # est definie. Un dev qui l'a exportee dans son shell (pour pointer les
    # migrations sur le role proprietaire en local/prod) verrait sinon les
    # tests migrer — et potentiellement modifier le schema de — une tout
    # autre base que TEST_DATABASE_URL. Les tests pilotent explicitement
    # DATABASE_URL ci-dessus ; on retire donc l'eventuel heritage.
    env.pop("MIGRATION_DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(API_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "`alembic upgrade head` a echoue:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


async def _reset_schema() -> None:
    reset_engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with reset_engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await reset_engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    """Repart d'un schema vierge une fois par session, puis migre au head.

    `alembic downgrade` n'est pas une option : la migration 0001 est
    volontairement irreversible (chaine fiscale). `DROP SCHEMA ... CASCADE`
    efface aussi les fonctions/triggers d'une execution precedente.
    """
    asyncio.run(_reset_schema())
    _run_alembic_upgrade()
    yield


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _truncate_tables():
    """Vide les tables entre chaque test pour isoler les cas.

    TRUNCATE contourne les triggers ROW (BEFORE UPDATE/DELETE) — c'est
    acceptable UNIQUEMENT ici : c'est justement l'inverse de ce que
    `test_jet.py` verifie ne pas etre possible via un UPDATE/DELETE normal.

    Fixture volontairement SYNCHRONE (via `asyncio.run`), pas `async def` :
    etant `autouse`, elle s'applique aussi aux tests synchrones de
    `test_config.py`/`test_isolation.py`/`test_expected_db_revision.py`, qui
    ne sont pas marques `@pytest.mark.anyio`. Un fixture `async def` autouse
    non marquee y declenche un conflit d'enregistrement entre le plugin
    pytest de `anyio` et le coeur de pytest (l'invariant interne
    `assert not self._finalizers` casse en cascade pour le reste de la
    session). `asyncio.run` fonctionne uniformement, marque ou non — c'est
    un simple appel synchrone au moment du setup pytest.
    """
    asyncio.run(_truncate())
    # Le rate-limiter est en memoire process (pas en base) : sans ce reset,
    # les tentatives d'un test rate-limite persisteraient dans le suivant
    # (meme cle "login:<ip>", tous les tests partageant le meme process).
    _rate_limit_module._buckets.clear()
    yield


async def _truncate() -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE users, journal_events, app_settings, transactions, "
                "transaction_items, payments, cash_drawers, z_reports, "
                "cash_movements, payment_attempts, receipts, "
                "clients, consents, communications "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
async def client():
    """Client HTTP asynchrone monte directement sur l'app ASGI.

    `ASGITransport` ne declenche pas le lifespan de l'app (comme dans
    Vintiz) : le schema est deja pret via `_prepare_database`, donc ce n'est
    pas necessaire ici.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def manager() -> User:
    """Le compte unique de fripco-street, pret pour les tests d'auth."""
    async with async_session() as db:
        user = User(
            username=MANAGER_USERNAME,
            email=MANAGER_EMAIL,
            hashed_password=hash_password(MANAGER_PASSWORD),
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


@pytest.fixture
async def auth_headers(client: AsyncClient, manager: User) -> dict[str, str]:
    response = await client.post(
        "/api/auth/login",
        data={"username": manager.username, "password": MANAGER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def open_drawer(client: AsyncClient, auth_headers: dict[str, str]) -> dict:
    """Ouvre une caisse (fond 100 €) — la plupart des tests POS/PR2 exigent
    une caisse ouverte (D1)."""
    response = await client.post(
        "/api/pos/drawer/open", json={"opening_amount": "100.00"}, headers=auth_headers
    )
    assert response.status_code == 200, response.text
    return response.json()
