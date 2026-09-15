# Extrait de Vintiz (apps/api/app/main.py) — perimetre PR1 (auth + JET) :
# pas d'APScheduler, pas de create_all en dev (schema migration-owned des
# la premiere revision, y compris le trigger d'immuabilite NF525), pas de
# handler AuditContextMiddleware (retire, cf. app/core/middleware.py).
import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.admin.router import router as admin_router
from app.api.auth.router import router as auth_router
from app.api.health import router as health_router
from app.api.pos.cb_router import router as cb_router
from app.api.pos.router import router as pos_router
from app.core.config import settings
from app.core.database import async_session, engine
from app.core.exceptions import (
    AuthenticationError,
    FripcoError,
    PermissionDenied,
    ResourceConflict,
    ResourceNotFound,
)
from app.core.logging_config import setup_logging
from app.core.middleware import RequestIdMiddleware, SecurityHeadersMiddleware
from app.jobs import register_all_jobs
from app.services.fiscal import PosServiceError
from app.services.jet import EVENT_SYSTEM_STARTUP, JournalService
from app.version import APP_VERSION, EXPECTED_DB_REVISION

setup_logging()
logger = logging.getLogger("fripco")


async def _current_db_revision() -> str | None:
    async with engine.connect() as conn:
        try:
            return (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
        except Exception:
            return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.is_production:
        # Schema migration-owned (triggers NF525 inclus) : on refuse de servir
        # des requetes si la revision de la base ne correspond pas exactement
        # a celle attendue par ce build, plutot que de tourner sur un schema
        # hybride/partiel non certifiable.
        async with engine.connect() as conn:
            try:
                db_revision = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one_or_none()
            except Exception as exc:
                raise RuntimeError(
                    "Production database is not initialized by Alembic"
                ) from exc
        if db_revision != EXPECTED_DB_REVISION:
            raise RuntimeError(
                f"Database revision {db_revision!r} does not match "
                f"required revision {EXPECTED_DB_REVISION!r}"
            )
    else:
        # Hors production, le schema reste migration-owned (pas de create_all
        # implicite : les migrations portent le trigger d'immuabilite du JET,
        # qu'un create_all omettrait silencieusement).
        logger.warning(
            "ENVIRONMENT=%s — schema non verifie contre le head Alembic. "
            "Lancer `alembic upgrade head` avant d'utiliser l'API.",
            settings.ENVIRONMENT,
        )
        db_revision = await _current_db_revision()

    startup_payload = {
        "app_version": APP_VERSION,
        "db_revision": db_revision,
        "build_sha": settings.build_sha,
    }
    if settings.is_production:
        # En production l'ecriture du JET de demarrage DOIT reussir : un
        # evenement de securite manquant est pire qu'un echec de boot bruyant.
        async with async_session() as db:
            await JournalService(db).record(EVENT_SYSTEM_STARTUP, payload=startup_payload)
            await db.commit()
    else:
        # En dev/test, ne pas empecher un demarrage local sans base prete
        # (premier `pip install -e .` avant toute migration, par exemple).
        try:
            async with async_session() as db:
                await JournalService(db).record(EVENT_SYSTEM_STARTUP, payload=startup_payload)
                await db.commit()
        except Exception as exc:
            logger.warning("Evenement JET system.startup non enregistre (dev) : %s", exc)

    logger.info(
        "fripco-street API started (version=%s, db_revision=%s)",
        APP_VERSION,
        db_revision,
    )

    # Garde fiscale 23:59 (§4.3) — seul cron du perimetre PR2. Fuseau
    # Europe/Paris explicite (la boutique, pas le serveur, peut tourner en
    # UTC). Import protege : `apscheduler` est une dependance obligatoire
    # (pyproject.toml) mais un environnement de dev incomplet ne doit pas
    # empecher de demarrer l'API pour autre chose que la caisse.
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        scheduler = AsyncIOScheduler(timezone="Europe/Paris")
        register_all_jobs(scheduler)
        scheduler.start()
        logger.info("Ordonnanceur demarre (garde fiscale 23:59 Europe/Paris)")
    except ImportError:
        scheduler = None
        logger.warning("APScheduler non installe — la garde fiscale 23:59 est desactivee")

    yield

    if scheduler is not None:
        scheduler.shutdown(wait=False)
    logger.info("fripco-street API shutting down")


app = FastAPI(
    title="Frip & Co Street — API",
    description="API de caisse pour la boutique Frip & Co Street (Rouen)",
    version=APP_VERSION,
    lifespan=lifespan,
)

# Les middlewares sont appliques du bas vers le haut : le DERNIER ajoute est
# le PLUS externe. CORS doit etre le plus externe pour que TOUTE reponse — y
# compris les 500 produites par le handler d'exception global — porte
# Access-Control-Allow-Origin. CORS_ORIGINS vide (deploiement same-origin
# derriere Caddy) = pas de middleware CORS du tout.
app.add_middleware(RequestIdMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
if settings.cors_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )


_DOMAIN_STATUS: dict[type, int] = {
    ResourceNotFound: 404,
    ResourceConflict: 409,
    AuthenticationError: 401,
    PermissionDenied: 403,
}


@app.exception_handler(FripcoError)
async def domain_exception_handler(request: Request, exc: FripcoError):
    status_code = next(
        (v for k, v in _DOMAIN_STATUS.items() if isinstance(exc, k)), 422
    )
    logger.warning(
        "[%s] %s %s %s refuse (%d): %s",
        getattr(request.state, "request_id", "-"),
        request.method,
        request.url.path,
        type(exc).__name__,
        status_code,
        exc,
    )
    return JSONResponse(status_code=status_code, content={"detail": str(exc)})


@app.exception_handler(PosServiceError)
async def pos_exception_handler(request: Request, exc: PosServiceError):
    """Filet de securite pour les erreurs metier POS/fiscal (§5 : `detail` +
    `code`) — chaque route les convertit deja explicitement, ce handler ne
    couvre qu'un appel qui aurait echappe a cette conversion."""
    logger.warning(
        "[%s] %s %s %s refuse (%d %s): %s",
        getattr(request.state, "request_id", "-"),
        request.method,
        request.url.path,
        type(exc).__name__,
        exc.status_code,
        exc.code,
        exc,
    )
    return JSONResponse(
        status_code=exc.status_code, content={"detail": str(exc), "code": exc.code}
    )


# Filet de securite : la plupart des exceptions non gerees sont deja
# capturees par RequestIdMiddleware (la frontiere d'erreur 500 principale) ;
# ce handler global couvre uniquement ce qui serait leve en dehors d'elle.
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "-")
    logger.error(
        "[%s] Unhandled exception on %s %s: %s\n%s",
        request_id,
        request.method,
        request.url.path,
        exc,
        traceback.format_exc(),
    )
    if settings.is_production:
        body = {"detail": "Une erreur interne est survenue.", "request_id": request_id}
    else:
        body = {"detail": f"{type(exc).__name__}: {exc}", "request_id": request_id}
    return JSONResponse(status_code=500, content=body)


app.include_router(health_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(pos_router, prefix="/api")
app.include_router(cb_router, prefix="/api")
