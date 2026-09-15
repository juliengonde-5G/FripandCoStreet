import pytest

pytestmark = pytest.mark.anyio


async def test_health_ok(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["app"] == "fripco-street-api"
    assert "version" in body
    assert body["environment"] == "test"
    # PR7 (0007_clients_phone) — cf. app/version.py::EXPECTED_DB_REVISION.
    assert body["expected_db_revision"] == "0007"
