import pytest
from sqlalchemy import select, text

from app.core.database import async_session, engine
from app.core.config import settings
from app.models.jet import JournalEvent
from app.services.jet import JournalService

pytestmark = pytest.mark.anyio


async def test_recording_events_chains_previous_hash():
    async with async_session() as db:
        journal = JournalService(db)
        e1 = await journal.record("auth.login_success", username="a")
        await db.commit()
    async with async_session() as db:
        journal = JournalService(db)
        e2 = await journal.record("auth.login_success", username="b")
        await db.commit()
    async with async_session() as db:
        journal = JournalService(db)
        e3 = await journal.record("auth.login_success", username="c")
        await db.commit()

    assert e1.seq == 1
    assert e1.previous_hash == "0"
    assert e2.seq == 2
    assert e2.previous_hash == e1.hash
    assert e3.seq == 3
    assert e3.previous_hash == e2.hash
    # Chaque evenement a un hash distinct (pas de collision triviale)
    assert len({e1.hash, e2.hash, e3.hash}) == 3


async def test_verify_chain_is_valid_after_clean_writes():
    async with async_session() as db:
        journal = JournalService(db)
        await journal.record("auth.login_success", username="a")
        await journal.record("auth.login_success", username="b")
        await db.commit()

    async with async_session() as db:
        result = await JournalService(db).verify_chain()
    assert result["valid"] is True
    assert result["count"] == 2
    assert result["first_invalid_seq"] is None
    assert result["errors"] == []


async def test_two_successful_records_are_sequential_without_gap():
    async with async_session() as db:
        e1 = await JournalService(db).record("system.startup", payload={})
        await db.commit()
    async with async_session() as db:
        e2 = await JournalService(db).record("system.startup", payload={})
        await db.commit()
    assert (e1.seq, e2.seq) == (1, 2)


async def test_tampering_payload_via_raw_sql_is_refused_by_trigger():
    async with async_session() as db:
        await JournalService(db).record("auth.login_success", username="a")
        await db.commit()

    with pytest.raises(Exception) as exc_info:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE journal_events SET payload = '{\"tampered\": true}' WHERE seq = 1")
            )
    assert "JET" in str(exc_info.value)

    # La ligne est bien restee intacte (le rollback implicite de `engine.begin()`
    # sur exception a annule la tentative de modification).
    async with async_session() as db:
        event = (
            await db.execute(select(JournalEvent).where(JournalEvent.seq == 1))
        ).scalar_one()
    assert event.payload == {}


async def test_delete_is_refused_by_trigger():
    async with async_session() as db:
        await JournalService(db).record("auth.login_success", username="a")
        await db.commit()

    with pytest.raises(Exception) as exc_info:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM journal_events WHERE seq = 1"))
    assert "JET" in str(exc_info.value)


async def test_verify_chain_fails_with_wrong_signing_key(monkeypatch):
    async with async_session() as db:
        await JournalService(db).record("auth.login_success", username="a")
        await JournalService(db).record("auth.login_success", username="b")
        await db.commit()

    monkeypatch.setattr(settings, "FISCAL_SIGNING_KEY", "une-autre-cle-totalement-differente-xx")

    async with async_session() as db:
        result = await JournalService(db).verify_chain()
    assert result["valid"] is False
    assert result["errors"]
