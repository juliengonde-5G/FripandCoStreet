# Nouveau — extrait/simplifie du endpoint /api/health de l'application
# source (module équivalent de l'application source : apps/api/app/main.py)
from fastapi import APIRouter

from app.core.config import settings
from app.version import APP_VERSION, EXPECTED_DB_REVISION

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    """Sonde de sante + identifiants de version.

    Permet de verifier qu'un deploiement a bien atterri : le SHA git court
    est injecte au build dans ``FRIPCO_BUILD_SHA`` (sinon ``unknown``).
    """
    return {
        "status": "ok",
        "app": "fripco-street-api",
        "version": APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "expected_db_revision": EXPECTED_DB_REVISION,
        "build_sha": settings.build_sha,
        "build_date": settings.build_date,
    }
