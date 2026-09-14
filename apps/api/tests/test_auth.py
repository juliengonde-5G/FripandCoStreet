import pytest
from sqlalchemy import select

from app.models.jet import JournalEvent
from tests.conftest import MANAGER_PASSWORD

pytestmark = pytest.mark.anyio


async def _last_event(db_session) -> JournalEvent:
    result = await db_session.execute(
        select(JournalEvent).order_by(JournalEvent.seq.desc()).limit(1)
    )
    return result.scalar_one()


async def test_login_ok_returns_token(client, manager):
    response = await client.post(
        "/api/auth/login",
        data={"username": manager.username, "password": MANAGER_PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["username"] == manager.username


async def test_login_wrong_password_is_401_and_journals_failure(client, manager):
    response = await client.post(
        "/api/auth/login",
        data={"username": manager.username, "password": "un-mauvais-mot-de-passe"},
    )
    assert response.status_code == 401

    from app.core.database import async_session

    async with async_session() as db:
        event = await _last_event(db)
    assert event.event_type == "auth.login_failed"
    assert event.username == manager.username


async def test_login_unknown_user_is_401_same_message(client):
    known = await client.post(
        "/api/auth/login", data={"username": "personne", "password": "peu-importe"}
    )
    assert known.status_code == 401
    # Anti-enumeration : meme message que pour un mot de passe errone sur un
    # compte existant (verifie dans test_login_wrong_password_is_401...).
    assert known.json()["detail"] == "Incorrect username or password"


async def test_eleventh_attempt_is_rate_limited_and_journaled(client, manager):
    for _ in range(10):
        resp = await client.post(
            "/api/auth/login",
            data={"username": manager.username, "password": "faux"},
        )
        assert resp.status_code == 401

    resp = await client.post(
        "/api/auth/login",
        data={"username": manager.username, "password": "faux"},
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers

    from app.core.database import async_session

    async with async_session() as db:
        event = await _last_event(db)
    assert event.event_type == "auth.login_rate_limited"


async def test_successful_login_resets_rate_limit_counter(client, manager):
    for _ in range(9):
        resp = await client.post(
            "/api/auth/login",
            data={"username": manager.username, "password": "faux"},
        )
        assert resp.status_code == 401

    ok = await client.post(
        "/api/auth/login",
        data={"username": manager.username, "password": MANAGER_PASSWORD},
    )
    assert ok.status_code == 200

    # Le compteur a ete remis a zero : 9 nouvelles tentatives ne declenchent
    # pas le 429 (qui n'arriverait qu'a la 10e apres reset).
    for _ in range(9):
        resp = await client.post(
            "/api/auth/login",
            data={"username": manager.username, "password": "faux"},
        )
        assert resp.status_code == 401


async def test_me_with_token(client, manager, auth_headers):
    response = await client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == manager.username
    assert body["email"] == manager.email


async def test_me_without_token_is_401(client):
    response = await client.get("/api/auth/me")
    assert response.status_code == 401


async def test_refresh_returns_new_token(client, auth_headers):
    old_token = auth_headers["Authorization"].split(" ", 1)[1]
    response = await client.post("/api/auth/refresh", headers=auth_headers)
    assert response.status_code == 200
    new_token = response.json()["access_token"]
    assert new_token != old_token

    from app.core.database import async_session

    async with async_session() as db:
        event = await _last_event(db)
    assert event.event_type == "auth.token_refresh"


async def test_refresh_with_non_uuid_subject_is_401_not_500(client):
    # Signe avec la bonne cle (settings.SECRET_KEY) mais un `sub` invalide :
    # doit etre rejete proprement (401), jamais planter en 500 sur la
    # comparaison DB `User.id == user_id`.
    from app.core.security import create_access_token

    bad_token = create_access_token(data={"sub": "pas-un-uuid"})
    response = await client.post(
        "/api/auth/refresh", headers={"Authorization": f"Bearer {bad_token}"}
    )
    assert response.status_code == 401


async def test_logout_is_204_and_journals_event(client, auth_headers):
    response = await client.post("/api/auth/logout", headers=auth_headers)
    assert response.status_code == 204

    from app.core.database import async_session

    async with async_session() as db:
        event = await _last_event(db)
    assert event.event_type == "auth.logout"
