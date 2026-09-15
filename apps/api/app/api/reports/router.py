# Nouveau routeur (PR6, H3 de docs/ARCHITECTURE_PR6.md) — tableau de bord
# d'accueil. Lecture seule : aucune ecriture, aucune signature fiscale,
# aucun evenement JET (comme la lecture du JET elle-meme, §4.7 PR2).
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.fiscal import PosServiceError
from app.services.pos import _PARIS
from app.services.reporting import dashboard, serialize_dashboard

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/dashboard")
async def get_dashboard(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date: Annotated[str | None, Query(description="Jour de reference, YYYY-MM-DD")] = None,
):
    """Chiffres d'accueil : jour, mois, sept derniers jours, vs objectifs.

    `?date=YYYY-MM-DD` deplace le jour de reference (consultation d'un jour
    passe) : il est converti en instant de reference (midi, heure de Paris —
    n'importe quelle heure de la journee civile donne le meme resultat) et
    passe tel quel au service, qui en derive « aujourd'hui », le mois et la
    serie 7 jours.
    """
    now: datetime | None = None
    if date is not None:
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise PosServiceError(
                f"Date invalide ({date!r}) : format YYYY-MM-DD attendu.",
                code="invalid_date",
                status_code=422,
            )
        now = datetime(day.year, day.month, day.day, 12, 0, tzinfo=_PARIS)

    return serialize_dashboard(await dashboard(db, now=now))
