# Extrait de l'application source (apps/api/app/core/middleware.py) — AuditContextMiddleware
# retire (pas de piste d'audit ORM generique ici, seul le JET fait foi).
"""Middlewares HTTP pour le suivi des requetes et les en-tetes de securite."""

from __future__ import annotations

import logging
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("fripco")

# ---------------------------------------------------------------------------
# Tampon des dernieres erreurs 500 (PR12, N5/N2) — alimente la carte
# « Dernieres erreurs » de la supervision.
#
# En memoire de processus, volontairement : c'est une aide au diagnostic a
# chaud (« la vendeuse vient d'avoir une erreur, quelle reference ? »), pas
# une piste d'audit. Le JET reste le seul journal durable, et ecrire en base
# depuis la frontiere d'erreur reviendrait a tenter une ecriture au moment
# precis ou la base est peut-etre la cause de la panne.
#
# On n'y met JAMAIS le corps de la reponse ni celui de la requete : une
# erreur peut survenir sur une route qui porte un e-mail client, et ce
# tampon est relu par une route d'administration (CLAUDE.md : aucune donnee
# personnelle hors des tables prevues).
# ---------------------------------------------------------------------------

RECENT_ERRORS_MAX = 50
_recent_errors: deque[dict] = deque(maxlen=RECENT_ERRORS_MAX)


def record_recent_error(
    *,
    request_id: str,
    method: str,
    path: str,
    status: int,
    error_type: str | None,
) -> None:
    _recent_errors.append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "method": method,
            "path": path,
            "status": status,
            "error_type": error_type,
        }
    )


def recent_errors() -> list[dict]:
    """Copie du tampon, la plus recente d'abord (lecture seule)."""
    return [dict(item) for item in reversed(_recent_errors)]


def reset_recent_errors() -> None:
    """Vide le tampon — tests uniquement (le processus vit sinon en continu)."""
    _recent_errors.clear()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attache un `X-Request-ID` UUID a chaque requete + ligne de log d'acces.

    L'ID est expose sur `request.state.request_id` pour que les handlers
    puissent l'inclure dans leurs reponses d'erreur.

    Frontiere d'erreur : ce middleware est le plus interne, donc celui qui
    enveloppe le plus etroitement l'endpoint. Si l'endpoint leve une
    exception non geree, la laisser se propager a travers la pile
    ``BaseHTTPMiddleware`` corrompt la reponse (le socket est reinitialise,
    le navigateur voit un "Failed to fetch" trompeur au lieu d'un 500
    lisible). On l'attrape donc ICI et on renvoie un ``JSONResponse`` propre
    (avec le ``request_id`` pour correler logs serveur et erreur client) qui
    repart a travers les middlewares restants (securite -> CORS), pour que
    les en-tetes CORS soient presents et que la connexion se ferme proprement.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        from app.core.config import settings

        incoming = request.headers.get("x-request-id")
        request_id = incoming if incoming else uuid.uuid4().hex[:16]
        request.state.request_id = request_id

        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.error(
                "[%s] Unhandled %s on %s %s (%dms): %s\n%s",
                request_id,
                type(exc).__name__,
                request.method,
                request.url.path,
                duration_ms,
                exc,
                traceback.format_exc(),
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                    "error_type": type(exc).__name__,
                },
            )
            if settings.is_production:
                detail = "Une erreur interne est survenue."
            else:
                detail = f"{type(exc).__name__}: {exc}"
            err_response: Response = JSONResponse(
                status_code=500,
                content={
                    "detail": detail,
                    "request_id": request_id,
                    "error_type": type(exc).__name__,
                },
            )
            err_response.headers["x-request-id"] = request_id
            record_recent_error(
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status=500,
                error_type=type(exc).__name__,
            )
            return err_response

        duration_ms = int((time.perf_counter() - start) * 1000)
        response.headers["x-request-id"] = request_id

        # Une 5xx peut aussi arriver sans exception remontee jusqu'ici (un
        # endpoint qui renvoie deliberement un 502, le filet global de
        # `main.py`...). Elle merite la meme trace : c'est la reference
        # affichee a la vendeuse qui doit permettre de la retrouver.
        if response.status_code >= 500:
            record_recent_error(
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                error_type=None,
            )

        if request.url.path != "/api/health" or response.status_code >= 400:
            logger.info(
                "%s %s -> %d (%dms)",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Ajoute les en-tetes de securite de base a chaque reponse."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=()",
        )
        return response
