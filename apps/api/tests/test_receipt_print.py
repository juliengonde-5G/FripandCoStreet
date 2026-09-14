# Nouveau test (PR3b, décision Julien) — impression physique des tickets
# (`POST /pos/transactions/{id}/print`, `GET .../escpos`) et ouverture du
# tiroir-caisse (`POST /pos/drawer/kick`, `GET /pos/drawer/kick-escpos`).
# Le "faux serveur TCP local" (fixture ``fake_printer``) tient lieu de
# MUNBYN 047P réseau et capture les octets reçus.
import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.jet import JournalEvent
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


async def _sell(client, auth_headers, amount: str = "10.00") -> dict:
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Article", "unit_price": amount, "quantity": 1}],
            "payments": [{"method": "cash", "amount": amount, "tendered_amount": amount}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


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


async def _journal_events(event_type: str) -> list[JournalEvent]:
    async with async_session() as db:
        return (
            await db.execute(select(JournalEvent).where(JournalEvent.event_type == event_type))
        ).scalars().all()


# ---------------------------------------------------------------------------
# POST /pos/transactions/{id}/print — mode network
# ---------------------------------------------------------------------------


async def test_print_receipt_network_success_first_print_is_not_a_duplicate(
    client, auth_headers, open_drawer, fake_printer
):
    host, port, received = fake_printer
    await _configure_hardware(
        client, auth_headers, printer_mode="network", printer_host=host, printer_port=port
    )
    sale = await _sell(client, auth_headers)

    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/print", json={}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"printed": True, "printed_count": 1, "duplicate": False}

    assert len(received) == 1
    assert escpos_service.encode_text("Ticket #1") in received[0]
    assert escpos_service.encode_text("FRIP & CO STREET") in received[0]

    events = await _journal_events("receipt.printed")
    assert len(events) == 1
    assert events[0].payload == {
        "transaction_id": sale["id"],
        "number": 1,
        "mode": "network",
        "duplicate": False,
    }


async def test_print_receipt_second_print_is_a_duplicate(
    client, auth_headers, open_drawer, fake_printer
):
    host, port, received = fake_printer
    await _configure_hardware(
        client, auth_headers, printer_mode="network", printer_host=host, printer_port=port
    )
    sale = await _sell(client, auth_headers)

    await client.post(f"/api/pos/transactions/{sale['id']}/print", json={}, headers=auth_headers)
    r2 = await client.post(
        f"/api/pos/transactions/{sale['id']}/print", json={}, headers=auth_headers
    )
    assert r2.status_code == 200
    assert r2.json() == {"printed": True, "printed_count": 2, "duplicate": True}
    assert len(received) == 2

    events = await _journal_events("receipt.printed")
    assert len(events) == 2
    assert events[1].payload["duplicate"] is True


async def test_print_receipt_with_kick_appends_kick_bytes(
    client, auth_headers, open_drawer, fake_printer
):
    host, port, received = fake_printer
    await _configure_hardware(
        client,
        auth_headers,
        printer_mode="network",
        printer_host=host,
        printer_port=port,
        drawer_enabled=True,
        drawer_pin=0,
    )
    sale = await _sell(client, auth_headers)

    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/print", json={"kick": True}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert received[0].endswith(escpos_service.build_drawer_kick(pin=0))


async def test_print_receipt_kick_ignored_when_drawer_disabled(
    client, auth_headers, open_drawer, fake_printer
):
    host, port, received = fake_printer
    await _configure_hardware(
        client,
        auth_headers,
        printer_mode="network",
        printer_host=host,
        printer_port=port,
        drawer_enabled=False,
    )
    sale = await _sell(client, auth_headers)

    r = await client.post(
        f"/api/pos/transactions/{sale['id']}/print", json={"kick": True}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert not received[0].endswith(escpos_service.build_drawer_kick(pin=0))


# ---------------------------------------------------------------------------
# POST /pos/transactions/{id}/print — modes webusb / none / erreurs
# ---------------------------------------------------------------------------


async def test_print_receipt_webusb_mode_returns_409(client, auth_headers, open_drawer):
    await _configure_hardware(client, auth_headers, printer_mode="webusb")
    sale = await _sell(client, auth_headers)

    r = await client.post(f"/api/pos/transactions/{sale['id']}/print", json={}, headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "printer_webusb"


async def test_print_receipt_disabled_mode_returns_409(client, auth_headers, open_drawer):
    # Pas de configuration explicite -> defaut "none".
    sale = await _sell(client, auth_headers)

    r = await client.post(f"/api/pos/transactions/{sale['id']}/print", json={}, headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "printer_disabled"


async def test_print_receipt_unreachable_printer_returns_502_and_does_not_count(
    client, auth_headers, open_drawer
):
    # Port TCP ferme sur localhost : connexion refusee immediatement.
    await _configure_hardware(
        client, auth_headers, printer_mode="network", printer_host="127.0.0.1", printer_port=1
    )
    sale = await _sell(client, auth_headers)

    r = await client.post(f"/api/pos/transactions/{sale['id']}/print", json={}, headers=auth_headers)
    assert r.status_code == 502
    assert r.json()["code"] == "printer_unreachable"

    r2 = await client.get(f"/api/pos/transactions/{sale['id']}/receipt", headers=auth_headers)
    # duplicate_count (PR2, lecture texte) n'est pas le meme compteur que
    # printed_count : l'echec d'impression ne l'a pas touche non plus.
    assert r2.status_code == 200

    events = await _journal_events("receipt.printed")
    assert events == []


async def test_print_receipt_unknown_transaction_404(client, auth_headers):
    r = await client.post(f"/api/pos/transactions/{uuid.uuid4()}/print", json={}, headers=auth_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /pos/transactions/{id}/escpos — WebUSB
# ---------------------------------------------------------------------------


async def test_get_escpos_returns_raw_bytes_and_counts_as_print(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)

    r = await client.get(f"/api/pos/transactions/{sale['id']}/escpos", headers=auth_headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert escpos_service.encode_text("Ticket #1") in r.content

    events = await _journal_events("receipt.printed")
    assert len(events) == 1
    assert events[0].payload["mode"] == "webusb"
    assert events[0].payload["duplicate"] is False

    r2 = await client.get(f"/api/pos/transactions/{sale['id']}/escpos", headers=auth_headers)
    assert r2.status_code == 200
    events2 = await _journal_events("receipt.printed")
    assert len(events2) == 2
    assert events2[1].payload["duplicate"] is True


async def test_get_escpos_with_kick_query_param(client, auth_headers, open_drawer):
    await _configure_hardware(client, auth_headers, drawer_enabled=True)
    sale = await _sell(client, auth_headers)

    r = await client.get(
        f"/api/pos/transactions/{sale['id']}/escpos", params={"kick": "true"}, headers=auth_headers
    )
    assert r.status_code == 200
    assert r.content.endswith(escpos_service.build_drawer_kick(pin=0))


async def test_get_escpos_unknown_transaction_404(client, auth_headers):
    r = await client.get(f"/api/pos/transactions/{uuid.uuid4()}/escpos", headers=auth_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /pos/drawer/kick — impulsion seule (réseau)
# ---------------------------------------------------------------------------


async def test_drawer_kick_network_sends_exact_pulse_bytes(
    client, auth_headers, open_drawer, fake_printer
):
    host, port, received = fake_printer
    await _configure_hardware(
        client,
        auth_headers,
        printer_mode="network",
        printer_host=host,
        printer_port=port,
        drawer_enabled=True,
        drawer_pin=1,
    )

    r = await client.post(
        "/api/pos/drawer/kick", json={"reason": "cash_sale"}, headers=auth_headers
    )
    assert r.status_code == 200
    assert r.json() == {"kicked": True}
    assert received[0] == escpos_service.build_drawer_kick(pin=1)

    events = await _journal_events("drawer.kicked")
    assert len(events) == 1
    assert events[0].payload == {"reason": "cash_sale"}


async def test_drawer_kick_returns_409_when_drawer_disabled(client, auth_headers, open_drawer):
    await _configure_hardware(
        client, auth_headers, printer_mode="network", printer_host="127.0.0.1", drawer_enabled=False
    )
    r = await client.post("/api/pos/drawer/kick", json={}, headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "drawer_unavailable"


async def test_drawer_kick_returns_409_when_printer_not_network(client, auth_headers, open_drawer):
    await _configure_hardware(client, auth_headers, printer_mode="webusb", drawer_enabled=True)
    r = await client.post("/api/pos/drawer/kick", json={}, headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "drawer_unavailable"


async def test_drawer_kick_defaults_to_manual_reason(client, auth_headers, open_drawer, fake_printer):
    host, port, _received = fake_printer
    await _configure_hardware(
        client,
        auth_headers,
        printer_mode="network",
        printer_host=host,
        printer_port=port,
        drawer_enabled=True,
    )
    r = await client.post("/api/pos/drawer/kick", json={}, headers=auth_headers)
    assert r.status_code == 200
    events = await _journal_events("drawer.kicked")
    assert events[0].payload == {"reason": "manual"}


# ---------------------------------------------------------------------------
# GET /pos/drawer/kick-escpos — WebUSB
# ---------------------------------------------------------------------------


async def test_drawer_kick_escpos_returns_exact_bytes(client, auth_headers):
    await _configure_hardware(client, auth_headers, drawer_enabled=True, drawer_pin=0)
    r = await client.get("/api/pos/drawer/kick-escpos", headers=auth_headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.content == escpos_service.build_drawer_kick(pin=0)

    events = await _journal_events("drawer.kicked")
    assert events[0].payload == {"reason": "manual"}


async def test_drawer_kick_escpos_returns_409_when_disabled(client, auth_headers):
    r = await client.get("/api/pos/drawer/kick-escpos", headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "drawer_unavailable"
