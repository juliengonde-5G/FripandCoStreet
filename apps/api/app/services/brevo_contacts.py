# Extrait de Vintiz (apps/api/app/services/brevo_contacts.py), fortement
# reduit et adapte aux regles E1/E2 du contrat (docs/ARCHITECTURE_PR3.md) :
# le compte Brevo est PARTAGE avec Vintiz Vernon (Julien), donc ce module
# n'ecrit JAMAIS sur la blocklist globale d'un contact (`emailBlacklisted`)
# ni ne le supprime (`DELETE /v3/contacts`) — uniquement sur la liste
# dediee `BREVO_LIST_ID` (`listIds`/`unlinkListIds`) et les attributs
# PRENOM/NOM. Retire : synchro SMS (pas de SMS, cf. CLAUDE.md), listes
# multiples, `push_all_clients` (pas de synchro de masse en PR3).
#
# Client HTTP : `httpx.AsyncClient`, point d'injection `_transport`
# module-level pour les tests — meme patron que
# `SumUpService._transport`/`app.services.email_gateway._transport`.
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.client import Client, Consent, ConsentPurpose, ConsentSource
from app.services.jet import EVENT_CONSENT_REVOKED, JournalService
from app.version import CONSENT_POLICY_VERSION

_log = logging.getLogger("fripco.brevo.contacts")

# Override de transport httpx pour les tests — ``None`` = reseau reel.
_transport: httpx.BaseTransport | None = None

# Evenements webhook Brevo geres (E9) — tout autre type est ignore
# silencieusement (open/click/delivered... : Brevo gere son propre tracking).
_HANDLED_WEBHOOK_EVENTS = {"unsubscribed", "unsubscribe", "hard_bounce", "hardBounce", "contact_deleted"}


def _client(timeout: float = 15.0) -> httpx.AsyncClient:
    kwargs: dict = {"timeout": timeout}
    if _transport is not None:
        kwargs["transport"] = _transport
    return httpx.AsyncClient(**kwargs)


def _base_url() -> str:
    return (settings.BREVO_API_BASE or "https://api.brevo.com").rstrip("/") + "/v3"


def _headers() -> dict:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": (settings.BREVO_API_KEY or "").strip(),
    }


def _api_key() -> str:
    return (settings.BREVO_API_KEY or "").strip()


def _list_id() -> str:
    return (settings.BREVO_LIST_ID or "").strip()


def is_configured() -> bool:
    return bool(_api_key() and _list_id())


def describe() -> dict:
    """Etat de la synchro Brevo Contacts — AUCUN secret (§4, `GET
    /admin/messaging/status`)."""
    return {
        "configured": bool(_api_key()),
        "list_id_set": bool(_list_id()),
        "webhook_token_set": bool((settings.BREVO_WEBHOOK_TOKEN or "").strip()),
    }


@dataclass
class SyncResult:
    ok: bool
    status_code: int | None = None
    detail: str = ""


async def push_contact(client: Client) -> SyncResult:
    """Pousse (upsert) un contact sur la liste Fripco dediee.

    ``POST /v3/contacts {email, attributes:{PRENOM,NOM}, listIds:[BREVO_LIST_ID],
    updateEnabled:true}`` — 201 (cree) et 204 (mis a jour) sont des succes.
    N'ecrit JAMAIS `emailBlacklisted` (E1) : un client sans consentement
    newsletter n'est simplement jamais poussé (voir l'appelant,
    `ClientService.sync_brevo`).
    """
    if not _api_key():
        return SyncResult(ok=False, detail="BREVO_API_KEY non configurée")
    list_id = _list_id()
    if not list_id:
        return SyncResult(ok=False, detail="BREVO_LIST_ID non configurée")
    if not (client.email or "").strip():
        return SyncResult(ok=False, detail="client sans e-mail")

    payload = {
        "email": client.email,
        "attributes": {
            "PRENOM": (client.first_name or "").strip(),
            "NOM": (client.last_name or "").strip(),
        },
        "listIds": [int(list_id)],
        "updateEnabled": True,
    }
    async with _client() as http_client:
        try:
            resp = await http_client.post(f"{_base_url()}/contacts", json=payload, headers=_headers())
        except httpx.HTTPError as exc:
            return SyncResult(ok=False, detail=f"réseau : {exc}")
    if resp.status_code in (200, 201, 204):
        return SyncResult(ok=True, status_code=resp.status_code)
    _log.warning("Brevo push contact %s → HTTP %d: %s", client.email, resp.status_code, resp.text[:200])
    return SyncResult(ok=False, status_code=resp.status_code, detail=resp.text[:200])


async def remove_from_list(email: str) -> SyncResult:
    """Retire un contact de la liste Fripco dediee — JAMAIS `DELETE
    /v3/contacts` (E1) : le contact reste chez Brevo (compte partagé avec
    Vintiz), seule son appartenance à la liste Fripco change.

    ``POST /v3/contacts/lists/{id}/contacts/remove {emails:[email]}``.
    """
    if not _api_key():
        return SyncResult(ok=False, detail="BREVO_API_KEY non configurée")
    list_id = _list_id()
    if not list_id:
        return SyncResult(ok=False, detail="BREVO_LIST_ID non configurée")
    if not email:
        return SyncResult(ok=False, detail="e-mail vide")

    url = f"{_base_url()}/contacts/lists/{list_id}/contacts/remove"
    async with _client() as http_client:
        try:
            resp = await http_client.post(url, json={"emails": [email]}, headers=_headers())
        except httpx.HTTPError as exc:
            return SyncResult(ok=False, detail=f"réseau : {exc}")
    # 404 = contact déjà absent de la liste (ou du compte) — acceptable.
    if resp.status_code in (200, 201, 204, 404):
        return SyncResult(ok=True, status_code=resp.status_code)
    return SyncResult(ok=False, status_code=resp.status_code, detail=resp.text[:200])


async def apply_webhook_event(
    db: AsyncSession, event: dict, *, user_id=None
) -> dict:
    """Applique un évènement webhook Brevo (E9) : révoque le consentement
    newsletter local (append-only) et met à jour le cache
    `Client.newsletter_optin`. N'écrit RIEN côté Brevo (c'est Brevo qui nous
    informe, pas l'inverse) et ne supprime jamais la ligne client (E4)."""
    kind = (event.get("event") or event.get("type") or "").strip()
    email = (event.get("email") or "").strip().lower()
    if not email or kind not in _HANDLED_WEBHOOK_EVENTS:
        return {"applied": False, "reason": "event_ignored"}

    client = (
        await db.execute(select(Client).where(Client.email == email))
    ).scalar_one_or_none()
    if client is None:
        return {"applied": False, "reason": "client_not_found"}

    if not client.newsletter_optin:
        return {"applied": False, "reason": "already_revoked"}

    client.newsletter_optin = False
    db.add(
        Consent(
            client_id=client.id,
            purpose=ConsentPurpose.newsletter,
            granted=False,
            source=ConsentSource.webhook,
            policy_version=CONSENT_POLICY_VERSION,
            recorded_by_user_id=user_id,
            note=f"Brevo webhook: {kind}",
        )
    )
    await JournalService(db).record(
        EVENT_CONSENT_REVOKED,
        user_id=user_id,
        payload={"client_id": str(client.id), "purpose": "newsletter", "source": "webhook", "event": kind},
    )
    await db.flush()
    return {"applied": True, "client_id": str(client.id), "event": kind}
