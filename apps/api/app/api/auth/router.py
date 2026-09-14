# Extrait de Vintiz (apps/api/app/api/auth/router.py, L46-109) — magic-link
# client retire (pas d'espace client public ici), /me + /logout ajoutes,
# emission JET sur chaque evenement d'authentification.
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limit import login_rate_limit, reset_login_rate_limit
from app.core.security import (
    _uuid_subject,
    create_access_token,
    get_current_user,
    oauth2_scheme,
    verify_password,
    verify_token,
)
from app.models.user import User
from app.services.jet import (
    EVENT_LOGIN_FAILED,
    EVENT_LOGIN_RATE_LIMITED,
    EVENT_LOGIN_SUCCESS,
    EVENT_LOGOUT,
    EVENT_TOKEN_REFRESH,
    JournalService,
)

logger = logging.getLogger("fripco")
router = APIRouter(prefix="/auth", tags=["auth"])


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str


class MeResponse(BaseModel):
    id: str
    username: str
    email: str


def _client_ip(request: Request) -> str | None:
    """IP client en tenant compte de X-Forwarded-For (Caddy est en frontal)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


async def _login_rate_limit_with_jet(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Enveloppe `login_rate_limit` pour journaliser un depassement au JET.

    Le 429 est leve par `login_rate_limit` AVANT que le handler `login` ne
    s'execute — cette dependance est donc celle reellement attachee a la
    route : elle delegue au limiteur generique et, uniquement s'il leve,
    journalise `auth.login_rate_limited` avant de re-lever. Ce choix garde
    `app.core.rate_limit` generique et reutilisable, sans aucun couplage au
    JET ou au domaine metier. Le commit explicite est necessaire : sans lui,
    l'exception remontant a travers la dependance `get_db` provoquerait un
    ROLLBACK qui effacerait l'evenement qu'on vient d'ecrire.
    """
    try:
        await login_rate_limit(request)
    except HTTPException:
        journal = JournalService(db)
        await journal.record(
            EVENT_LOGIN_RATE_LIMITED,
            ip=_client_ip(request),
            request_id=_request_id(request),
            payload={},
        )
        await db.commit()
        raise


@router.post("/login", response_model=Token, dependencies=[Depends(_login_rate_limit_with_jet)])
async def login(
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Authentifie l'utilisateur (username + mot de passe) et retourne un JWT.

    Rate-limite par IP client via `_login_rate_limit_with_jet`.
    """
    journal = JournalService(db)
    result = await db.execute(select(User).where(User.username == form_data.username))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(form_data.password, user.hashed_password):
        # Message generique anti-enumeration : meme reponse que l'utilisateur
        # existe ou non.
        await journal.record(
            EVENT_LOGIN_FAILED,
            username=form_data.username,
            ip=_client_ip(request),
            request_id=_request_id(request),
            payload={"reason": "invalid_credentials"},
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        await journal.record(
            EVENT_LOGIN_FAILED,
            user_id=user.id,
            username=user.username,
            ip=_client_ip(request),
            request_id=_request_id(request),
            payload={"reason": "account_disabled"},
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )

    # Reinitialise le compteur de rate-limit sur un login reussi
    await reset_login_rate_limit(request)
    await journal.record(
        EVENT_LOGIN_SUCCESS,
        user_id=user.id,
        username=user.username,
        ip=_client_ip(request),
        request_id=_request_id(request),
    )
    await db.commit()

    access_token = create_access_token(data={"sub": str(user.id)})
    return Token(access_token=access_token, username=user.username)


@router.post("/refresh", response_model=Token)
async def refresh_token(
    request: Request,
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Accepte un token valide et en retourne un nouveau avec une expiration fraiche."""
    payload = verify_token(token)
    # `_uuid_subject` valide le format UUID et leve 401 (jamais 500) si `sub`
    # est absent ou n'est pas un UUID valide — un `sub` non-UUID envoye tel
    # quel a `User.id == user_id` remonterait une erreur DB brute (500).
    user_id = _uuid_subject(payload)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    new_token = create_access_token(data={"sub": str(user.id)})
    journal = JournalService(db)
    await journal.record(
        EVENT_TOKEN_REFRESH,
        user_id=user.id,
        username=user.username,
        ip=_client_ip(request),
        request_id=_request_id(request),
    )
    await db.commit()
    return Token(access_token=new_token, username=user.username)


@router.get("/me", response_model=MeResponse)
async def me(user: Annotated[User, Depends(get_current_user)]):
    """Retourne l'identite de l'utilisateur authentifie."""
    return MeResponse(id=str(user.id), username=user.username, email=user.email)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Deconnexion.

    Le JWT est sans etat (stateless) : cette route n'invalide aucun token
    cote serveur (le client doit simplement l'oublier). Son seul effet
    reel est d'ecrire un evenement `auth.logout` dans le JET.
    """
    journal = JournalService(db)
    await journal.record(
        EVENT_LOGOUT,
        user_id=user.id,
        username=user.username,
        ip=_client_ip(request),
        request_id=_request_id(request),
    )
    await db.commit()
    return None
