# Nouveau test (PR3, §7 ARCHITECTURE_PR3.md, E5) — registre de consentement
# append-only : UPDATE/DELETE interdits par `trg_protect_consent`
# (migration 0003), consentement idempotent depuis le POS.
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import async_session, engine
from app.models.client import Client, Consent, ConsentPurpose, ConsentSource
from app.services.client_service import ClientService
from app.version import CONSENT_POLICY_VERSION

pytestmark = pytest.mark.anyio


async def _make_client(email: str) -> uuid.UUID:
    async with async_session() as db:
        c = Client(email=email)
        db.add(c)
        await db.commit()
        await db.refresh(c)
        return c.id


async def _make_consent(client_id: uuid.UUID) -> uuid.UUID:
    async with async_session() as db:
        entry = Consent(
            client_id=client_id,
            purpose=ConsentPurpose.newsletter,
            granted=True,
            source=ConsentSource.pos,
            policy_version=CONSENT_POLICY_VERSION,
        )
        db.add(entry)
        await db.commit()
        await db.refresh(entry)
        return entry.id


async def test_consent_update_rejected_by_trigger():
    client_id = await _make_client("update-refuse@example.com")
    consent_id = await _make_consent(client_id)

    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE consents SET granted = false WHERE id = :id"),
                {"id": consent_id},
            )


async def test_consent_delete_rejected_by_trigger():
    client_id = await _make_client("delete-refuse@example.com")
    consent_id = await _make_consent(client_id)

    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM consents WHERE id = :id"), {"id": consent_id})

    async with async_session() as db:
        assert (
            await db.execute(select(Consent).where(Consent.id == consent_id))
        ).scalar_one()


async def test_revoking_consent_appends_a_new_row_rather_than_mutating():
    async with async_session() as db:
        client = Client(email="revoque@example.com")
        db.add(client)
        await db.flush()
        service = ClientService(db)
        await service.record_consent(
            client=client,
            purpose=ConsentPurpose.newsletter,
            granted=True,
            source=ConsentSource.pos,
            user_id=None,
        )
        await service.record_consent(
            client=client,
            purpose=ConsentPurpose.newsletter,
            granted=False,
            source=ConsentSource.admin,
            user_id=None,
        )
        await db.commit()
        client_id = client.id

    async with async_session() as db:
        rows = (
            await db.execute(
                select(Consent).where(Consent.client_id == client_id).order_by(Consent.created_at.asc())
            )
        ).scalars().all()
        assert [r.granted for r in rows] == [True, False]
        refreshed = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one()
        assert refreshed.newsletter_optin is False


async def test_pos_consent_is_idempotent_when_state_unchanged():
    async with async_session() as db:
        client = Client(email="idempotent@example.com")
        db.add(client)
        await db.flush()
        service = ClientService(db)
        first = await service.record_consent(
            client=client, purpose=ConsentPurpose.newsletter, granted=True,
            source=ConsentSource.pos, user_id=None,
        )
        second = await service.record_consent(
            client=client, purpose=ConsentPurpose.newsletter, granted=True,
            source=ConsentSource.pos, user_id=None,
        )
        await db.commit()
        client_id = client.id

    assert first is not None
    assert second is None  # pas de nouvelle ligne — meme etat, meme source pos

    async with async_session() as db:
        rows = (
            await db.execute(select(Consent).where(Consent.client_id == client_id))
        ).scalars().all()
        assert len(rows) == 1


async def test_admin_consent_always_writes_even_if_state_unchanged():
    """Contrairement a `pos`, une action `admin` ecrit toujours une ligne
    (piste d'audit explicite d'une action manuelle), meme redondante."""
    async with async_session() as db:
        client = Client(email="admin-toujours@example.com")
        db.add(client)
        await db.flush()
        service = ClientService(db)
        await service.record_consent(
            client=client, purpose=ConsentPurpose.newsletter, granted=True,
            source=ConsentSource.admin, user_id=None,
        )
        await service.record_consent(
            client=client, purpose=ConsentPurpose.newsletter, granted=True,
            source=ConsentSource.admin, user_id=None,
        )
        await db.commit()
        client_id = client.id

    async with async_session() as db:
        rows = (
            await db.execute(select(Consent).where(Consent.client_id == client_id))
        ).scalars().all()
        assert len(rows) == 2
