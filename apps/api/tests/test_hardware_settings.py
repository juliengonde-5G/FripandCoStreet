# Nouveau test (PR3b) — réglages matériel (`app_settings` clé `hardware`,
# décision Julien) : GET/PUT via `SettingsService`, validation IP/port,
# JET `config.changed`.
import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.jet import JournalEvent

pytestmark = pytest.mark.anyio


async def test_get_hardware_settings_defaults(client, auth_headers):
    r = await client.get("/api/admin/settings/hardware", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["printer_mode"] == "none"
    assert body["printer_port"] == 9100
    assert body["drawer_enabled"] is False
    assert body["auto_print_on_sale"] is False
    assert body["auto_kick_on_cash"] is False


async def test_put_hardware_settings_network_mode_persists_and_journals(client, auth_headers):
    r = await client.put(
        "/api/admin/settings/hardware",
        json={
            "printer_mode": "network",
            "printer_host": "192.168.1.50",
            "printer_port": 9100,
            "drawer_enabled": True,
            "drawer_pin": 0,
            "auto_print_on_sale": True,
            "auto_kick_on_cash": True,
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["printer_mode"] == "network"
    assert body["printer_host"] == "192.168.1.50"
    assert body["drawer_enabled"] is True

    r2 = await client.get("/api/admin/settings/hardware", headers=auth_headers)
    assert r2.json()["printer_host"] == "192.168.1.50"

    async with async_session() as db:
        events = (
            await db.execute(select(JournalEvent).where(JournalEvent.event_type == "config.changed"))
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["key"] == "hardware"


async def test_put_hardware_settings_webusb_mode_allows_blank_host(client, auth_headers):
    r = await client.put(
        "/api/admin/settings/hardware",
        json={"printer_mode": "webusb", "drawer_enabled": True},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["printer_mode"] == "webusb"
    assert r.json()["printer_host"] == ""


async def test_put_hardware_settings_network_mode_requires_host(client, auth_headers):
    r = await client.put(
        "/api/admin/settings/hardware",
        json={"printer_mode": "network", "printer_host": ""},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_setting"


async def test_put_hardware_settings_rejects_invalid_ip(client, auth_headers):
    r = await client.put(
        "/api/admin/settings/hardware",
        json={"printer_mode": "network", "printer_host": "not-an-ip"},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_setting"


async def test_put_hardware_settings_rejects_out_of_range_port(client, auth_headers):
    r = await client.put(
        "/api/admin/settings/hardware",
        json={"printer_mode": "webusb", "printer_port": 70000},
        headers=auth_headers,
    )
    assert r.status_code == 422


async def test_put_hardware_settings_rejects_bad_drawer_pin(client, auth_headers):
    r = await client.put(
        "/api/admin/settings/hardware",
        json={"printer_mode": "none", "drawer_pin": 5},
        headers=auth_headers,
    )
    assert r.status_code == 422


async def test_hardware_settings_never_expose_secret_style_fields(client, auth_headers):
    # Garde-fou de conception : la clé `hardware` ne doit jamais porter de
    # secret (contrairement à SumUp, en variables d'environnement — D12).
    r = await client.get("/api/admin/settings/hardware", headers=auth_headers)
    body = r.json()
    assert not any("key" in k.lower() or "secret" in k.lower() or "token" in k.lower() for k in body)
