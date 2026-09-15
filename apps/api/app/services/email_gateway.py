# Extrait de l'application source (apps/api/app/services/email_gateway.py), reduit au
# perimetre PR3 (docs/ARCHITECTURE_PR3.md §3/E6) : un seul type de message
# (le ticket), pas de templates editables, pas de piece jointe, pas de tags
# analytics/idempotency-key. Retire : suivi d'ouverture PAR CONSENTEMENT
# INDIVIDUEL (Fripco n'envoie aucun e-mail marketing — seul le ticket, un
# envoi transactionnel), Twilio (pas de SMS, cf. CLAUDE.md). Conserve la
# garde CNIL "pixel d'ouverture" (E6) : le compte Brevo etant PARTAGE avec
# la boutique de Vernon, un envoi Brevo est refuse tant que `BREVO_ANONYMOUS_TRACKING`
# n'est pas confirme.
#
# Client HTTP : `httpx.AsyncClient`, avec un point d'injection de transport
# (`_transport`, module-level) pour les tests — meme patron que
# `SumUpService._transport` (app/services/sumup_service.py).
from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from app.core.config import settings

logger = logging.getLogger("fripco.email")

BREVO_SEND_PATH = "/v3/smtp/email"

# Override de transport httpx pour les tests (`httpx.MockTransport` injecte
# ici) — ``None`` = reseau reel.
_transport: httpx.BaseTransport | None = None


class EmailDeliveryError(RuntimeError):
    """Un backend est configure mais l'envoi a echoue."""


@dataclass
class EmailMessage:
    to: str
    subject: str
    html: str
    text: str | None = None


@dataclass
class EmailResult:
    provider: str  # "brevo" | "smtp" | "simulated"
    status: str    # "sent" | "failed" | "simulated"
    message_id: str | None = None
    error: str | None = None


def _client(timeout: float = 15.0) -> httpx.AsyncClient:
    kwargs: dict = {"timeout": timeout}
    if _transport is not None:
        kwargs["transport"] = _transport
    return httpx.AsyncClient(**kwargs)


def _strip_html(html: str) -> str:
    """Repli texte brut a minima — utilise uniquement si l'appelant n'a pas
    deja fourni `message.text` (le ticket fournit toujours le sien)."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _brevo_configured() -> bool:
    return bool((settings.BREVO_API_KEY or "").strip())


def _smtp_configured() -> bool:
    return bool(
        (settings.SMTP_HOST or "").strip()
        and (settings.SMTP_USER or "").strip()
        and (settings.SMTP_PASSWORD or "").strip()
    )


def describe_active_provider() -> dict:
    """Etat du fournisseur d'e-mail actif — AUCUN secret (§4, `GET
    /admin/messaging/status`)."""
    provider = "simulated"
    if _brevo_configured():
        provider = "brevo"
    elif _smtp_configured():
        provider = "smtp"
    return {
        "provider": provider,
        "anonymous_tracking": bool(settings.BREVO_ANONYMOUS_TRACKING),
        "from": settings.EMAIL_FROM_ADDRESS or "",
    }


async def _send_via_brevo(message: EmailMessage) -> EmailResult:
    api_key = (settings.BREVO_API_KEY or "").strip()
    if not api_key:
        raise EmailDeliveryError("BREVO_API_KEY non configurée")
    # Garde E6 — pixel d'ouverture CNIL : le compte Brevo est PARTAGÉ avec
    # la boutique de Vernon. Tant que le suivi anonyme n'est pas confirmé, on refuse
    # d'envoyer via Brevo (repli SMTP/simulation décidé par `send_email`).
    if not settings.BREVO_ANONYMOUS_TRACKING:
        raise EmailDeliveryError(
            "BREVO_ANONYMOUS_TRACKING non confirmé — le compte Brevo partagé "
            "doit être basculé en suivi anonyme avant tout envoi (E6)."
        )
    sender_email = settings.EMAIL_FROM_ADDRESS or "noreply@fripco-street.fr"
    sender_name = settings.EMAIL_FROM_NAME or "Frip & Co Street"
    payload = {
        "sender": {"email": sender_email, "name": sender_name},
        "to": [{"email": message.to}],
        "subject": message.subject,
        "htmlContent": message.html,
        "textContent": message.text or _strip_html(message.html),
    }
    url = f"{(settings.BREVO_API_BASE or 'https://api.brevo.com').rstrip('/')}{BREVO_SEND_PATH}"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": api_key,
    }
    async with _client() as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise EmailDeliveryError(f"Brevo réseau : {exc}") from exc
    if resp.status_code >= 400:
        raise EmailDeliveryError(f"Brevo {resp.status_code} : {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return EmailResult(provider="brevo", status="sent", message_id=data.get("messageId"))


def _send_via_smtp(message: EmailMessage) -> EmailResult:
    host = settings.SMTP_HOST
    user = settings.SMTP_USER
    password = settings.SMTP_PASSWORD
    port = int(settings.SMTP_PORT or 587)
    sender = settings.EMAIL_FROM_ADDRESS or user

    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(message.text or _strip_html(message.html), "plain", "utf-8"))
    msg.attach(MIMEText(message.html, "html", "utf-8"))
    msg["Subject"] = message.subject
    msg["From"] = sender
    msg["To"] = message.to

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(sender, message.to, msg.as_string())
    except Exception as exc:  # smtplib leve une famille de sous-classes
        raise EmailDeliveryError(f"SMTP : {exc}") from exc

    return EmailResult(provider="smtp", status="sent")


def _simulate(message: EmailMessage) -> EmailResult:
    logger.info(
        "[email simulé] to=%s subject=%s body_len=%d",
        message.to, message.subject, len(message.html),
    )
    return EmailResult(provider="simulated", status="simulated")


async def send_email(message: EmailMessage) -> EmailResult:
    """Passerelle Brevo -> SMTP -> simulation (E6).

    Un refus de garde CNIL (BREVO_ANONYMOUS_TRACKING absent) est traité
    comme "Brevo indisponible pour cet envoi" : on retombe sur SMTP puis
    simulation, exactement comme si Brevo n'était pas configuré. Un échec
    RÉEL de l'appel Brevo (réseau, 4xx/5xx une fois la garde passée) est en
    revanche renvoyé tel quel (`status="failed"`) — pas de repli silencieux
    qui masquerait une vraie panne du fournisseur configuré.
    """
    if _brevo_configured():
        if not settings.BREVO_ANONYMOUS_TRACKING:
            logger.warning(
                "Brevo configuré mais BREVO_ANONYMOUS_TRACKING absent — "
                "repli SMTP/simulation pour ce ticket (E6)."
            )
        else:
            try:
                return await _send_via_brevo(message)
            except EmailDeliveryError as exc:
                logger.warning("Envoi Brevo échoué : %s", exc)
                return EmailResult(provider="brevo", status="failed", error=str(exc))
    if _smtp_configured():
        try:
            return _send_via_smtp(message)
        except EmailDeliveryError as exc:
            logger.warning("Envoi SMTP échoué : %s", exc)
            return EmailResult(provider="smtp", status="failed", error=str(exc))
    return _simulate(message)
