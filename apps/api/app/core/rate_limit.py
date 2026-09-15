# Extrait de l'application source (apps/api/app/core/rate_limit.py) — implementation
# en memoire uniquement (le backend Redis a ete retire : mono-conteneur,
# mono-utilisateur, pas de partage de compteur entre workers necessaire).
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from app.core.config import settings

_buckets: dict[str, deque[float]] = defaultdict(deque)
_lock = asyncio.Lock()


def _raise_429(attempts: int, window_seconds: int, retry_after: int) -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Trop de tentatives. Réessayez dans {retry_after} secondes.",
        headers={"Retry-After": str(retry_after)},
    )


async def _check(key: str, max_attempts: int, window_seconds: int) -> int:
    now = time.monotonic()
    cutoff = now - window_seconds
    async with _lock:
        bucket = _buckets[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_attempts:
            retry_after = max(1, int(bucket[0] + window_seconds - now))
            _raise_429(len(bucket), window_seconds, retry_after)
        bucket.append(now)
        return max_attempts - len(bucket)


async def _reset(key: str) -> None:
    async with _lock:
        _buckets.pop(key, None)


def _client_key(request: Request, prefix: str) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "unknown"
    )
    return f"{prefix}:{ip}"


async def login_rate_limit(request: Request) -> None:
    """Dependance FastAPI : limite le debit de /auth/login par IP client."""
    key = _client_key(request, "login")
    await _check(
        key,
        max_attempts=settings.LOGIN_RATE_LIMIT_ATTEMPTS,
        window_seconds=settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS,
    )


async def reset_login_rate_limit(request: Request) -> None:
    """Reinitialise le compteur du client courant (appele au login reussi)."""
    key = _client_key(request, "login")
    await _reset(key)


# ---------------------------------------------------------------------------
# Code PIN des vendeuses (PR8, docs/ARCHITECTURE_PR8.md J2) — 5 essais / 5
# min PAR VENDEUSE ET PAR IP. Compteur distinct de celui du login manager
# (cle prefixee differemment) : bloquer une vendeuse qui se trompe de code
# ne doit jamais bloquer la connexion du manager depuis le meme poste, et
# reciproquement. Valeurs figees ici plutot que dans `Settings` : ce sont
# des essais sur un code a 4 chiffres, pas un reglage d'exploitation.
# ---------------------------------------------------------------------------

CASHIER_PIN_RATE_LIMIT_ATTEMPTS = 5
CASHIER_PIN_RATE_LIMIT_WINDOW_SECONDS = 300


def _cashier_pin_key(cashier_id: str, ip: str | None) -> str:
    return f"cashier_pin:{ip or 'unknown'}:{cashier_id}"


async def cashier_pin_rate_limit(cashier_id: str, ip: str | None) -> None:
    """Compte un essai de code PIN ; leve 429 (avec `Retry-After`) au-dela.

    Prend l'IP deja extraite par l'appelant (et non la `Request`) : la
    verification a lieu dans le service, pas dans une dependance FastAPI,
    parce que l'echec doit d'abord etre journalise au JET.
    """
    await _check(
        _cashier_pin_key(cashier_id, ip),
        max_attempts=CASHIER_PIN_RATE_LIMIT_ATTEMPTS,
        window_seconds=CASHIER_PIN_RATE_LIMIT_WINDOW_SECONDS,
    )


async def reset_cashier_pin_rate_limit(cashier_id: str, ip: str | None) -> None:
    """Reinitialise le compteur (appele sur une identification reussie)."""
    await _reset(_cashier_pin_key(cashier_id, ip))
