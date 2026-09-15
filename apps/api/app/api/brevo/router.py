# Nouveau routeur (PR3, docs/ARCHITECTURE_PR3.md §4/E9) — webhook public
# Brevo -> Fripco. Authentifie par token partage (`BREVO_WEBHOOK_TOKEN`),
# PAS par JWT (Brevo ne porte pas de Bearer applicatif) : sans token
# configure, 403 (fail-closed). Meme patron d'authentification que le
# module equivalent de l'application source (`api/brevo/router.py`), adapte
# a `settings` (pydantic-settings) plutot que
# `os.getenv` direct.
from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.services import brevo_contacts
from app.services.fiscal import PosServiceError
from app.services.jet import EVENT_BREVO_WEBHOOK_RECEIVED, JournalService

router = APIRouter(prefix="/brevo", tags=["brevo"])
_log = logging.getLogger("fripco.brevo.webhook")


class WebhookForbidden(PosServiceError):
    status_code = 403
    code = "webhook_forbidden"


def _check_token(token: str | None, header_token: str | None) -> None:
    expected = (settings.BREVO_WEBHOOK_TOKEN or "").strip()
    if not expected:
        # Pas de token configure => on refuse plutot que de laisser passer
        # n'importe quel evenement (E9). La synchro Brevo -> Fripco (push
        # de contact) reste possible ; seul ce retour est desactive tant
        # que le token n'est pas pose.
        raise WebhookForbidden("Webhook désactivé : configurez BREVO_WEBHOOK_TOKEN.")
    provided = (token or header_token or "").strip()
    # Comparaison en temps constant (revue securite) — un `!=` classique sur
    # deux chaines fuit sa duree d'execution selon le prefixe commun, ce qui
    # permet en theorie de deviner le token octet par octet par timing.
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise WebhookForbidden("Token webhook invalide.")


@router.post("/webhook")
async def webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: str | None = Query(default=None),
    x_brevo_token: Annotated[str | None, Header(alias="X-Brevo-Token")] = None,
):
    """Reçoit un évènement Brevo (JSON, objet ou liste) et applique la
    révocation de consentement correspondante (§3, `brevo_contacts.
    apply_webhook_event`)."""
    _check_token(token, x_brevo_token)
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"JSON invalide : {exc}") from exc

    events = payload if isinstance(payload, list) else [payload]
    applied = 0
    skipped = 0
    for evt in events:
        if not isinstance(evt, dict):
            skipped += 1
            continue
        try:
            result = await brevo_contacts.apply_webhook_event(db, evt)
        except Exception as exc:  # noqa: BLE001 — un evenement invalide ne doit pas faire echouer les autres
            _log.warning("Évènement webhook Brevo ignoré : %s", exc)
            skipped += 1
            continue
        if result.get("applied"):
            applied += 1
        else:
            skipped += 1

    await JournalService(db).record(
        EVENT_BREVO_WEBHOOK_RECEIVED,
        payload={"applied": applied, "skipped": skipped, "total": len(events)},
    )
    await db.commit()
    return {"applied": applied, "skipped": skipped}
