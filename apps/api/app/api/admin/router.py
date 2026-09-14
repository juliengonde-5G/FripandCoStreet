# Extrait de Vintiz (apps/api/app/api/admin/router.py) — perimetre reduit a
# la lecture du JET (compte unique : tout utilisateur authentifie a acces
# admin, pas de RoleChecker).
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.jet import JournalEvent
from app.models.user import User
from app.services.jet import JournalService

router = APIRouter(prefix="/admin", tags=["admin"])


def _serialize(event: JournalEvent) -> dict:
    return {
        "id": str(event.id),
        "seq": event.seq,
        "event_type": event.event_type,
        "user_id": str(event.user_id) if event.user_id else None,
        "username": event.username,
        "ip": event.ip,
        "request_id": event.request_id,
        "payload": event.payload,
        "previous_hash": event.previous_hash,
        "hash": event.hash,
        "signature_version": event.signature_version,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


@router.get("/jet")
async def list_journal_events(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=100, ge=1, le=500),
    before_seq: int | None = Query(default=None),
):
    """Journal des evenements techniques, du plus recent au plus ancien.

    La lecture du JET n'est volontairement pas elle-meme journalisee (seul
    l'export futur le sera) — pas d'ajout de complexite non demandee ici.
    """
    query = select(JournalEvent).order_by(JournalEvent.seq.desc())
    if before_seq is not None:
        query = query.where(JournalEvent.seq < before_seq)
    events = (await db.execute(query.limit(limit))).scalars().all()
    return {"events": [_serialize(e) for e in events], "count": len(events)}


@router.get("/jet/integrity")
async def journal_integrity(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Verifie l'integrite complete de la chaine JET (recalcul des hash)."""
    return await JournalService(db).verify_chain()
