# Nouveau test (PR3b, décision Julien) — endpoints `/hardware/*` : sonde
# imprimante (`GET /hardware/printer/status`) et ticket de test
# (`POST /hardware/receipt/test`, `GET /hardware/receipt/test-escpos`).
import asyncio

import pytest

from app.services import escpos_service

pytestmark = pytest.mark.anyio


@pytest.fixture
async def fake_printer():
    received: list[bytes] = []

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        data = await reader.read()
        received.append(data)
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    async with server:
        yield host, port, received


async def _configure_hardware(client, auth_headers, **overrides) -> dict:
    payload = {
        "printer_mode": "none",
        "printer_host": "",
        "printer_port": 9100,
        "drawer_enabled": False,
        "drawer_pin": 0,
        "auto_print_on_sale": False,
        "auto_kick_on_cash": False,
    }
    payload.update(overrides)
    r = await client.put("/api/admin/settings/hardware", json=payload, headers=auth_headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# GET /hardware/printer/status
# ---------------------------------------------------------------------------


async def test_printer_status_defaults_to_disabled(client, auth_headers):
    r = await client.get("/api/hardware/printer/status", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "none"
    assert body["online"] is None
    assert body["latency_ms"] is None


async def test_printer_status_webusb_mode_has_no_live_probe(client, auth_headers):
    await _configure_hardware(client, auth_headers, printer_mode="webusb")
    r = await client.get("/api/hardware/printer/status", headers=auth_headers)
    body = r.json()
    assert body["mode"] == "webusb"
    assert body["online"] is None
    assert body["latency_ms"] is None


async def test_printer_status_network_mode_online(client, auth_headers, fake_printer):
    host, port, _received = fake_printer
    await _configure_hardware(
        client, auth_headers, printer_mode="network", printer_host=host, printer_port=port
    )
    r = await client.get("/api/hardware/printer/status", headers=auth_headers)
    body = r.json()
    assert body["mode"] == "network"
    assert body["host"] == host
    assert body["port"] == port
    assert body["online"] is True
    assert isinstance(body["latency_ms"], (int, float))


async def test_printer_status_network_mode_offline(client, auth_headers):
    await _configure_hardware(
        client, auth_headers, printer_mode="network", printer_host="127.0.0.1", printer_port=1
    )
    r = await client.get("/api/hardware/printer/status", headers=auth_headers)
    body = r.json()
    assert body["online"] is False
    assert body["latency_ms"] is None


# ---------------------------------------------------------------------------
# POST /hardware/receipt/test — ticket de test réseau
# ---------------------------------------------------------------------------


async def test_receipt_test_network_success(client, auth_headers, fake_printer):
    host, port, received = fake_printer
    await _configure_hardware(
        client, auth_headers, printer_mode="network", printer_host=host, printer_port=port
    )
    r = await client.post("/api/hardware/receipt/test", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"printed": True, "host": host, "port": port}
    assert len(received) == 1
    assert escpos_service.encode_text("Ticket de test") in received[0]


async def test_receipt_test_disabled_returns_409(client, auth_headers):
    r = await client.post("/api/hardware/receipt/test", headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "printer_disabled"


async def test_receipt_test_unreachable_returns_502(client, auth_headers):
    await _configure_hardware(
        client, auth_headers, printer_mode="network", printer_host="127.0.0.1", printer_port=1
    )
    r = await client.post("/api/hardware/receipt/test", headers=auth_headers)
    assert r.status_code == 502
    assert r.json()["code"] == "printer_unreachable"


# ---------------------------------------------------------------------------
# GET /hardware/receipt/test-escpos — WebUSB
# ---------------------------------------------------------------------------


async def test_receipt_test_escpos_returns_bytes_regardless_of_mode(client, auth_headers):
    r = await client.get("/api/hardware/receipt/test-escpos", headers=auth_headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert escpos_service.encode_text("Ticket de test") in r.content
    assert r.content.endswith(escpos_service.CUT_PARTIAL)
