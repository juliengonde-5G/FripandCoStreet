# Nouveau test (PR12, docs/ARCHITECTURE_PR12.md §3, N5) — l'identifiant de
# requete doit sortir sur TOUTES les reponses, sans exception : c'est la
# « reference » que la vendeuse lit a l'ecran et qu'on retrouve ensuite dans
# les logs serveur. Une seule famille de reponses qui l'oublierait suffirait
# a rendre le dispositif inutilisable le jour ou il sert.
from __future__ import annotations

import uuid

import pytest

from app.core import middleware as middleware_mod
from app.services import monitoring

pytestmark = pytest.mark.anyio

HEADER = "x-request-id"


@pytest.fixture(autouse=True)
def _reset_recent_errors():
    middleware_mod.reset_recent_errors()
    yield
    middleware_mod.reset_recent_errors()


async def test_request_id_on_200(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.headers[HEADER]


async def test_incoming_request_id_is_kept(client):
    incoming = uuid.uuid4().hex[:16]
    r = await client.get("/api/health", headers={"X-Request-ID": incoming})
    assert r.status_code == 200
    assert r.headers[HEADER] == incoming


async def test_request_id_on_business_4xx(client, auth_headers):
    """Erreur metier levee en `PosServiceError` et convertie par le handler
    d'application (`main.py::pos_exception_handler`) : la reponse repasse par
    la frontiere d'erreur, donc elle porte l'en-tete."""
    incoming = uuid.uuid4().hex[:16]
    r = await client.get(
        "/api/admin/accounting/journal",
        params={"from": "pas-une-date", "to": "2026-01-01"},
        headers={**auth_headers, "X-Request-ID": incoming},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_date"
    assert r.headers[HEADER] == incoming


async def test_request_id_on_domain_conflict(client, auth_headers):
    """409 caisse fermee — l'autre famille d'erreurs metier (`FripcoError`)."""
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Robe", "unit_price": "10.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "10.00", "tendered_amount": "10.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 409, r.text
    assert r.headers[HEADER]


async def test_request_id_on_401(client):
    r = await client.get("/api/admin/monitoring")
    assert r.status_code == 401
    assert r.headers[HEADER]


async def test_request_id_on_500(client, auth_headers, monkeypatch):
    """Frontiere d'erreur : une exception non geree devient un 500 JSON
    lisible, avec le meme identifiant en en-tete ET dans le corps."""

    async def _boom(db, **kwargs):
        raise RuntimeError("panne simulee")

    monkeypatch.setattr(monitoring, "build_snapshot", _boom)
    incoming = uuid.uuid4().hex[:16]
    r = await client.get(
        "/api/admin/monitoring", headers={**auth_headers, "X-Request-ID": incoming}
    )
    assert r.status_code == 500
    assert r.headers[HEADER] == incoming
    body = r.json()
    assert body["request_id"] == incoming
    assert body["error_type"] == "RuntimeError"
    assert body["detail"]


async def test_500_feeds_the_recent_errors_buffer(client, auth_headers, monkeypatch):
    async def _boom(db, **kwargs):
        raise ValueError("panne simulee")

    monkeypatch.setattr(monitoring, "build_snapshot", _boom)
    r = await client.get("/api/admin/monitoring", headers=auth_headers)
    assert r.status_code == 500

    buffered = middleware_mod.recent_errors()
    assert len(buffered) == 1
    assert buffered[0]["request_id"] == r.headers[HEADER]
    assert buffered[0]["status"] == 500
    assert buffered[0]["error_type"] == "ValueError"
    assert buffered[0]["path"] == "/api/admin/monitoring"


async def test_successful_responses_do_not_feed_the_buffer(client, auth_headers):
    await client.get("/api/health")
    r = await client.get("/api/admin/monitoring", headers=auth_headers)
    assert r.status_code == 200
    assert middleware_mod.recent_errors() == []


async def test_business_4xx_does_not_feed_the_buffer(client, auth_headers):
    """Une 4xx metier est un refus normal (caisse fermee, date invalide) :
    elle n'a rien a faire dans le tampon des pannes."""
    r = await client.get(
        "/api/admin/accounting/journal",
        params={"from": "pas-une-date", "to": "2026-01-01"},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert middleware_mod.recent_errors() == []
