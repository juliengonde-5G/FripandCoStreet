# Nouveau test (§4.7/D13 du contrat) : parametrage boutique en base,
# validation par cle, journalisation JET `config.changed`.
import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.jet import JournalEvent

pytestmark = pytest.mark.anyio


async def test_get_shop_settings_defaults(client, auth_headers):
    r = await client.get("/api/admin/settings/shop", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["name"] == "Frip & Co Street"


async def test_get_shop_settings_defaults_include_dpo_email(client, auth_headers):
    # PR3 (E8) : `dpo_email` est déclaré par `ShopSettingsIn` — il doit être
    # renvoyé (vide) même avant tout PUT sur la clé `shop`, comme les autres
    # champs par défaut.
    r = await client.get("/api/admin/settings/shop", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["dpo_email"] == ""


async def test_get_fiscal_settings_defaults_to_20_percent(client, auth_headers):
    r = await client.get("/api/admin/settings/fiscal", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["tva_rate"] == "20.00"


async def test_get_unknown_settings_key_404(client, auth_headers):
    r = await client.get("/api/admin/settings/unknown", headers=auth_headers)
    assert r.status_code == 404


async def test_put_shop_settings_persists_and_journals(client, auth_headers):
    r = await client.put(
        "/api/admin/settings/shop",
        json={
            "name": "Frip & Co Street — Rouen",
            "address_line1": "12 rue de la Gare",
            "postal_code": "76000",
            "city": "Rouen",
            "siret": "12345678901234",
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["siret"] == "12345678901234"

    r2 = await client.get("/api/admin/settings/shop", headers=auth_headers)
    assert r2.json()["name"] == "Frip & Co Street — Rouen"

    async with async_session() as db:
        events = (
            await db.execute(select(JournalEvent).where(JournalEvent.event_type == "config.changed"))
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["key"] == "shop"
    assert "name" in events[0].payload["changed_fields"]


async def test_put_shop_settings_rejects_bad_siret(client, auth_headers):
    r = await client.put(
        "/api/admin/settings/shop", json={"name": "Boutique", "siret": "123"}, headers=auth_headers
    )
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_setting"


async def test_put_fiscal_settings_rejects_unsupported_rate(client, auth_headers):
    r = await client.put("/api/admin/settings/fiscal", json={"tva_rate": "19.60"}, headers=auth_headers)
    assert r.status_code == 422


async def test_put_fiscal_settings_accepts_supported_rate(client, auth_headers):
    r = await client.put("/api/admin/settings/fiscal", json={"tva_rate": "5.50"}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["tva_rate"] == "5.50"


async def test_new_sale_uses_updated_tva_rate(client, auth_headers, open_drawer):
    await client.put("/api/admin/settings/fiscal", json={"tva_rate": "5.50"}, headers=auth_headers)
    import uuid

    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Livre", "unit_price": "10.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "10.00", "tendered_amount": "10.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["tva_rate"] == 5.5


async def test_put_receipt_settings_free_form(client, auth_headers):
    r = await client.put(
        "/api/admin/settings/receipt",
        json={"header_note": "", "footer_note": "A bientot !", "return_policy": ""},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["footer_note"] == "A bientot !"
