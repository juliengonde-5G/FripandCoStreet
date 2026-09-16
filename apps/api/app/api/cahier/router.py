# Nouveau routeur (PR11, docs/ARCHITECTURE_PR11.md, contrat M2) — le cahier
# du jour. Toutes les routes exigent le JWT du manager.
#
# Rien de fiscal ici : les chiffres affiches sont des LECTURES agregees de
# `transactions` (recalculees a chaque appel), et les seules ecritures vont
# dans `cahier_days` (table d'exploitation) et au JET (qui ne recoit jamais
# le texte).
from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services import cahier as cahier_service
from app.services.fiscal import PosServiceError

router = APIRouter(prefix="/cahier", tags=["cahier"])


def _parse_day(value: str) -> date_cls:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise PosServiceError(
            f"Date invalide ({value!r}) : format YYYY-MM-DD attendu.",
            code="invalid_date",
            status_code=422,
        )


class CahierConfigIn(BaseModel):
    """Jours d'ouverture, lundi -> dimanche. Exactement 7 booleens : une
    semaine plus courte ou plus longue n'existe pas, et une liste tronquee
    decalerait silencieusement tous les jours suivants."""

    weekday_open: list[bool] = Field(min_length=7, max_length=7)


class CahierTextIn(BaseModel):
    """Les deux textes libres. Un champ ABSENT n'est pas touche (le front
    n'envoie que celui qu'il vient de quitter) ; un champ vide efface."""

    message: str | None = Field(
        default=None, max_length=cahier_service.TEXT_MAX_LENGTH
    )
    operation: str | None = Field(
        default=None, max_length=cahier_service.TEXT_MAX_LENGTH
    )


class CahierSignatureIn(BaseModel):
    role: Literal["manager", "team"]
    # Utilise seulement pour l'equipe, et seulement si aucune vendeuse n'est
    # identifiee sur le tiroir.
    name: str | None = Field(default=None, max_length=60)


# Declaree AVANT `/{day}` : sans cela, FastAPI lirait « config » comme une
# date et repondrait 422 (meme precaution que `/clients/duplicates`).
@router.get("/config")
async def get_cahier_config(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return {"weekday_open": await cahier_service.weekday_open(db)}


@router.put("/config")
async def put_cahier_config(
    payload: CahierConfigIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    opens = await cahier_service.set_weekday_open(
        db, payload.weekday_open, user_id=user.id
    )
    return {"weekday_open": opens}


@router.get("/{day}")
async def get_cahier_day(
    day: str,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Journee du cahier. La PREMIERE lecture d'un jour le cree : objectif
    fige, et instantane meteo fige s'il s'agit du jour meme."""
    return await cahier_service.read_day(db, _parse_day(day))


@router.put("/{day}/text")
async def put_cahier_text(
    day: str,
    payload: CahierTextIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await cahier_service.update_text(
        db,
        _parse_day(day),
        message=payload.message,
        operation=payload.operation,
        fields=payload.model_fields_set,
        user_id=user.id,
    )


@router.put("/{day}/signature")
async def put_cahier_signature(
    day: str,
    payload: CahierSignatureIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await cahier_service.sign(
        db, _parse_day(day), role=payload.role, name=payload.name, user=user
    )
