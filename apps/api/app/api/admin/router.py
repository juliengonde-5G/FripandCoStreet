# Extrait de l'application source (apps/api/app/api/admin/router.py) — perimetre reduit a
# la lecture du JET (compte unique : tout utilisateur authentifie a acces
# admin, pas de RoleChecker). PR2 (docs/ARCHITECTURE_PR2.md §4.7) ajoute les
# parametres boutique (`app_settings`) et le controle d'integrite fiscal.
# PR3 (docs/ARCHITECTURE_PR3.md §4) ajoute la fiche client/RGPD, l'etat de
# la messagerie et `dpo_email` sur `shop`.
# PR3b (impression tickets, décision Julien) ajoute la clé `hardware`
# (imprimante MUNBYN 047P + tiroir Safescan SD-4141) — jamais de secret,
# uniquement de la config réseau/USB (voir app/services/escpos_service.py).
import ipaddress
import re
import uuid
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.client import Client, ConsentPurpose, ConsentSource
from app.models.jet import JournalEvent
from app.models.user import User
from app.services import brevo_contacts, email_gateway
from app.services.client_service import ClientService
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
# l'application source : stocke en base, toute ecriture journalisee au JET.
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
    # PR3 (E8) — contact DPO affiché sur l'écran de saisie client et dans le
    # paragraphe RGPD de l'e-mail du ticket (`services/receipt_email.py`).
    dpo_email: str = ""

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


class HardwareSettingsIn(BaseModel):
    """Réglages matériel (PR3b, décision Julien) — imprimante ticket MUNBYN
    047P (réseau TCP 9100 ou WebUSB depuis la tablette) et tiroir-caisse
    Safescan SD-4141 branché dessus. Aucun secret : uniquement de la
    config réseau/USB, comme `shop`/`fiscal`/`receipt`."""

    printer_mode: Literal["network", "webusb", "none"] = "none"
    printer_host: str = ""
    printer_port: int = Field(default=9100, ge=1, le=65535)
    drawer_enabled: bool = False
    drawer_pin: Literal[0, 1] = 0
    auto_print_on_sale: bool = False
    auto_kick_on_cash: bool = False

    @field_validator("printer_host")
    @classmethod
    def _validate_printer_host(cls, value: str) -> str:
        value = (value or "").strip()
        if value:
            try:
                ipaddress.IPv4Address(value)
            except ValueError:
                raise ValueError(
                    "Adresse IP de l'imprimante invalide (format IPv4 attendu, ex. 192.168.1.50)"
                )
        return value

    @model_validator(mode="after")
    def _validate_network_requires_host(self) -> "HardwareSettingsIn":
        if self.printer_mode == "network" and not self.printer_host:
            raise ValueError(
                "Adresse IP requise en mode réseau (printer_mode=network)"
            )
        return self


_SETTINGS_SCHEMAS: dict[str, type[BaseModel]] = {
    "shop": ShopSettingsIn,
    "fiscal": FiscalSettingsIn,
    "receipt": ReceiptSettingsIn,
    "hardware": HardwareSettingsIn,
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


# ---------------------------------------------------------------------------
# Clients — fiche, consentements, RGPD (PR3, §4 ARCHITECTURE_PR3.md)
# ---------------------------------------------------------------------------


class AdminConsentIn(BaseModel):
    purpose: Literal["newsletter"] = "newsletter"
    granted: bool
    note: str | None = None


class AdminAnonymizeIn(BaseModel):
    reason: str = Field(min_length=3)


def _serialize_client_summary(client: Client) -> dict:
    return {
        "id": str(client.id),
        "email": client.email,
        "first_name": client.first_name,
        "last_name": client.last_name,
        "newsletter_optin": client.newsletter_optin,
        "created_at": client.created_at.isoformat() if client.created_at else None,
        "anonymized_at": client.anonymized_at.isoformat() if client.anonymized_at else None,
    }


@router.get("/clients")
async def list_clients(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    clients = await ClientService(db).search(q, limit=limit)
    return {"clients": [_serialize_client_summary(c) for c in clients]}


@router.get("/clients/{client_id}")
async def get_client(
    client_id: uuid.UUID,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await ClientService(db).get_by_id(client_id)
    if client is None:
        raise PosServiceError("Client introuvable.", code="not_found", status_code=404)
    return await ClientService(db).get_full(client)


@router.post("/clients/{client_id}/consents")
async def add_client_consent(
    client_id: uuid.UUID,
    body: AdminConsentIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await ClientService(db).get_by_id(client_id)
    if client is None:
        raise PosServiceError("Client introuvable.", code="not_found", status_code=404)
    clients = ClientService(db)
    purpose = ConsentPurpose(body.purpose)
    await clients.record_consent(
        client=client,
        purpose=purpose,
        granted=body.granted,
        source=ConsentSource.admin,
        user_id=user.id,
        note=body.note,
    )
    if purpose == ConsentPurpose.newsletter:
        # Revue RGPD : « Retirer de la newsletter »/« Inscrire (demande
        # orale) » doit refléter l'état sur la liste Brevo dédiée (push si
        # opt-in, retrait sinon) — best-effort, jamais bloquant.
        await clients.sync_brevo(client, user_id=user.id)
    await db.commit()
    return await clients.get_full(client)


@router.post("/clients/{client_id}/anonymize")
async def anonymize_client(
    client_id: uuid.UUID,
    body: AdminAnonymizeIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await ClientService(db).get_by_id(client_id)
    if client is None:
        raise PosServiceError("Client introuvable.", code="not_found", status_code=404)
    # L'e-mail original doit être capturé AVANT l'anonymisation (E4) :
    # `anonymize()` le remplace par une adresse `@anonyme.invalid`, ce
    # n'est donc plus l'adresse à retirer côté Brevo.
    original_email = client.email
    await ClientService(db).anonymize(client=client, user_id=user.id, reason=body.reason)
    if original_email:
        # Best-effort — jamais `DELETE /v3/contacts` ni `emailBlacklisted`
        # (E1) : seule l'appartenance à la liste Fripco dédiée change.
        await brevo_contacts.remove_from_list(original_email)
    await db.commit()
    return await ClientService(db).get_full(client)


@router.get("/clients/{client_id}/export")
async def export_client(
    client_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await ClientService(db).get_by_id(client_id)
    if client is None:
        raise PosServiceError("Client introuvable.", code="not_found", status_code=404)
    data = await ClientService(db).export(client, user_id=user.id)
    await db.commit()
    return data


# ---------------------------------------------------------------------------
# Messagerie — état des fournisseurs, aucun secret (PR3, §4)
# ---------------------------------------------------------------------------


@router.get("/messaging/status")
async def messaging_status(
    _user: Annotated[User, Depends(get_current_user)],
    _db: Annotated[AsyncSession, Depends(get_db)],
):
    return {
        "email": email_gateway.describe_active_provider(),
        "brevo_contacts": brevo_contacts.describe(),
    }
