# Extrait de Vintiz (apps/api/app/core/database.py)
from collections.abc import AsyncGenerator
from typing import TypeVar

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings

_engine_kwargs: dict = {"pool_pre_ping": True, "echo": False}
if settings.ENVIRONMENT == "test":
    # NullPool : chaque checkout ouvre une connexion asyncpg fraiche plutot
    # que d'en reutiliser une deja liee a une boucle asyncio precedente.
    # Necessaire en test : le plugin pytest `anyio` execute chaque test
    # (async) sur sa PROPRE boucle d'evenements, alors que `engine` est un
    # singleton importe une seule fois pour toute la session de tests — un
    # pool avec connexions persistantes (le defaut) leverait "Future
    # attached to a different loop" des le deuxieme test.
    _engine_kwargs["poolclass"] = NullPool
else:
    _engine_kwargs["pool_size"] = 20
    _engine_kwargs["max_overflow"] = 10

engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependance FastAPI qui fournit une session de base de donnees async."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


_M = TypeVar("_M")


async def get_or_404(
    db: AsyncSession,
    model: type[_M],
    pk,
    *,
    detail: str | None = None,
) -> _M:
    """Recupere une ligne par cle primaire ou leve HTTPException(404)."""
    obj = (await db.execute(select(model).where(model.id == pk))).scalar_one_or_none()
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail or f"{model.__name__} not found",
        )
    return obj
