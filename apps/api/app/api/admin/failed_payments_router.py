# Nouveau routeur (PR9, docs/ARCHITECTURE_PR9.md, contrat K3) — file des
# paiements carte échoués côté administration.
#
# Deux gestes seulement, et aucun n'encaisse : consulter la file, et fermer
# à la main une ligne dont on sait qu'elle ne sera jamais rejouée (« la
# cliente est repartie », « encaissé en espèces »). Le réessai, lui, vit en
# caisse (`POST /pos/payments/cb/retry-failed/{id}`) : il faut un terminal et
# une cliente devant, pas un écran d'admin.
#
# Fichier séparé de `api/admin/router.py` : le journal des échanges SumUp
# (K2) occupe déjà son propre module (`payments_router.py`), et la file a le
# même cycle de vie — un bloc PR9 autonome, retirable d'un bloc.
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services import failed_payment_service

router = APIRouter(prefix="/admin", tags=["admin", "payments"])


class AbandonIn(BaseModel):
    # Motif libre, borné ici ET dans le service (le front borne déjà la
    # saisie ; on ne lui fait pas confiance pour autant).
    reason: str = Field(min_length=1, max_length=failed_payment_service.ABANDON_REASON_MAX_LEN)


@router.get("/failed-payments")
async def list_failed_payments(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    """File des paiements carte échoués, du plus récent au plus ancien.

    `status` vide = tous les statuts (l'écran admin demande `pending` par
    défaut, mais l'historique des lignes abandonnées ou épuisées reste
    consultable).
    """
    rows, total = await failed_payment_service.list_queue(db, status, limit=limit)
    failed_payments = []
    for row in rows:
        checkout_id = await failed_payment_service.checkout_id_of(db, row)
        failed_payments.append(failed_payment_service.serialize(row, checkout_id=checkout_id))
    return {"failed_payments": failed_payments, "total": total}


@router.post("/failed-payments/{failed_payment_id}/abandon")
async def abandon_failed_payment(
    failed_payment_id: uuid.UUID,
    body: AbandonIn,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Ferme une ligne à la main : elle ne sera pas rejouée (motif journalisé)."""
    failed_payment = await failed_payment_service.get_or_404(db, failed_payment_id)
    failed_payment = await failed_payment_service.abandon(
        db,
        failed_payment,
        body.reason,
        user_id=_user.id,
        username=_user.username,
    )
    await db.commit()
    # La ligne mise à jour, telle quelle (pas d'enveloppe) : l'écran
    # d'administration remplace la ligne qu'il affichait par celle-ci.
    return await failed_payment_service.snapshot(db, failed_payment)
