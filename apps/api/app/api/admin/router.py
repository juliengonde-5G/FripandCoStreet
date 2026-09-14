# Extrait de Vintiz (apps/api/app/api/admin/router.py) — perimetre reduit a
# la lecture du JET (compte unique : tout utilisateur authentifie a acces
# admin, pas de RoleChecker). PR2 (docs/ARCHITECTURE_PR2.md §4.7) ajoute les
# parametres boutique (`app_settings`) et le controle d'integrite fiscal.
import re
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.jet import JournalEvent
from app.models.user import User
from app.services.fiscal import FiscalService, PosServiceError
from app.services.jet import EVENT_FISCAL_INTEGRITY_CHECKED, JournalService
from app.services.settings_service import SettingsService
from app.services.tva_service import SUPPORTED_TVA_RATES

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

    `next_before_seq` porte le curseur de pagination du front : le `seq` du
    dernier evenement de cette page (le plus ancien, l'ordre etant
    decroissant), a repasser en `before_seq` pour la page suivante — ou
    `null` quand cette page n'est pas pleine (`limit`), preuve qu'il n'y a
    plus rien au-dela.
    """
    query = select(JournalEvent).order_by(JournalEvent.seq.desc())
    if before_seq is not None:
        query = query.where(JournalEvent.seq < before_seq)
    events = (await db.execute(query.limit(limit))).scalars().all()
    next_before_seq = events[-1].seq if len(events) == limit else None
    return {
        "events": [_serialize(e) for e in events],
        "count": len(events),
        "next_before_seq": next_before_seq,
    }


@router.get("/jet/integrity")
async def journal_integrity(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Verifie l'integrite complete de la chaine JET (recalcul des hash)."""
    return await JournalService(db).verify_chain()


# ---------------------------------------------------------------------------
# Parametrage boutique (PR2, D13, §4.7) — remplace `data/app_config.json` de
# Vintiz : stocke en base, toute ecriture journalisee au JET.
# ---------------------------------------------------------------------------

_SIRET_RE = re.compile(r"^\d{14}$")


class ShopSettingsIn(BaseModel):
    name: str = "Frip & Co Street"
    address_line1: str = ""
    address_line2: str = ""
    postal_code: str = ""
    city: str = ""
    siret: str = ""
    vat_number: str = ""
    phone: str = ""
    email: str = ""

    @field_validator("siret")
    @classmethod
    def _validate_siret(cls, value: str) -> str:
        if value and not _SIRET_RE.match(value):
            raise ValueError("SIRET invalide : 14 chiffres attendus")
        return value


class FiscalSettingsIn(BaseModel):
    tva_rate: Decimal

    @field_validator("tva_rate")
    @classmethod
    def _validate_rate(cls, value: Decimal) -> Decimal:
        normalized = value.quantize(Decimal("0.01"))
        if normalized not in SUPPORTED_TVA_RATES:
            allowed = ", ".join(f"{r:.2f}" for r in SUPPORTED_TVA_RATES)
            raise ValueError(f"Taux de TVA non supporté ({value}) — valeurs autorisées : {allowed}")
        return normalized


class ReceiptSettingsIn(BaseModel):
    header_note: str = ""
    footer_note: str = ""
    return_policy: str = ""


_SETTINGS_SCHEMAS: dict[str, type[BaseModel]] = {
    "shop": ShopSettingsIn,
    "fiscal": FiscalSettingsIn,
    "receipt": ReceiptSettingsIn,
}


def _settings_to_json(value: BaseModel) -> dict:
    """Serialise un schema de settings en JSON-compatible (Decimal -> str)."""
    return {
        k: (str(v) if isinstance(v, Decimal) else v) for k, v in value.model_dump().items()
    }


@router.get("/settings/{key}")
async def get_settings(
    key: str,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if key not in _SETTINGS_SCHEMAS:
        raise PosServiceError(f"Paramètre inconnu : {key}", code="unknown_setting", status_code=404)
    return await SettingsService(db).get(key)


@router.put("/settings/{key}")
async def put_settings(
    key: str,
    body: dict,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    schema = _SETTINGS_SCHEMAS.get(key)
    if schema is None:
        raise PosServiceError(f"Paramètre inconnu : {key}", code="unknown_setting", status_code=404)
    try:
        validated = schema(**body)
    except Exception as exc:  # noqa: BLE001 — erreurs Pydantic -> 422 lisible
        raise PosServiceError(
            f"Paramètres invalides : {exc}", code="invalid_setting", status_code=422
        )
    row = await SettingsService(db).set(key, _settings_to_json(validated), user_id=user.id)
    await db.commit()
    return row.value


# ---------------------------------------------------------------------------
# Controle d'integrite fiscal (§4.7, §3)
# ---------------------------------------------------------------------------


@router.get("/fiscal/integrity")
async def fiscal_integrity(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    fiscal = FiscalService(db)
    transactions_result = await fiscal.verify_chain_integrity()
    z_reports_result = await fiscal.verify_z_chain_integrity()
    jet_result = await JournalService(db).verify_chain()
    await JournalService(db).record(
        EVENT_FISCAL_INTEGRITY_CHECKED,
        user_id=user.id,
        payload={
            "transactions_valid": transactions_result["valid"],
            "z_reports_valid": z_reports_result["valid"],
            "jet_valid": jet_result["valid"],
        },
    )
    await db.commit()
    return {"transactions": transactions_result, "z_reports": z_reports_result, "jet": jet_result}


# ---------------------------------------------------------------------------
# Débogage TPE (§4.7) — journal des tentatives de paiement CB.
# ---------------------------------------------------------------------------


@router.get("/payments/cb/attempts")
async def list_payment_attempts(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    from app.models.payment_attempt import PaymentAttempt, PaymentAttemptStatus

    query = select(PaymentAttempt).order_by(PaymentAttempt.created_at.desc())
    if status:
        try:
            query = query.where(PaymentAttempt.status == PaymentAttemptStatus(status))
        except ValueError:
            raise PosServiceError(f"Statut inconnu : {status}", code="invalid_status", status_code=422)
    rows = (await db.execute(query.limit(limit))).scalars().all()
    return {
        "attempts": [
            {
                "id": str(a.id),
                "client_uuid": str(a.client_uuid),
                "amount": float(a.amount),
                "status": a.status.value,
                "checkout_id": a.checkout_id,
                "reader_id": a.reader_id,
                "error_message": a.error_message,
                "transaction_id": str(a.transaction_id) if a.transaction_id else None,
                "attempt_count": a.attempt_count,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in rows
        ]
    }
