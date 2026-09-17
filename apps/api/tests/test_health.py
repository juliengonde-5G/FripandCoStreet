import pytest

pytestmark = pytest.mark.anyio


async def test_health_ok(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["app"] == "fripco-street-api"
    assert "version" in body
    assert body["environment"] == "test"
    # PR9 (0009_sumup_exchanges_failed_payments) — cf.
    # app/version.py::EXPECTED_DB_REVISION.
    assert body["expected_db_revision"] == "0009"
