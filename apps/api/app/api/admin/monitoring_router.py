# Nouveau bloc (PR12, docs/ARCHITECTURE_PR12.md §1, N2) — supervision
# technique. Monte a part du routeur d'administration historique (meme
# prefixe `/admin`), comme les blocs paiements et file de paiements echoues :
# c'est un sujet autonome, qui ne partage rien avec le parametrage boutique.
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services import monitoring

router = APIRouter(prefix="/admin", tags=["admin", "monitoring"])


@router.get("/monitoring")
async def get_monitoring(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    check: int = Query(default=0, ge=0, le=1),
):
    """Photographie technique de l'installation (N2).

    Lecture seule par defaut : le rafraichissement automatique de l'ecran
    (60 s) ne doit rien ecrire ni rien recalculer de couteux. `?check=1`
    force le recalcul des trois chaines d'integrite (et laisse la trace JET
    `fiscal.integrity_checked`).
    """
    return await monitoring.build_snapshot(db, check=bool(check), user_id=user.id)


@router.post("/monitoring/check")
async def run_monitoring_check(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Bouton « Verifier maintenant » : recalcule les integrites et renvoie
    la photographie complete, pour que l'ecran se remette a jour d'un seul
    aller-retour."""
    return await monitoring.build_snapshot(db, check=True, user_id=user.id)
