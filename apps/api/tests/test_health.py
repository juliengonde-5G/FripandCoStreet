import pytest

pytestmark = pytest.mark.anyio


async def test_health_ok(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["app"] == "fripco-street-api"
    assert "version" in body
    assert body["environment"] == "test"
    # PR11 (0011_cahier_days) — cf.
    # app/version.py::EXPECTED_DB_REVISION.
    assert body["expected_db_revision"] == "0011"


async def test_openapi_is_served_under_api(client):
    """PR12 — le reverse-proxy ne route que `/api/*` vers l'API : la
    description de l'API doit etre servie la, et nulle part ailleurs (le
    controle 6/9 de `scripts/smoke_prod.sh` et `docs/DEPLOIEMENT.md` §13 ne
    connaissent que ce chemin)."""
    response = await client.get("/api/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"]

    # Le chemin par defaut de FastAPI ne doit plus repondre : en production
    # il tomberait sur le front, pas sur l'API.
    assert (await client.get("/openapi.json")).status_code == 404
    assert (await client.get("/docs")).status_code == 404
    assert (await client.get("/api/docs")).status_code == 200
