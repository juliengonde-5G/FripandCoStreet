# Nouveau test (PR3b) — pilote ESC/POS (app/services/escpos_service.py) :
# encodage des accents, octets exacts de l'impulsion tiroir, envoi TCP
# contre un faux serveur local (asyncio.start_server) qui capture les
# octets reçus, sonde online/offline.
import asyncio

import pytest

from app.services import escpos_service

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Encodage
# ---------------------------------------------------------------------------


def test_encode_text_cp858_accents():
    # cp858 = CP850 + symbole Euro — memes octets que le codepage 19
    # selectionne par ESC t dans escpos_service.INIT.
    assert escpos_service.encode_text("é") == "é".encode("cp858")
    assert escpos_service.encode_text("à") == "à".encode("cp858")
    assert escpos_service.encode_text("ç") == "ç".encode("cp858")
    assert escpos_service.encode_text("€") == "€".encode("cp858")
    assert escpos_service.encode_text("Ticket #1 Café a 2,50 €") == (
        "Ticket #1 Café a 2,50 €".encode("cp858")
    )


def test_encode_text_falls_back_to_cp1252_for_em_dash():
    # "—" (em dash, U+2014) n'existe pas dans CP858 mais existe en CP1252
    # (0x97) — l'encodage doit retomber sur CP1252 pour ce caractere plutot
    # que de degrader tout le ticket en ASCII.
    assert escpos_service.encode_text("—") == "—".encode("cp1252")
    assert escpos_service.encode_text("Ticket #1 — Café") == "Ticket #1 — Café".encode("cp1252")


def test_encode_text_falls_back_to_ascii_replace_for_unsupported_chars():
    # Un caractère absent de CP858 ET CP1252 (ex. 一, CJK) ne doit jamais
    # faire échouer l'encodage — repli ASCII avec remplacement.
    result = escpos_service.encode_text("一")
    assert result == b"?"


def test_build_receipt_body_reused_verbatim():
    content = "Ligne 1\nLigne 2 — café\nTotal TTC:  10.00 EUR"
    payload = escpos_service.build_receipt(content, shop_name="", cut=False)
    # Le corps n'est jamais reformate : chaque ligne du texte fige se
    # retrouve encodee telle quelle, separee par LF.
    assert escpos_service.encode_text("Ligne 1") in payload
    assert escpos_service.encode_text("Ligne 2 — café") in payload
    assert escpos_service.encode_text("Total TTC:  10.00 EUR") in payload


def test_build_receipt_includes_shop_name_banner():
    payload = escpos_service.build_receipt("Corps", shop_name="Frip & Co Street", cut=False)
    assert escpos_service.encode_text("FRIP & CO STREET") in payload


def test_build_receipt_cuts_paper_by_default():
    payload = escpos_service.build_receipt("Corps")
    assert payload.endswith(escpos_service.CUT_PARTIAL)


def test_build_receipt_appends_kick_after_cut():
    kick = escpos_service.build_drawer_kick()
    payload = escpos_service.build_receipt("Corps", kick=kick)
    assert payload.endswith(kick)


# ---------------------------------------------------------------------------
# Tiroir-caisse — ESC p m t1 t2
# ---------------------------------------------------------------------------


def test_build_drawer_kick_default_bytes():
    payload = escpos_service.build_drawer_kick()
    # pin=0 -> m=0x00 ; on_time=50ms -> t1=25 (0x19) ; off_time=250ms ->
    # t2=125 (0x7D).
    assert payload == bytes([0x1B, 0x70, 0x00, 0x19, 0x7D])


def test_build_drawer_kick_pin_1_and_custom_timings():
    payload = escpos_service.build_drawer_kick(pin=1, on_time=100, off_time=400)
    # pin=1 -> m=0x01 ; on_time=100ms -> t1=50 (0x32) ; off_time=400ms ->
    # t2=200 (0xC8).
    assert payload == bytes([0x1B, 0x70, 0x01, 0x32, 0xC8])


def test_build_drawer_kick_clamps_to_255():
    payload = escpos_service.build_drawer_kick(on_time=10_000, off_time=10_000)
    assert payload == bytes([0x1B, 0x70, 0x00, 0xFF, 0xFF])


# ---------------------------------------------------------------------------
# Réseau (TCP) — faux serveur local
# ---------------------------------------------------------------------------


async def _start_capturing_server():
    received = bytearray()
    closed = asyncio.Event()

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        data = await reader.read()
        received.extend(data)
        writer.close()
        closed.set()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    return server, host, port, received, closed


async def test_send_to_printer_delivers_exact_bytes():
    server, host, port, received, closed = await _start_capturing_server()
    async with server:
        payload = escpos_service.build_receipt("Ticket de test", shop_name="Frip & Co Street")
        written = await escpos_service.send_to_printer(host, port, payload, timeout=2.0)
        await asyncio.wait_for(closed.wait(), timeout=2.0)
        assert written == len(payload)
        assert bytes(received) == payload


async def test_send_to_printer_raises_on_unreachable_host():
    with pytest.raises(escpos_service.PrinterUnreachable):
        # Port fermé sur localhost : connexion refusée immédiatement.
        await escpos_service.send_to_printer("127.0.0.1", 1, b"x", timeout=1.0)


async def test_send_to_printer_raises_when_host_missing():
    with pytest.raises(escpos_service.PrinterUnreachable):
        await escpos_service.send_to_printer("", 9100, b"x")


async def test_ping_printer_online():
    server, host, port, _received, _closed = await _start_capturing_server()
    async with server:
        status = await escpos_service.ping_printer(host, port, timeout=2.0)
        assert status.online is True
        assert status.latency_ms is not None
        assert status.latency_ms >= 0


async def test_ping_printer_offline():
    status = await escpos_service.ping_printer("127.0.0.1", 1, timeout=1.0)
    assert status.online is False
    assert status.latency_ms is None
    assert status.detail


async def test_ping_printer_no_host_configured():
    status = await escpos_service.ping_printer("", 9100)
    assert status.online is False
    assert status.latency_ms is None
