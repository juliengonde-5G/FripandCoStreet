# Nouveau test (PR3, §7 ARCHITECTURE_PR3.md, E3) — `client_id` est la SEULE
# colonne mutable hors hash sur une transaction déjà signée : le trigger
# `fripco_protect_signed_transaction` (réécrit en migration 0003) doit
# laisser passer `UPDATE transactions SET client_id = …` mais continuer à
# refuser toute autre colonne (`total_ttc` notamment). `verify_chain_integrity`
# doit rester valide après un rattachement client — le payload signé ne
# référence jamais `client_id` (`fiscal.py::_transaction_payload`).
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import async_session, engine
from app.models.client import Client
from app.models.pos import Transaction
from app.services.fiscal import FiscalService

pytestmark = pytest.mark.anyio


async def _sell(client, auth_headers, amount: str = "10.00") -> dict:
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Robe", "unit_price": amount, "quantity": 1}],
            "payments": [{"method": "cash", "amount": amount, "tendered_amount": amount}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_client_id_update_allowed_on_signed_transaction(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    async with async_session() as db:
        c = Client(email="direct-sql@example.com")
        db.add(c)
        await db.commit()
        await db.refresh(c)
        new_client_id = c.id

    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE transactions SET client_id = :cid WHERE id = :id"),
            {"cid": str(new_client_id), "id": sale["id"]},
        )

    async with async_session() as db:
        tx = (
            await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))
        ).scalar_one()
        assert tx.client_id == new_client_id


async def test_total_ttc_update_still_rejected_on_signed_transaction(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE transactions SET total_ttc = 999.99 WHERE id = :id"),
                {"id": sale["id"]},
            )


async def test_verify_chain_integrity_valid_after_client_link(client, auth_headers, open_drawer):
    sale1 = await _sell(client, auth_headers, "10.00")
    sale2 = await _sell(client, auth_headers, "20.00")

    r = await client.post(
        f"/api/pos/transactions/{sale1['id']}/client",
        json={"email": "chain-ok@example.com", "send_receipt": False},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    async with async_session() as db:
        result = await FiscalService(db).verify_chain_integrity()
    assert result["valid"] is True
    assert result["checked"] == 2

    # Deuxieme rattachement (autre vente, autre client) — toujours valide.
    r2 = await client.post(
        f"/api/pos/transactions/{sale2['id']}/client",
        json={"email": "chain-ok-2@example.com", "send_receipt": False},
        headers=auth_headers,
    )
    assert r2.status_code == 200

    async with async_session() as db:
        result2 = await FiscalService(db).verify_chain_integrity()
    assert result2["valid"] is True


async def test_transaction_detail_and_list_expose_linked_client(client, auth_headers, open_drawer):
    """Complement au contrat : `TransactionOut` (liste ET detail) porte un
    champ `client` = {id, email, first_name, last_name} quand `client_id`
    est pose, `null` sinon — pour prereplir « Envoyer par e-mail » dans
    Tickets du jour, sans requete supplementaire par ligne (`_load_clients`,
    `app/api/pos/router.py`)."""
    sale = await _sell(client, auth_headers)

    # Avant rattachement : `client` est explicitement null.
    before = await client.get(f"/api/pos/transactions/{sale['id']}", headers=auth_headers)
    assert before.status_code == 200
    assert before.json()["client"] is None

    attach = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={
            "email": "ticket-du-jour@example.com",
            "first_name": "Camille",
            "last_name": "Durand",
            "send_receipt": False,
        },
        headers=auth_headers,
    )
    assert attach.status_code == 200, attach.text

    detail = await client.get(f"/api/pos/transactions/{sale['id']}", headers=auth_headers)
    assert detail.status_code == 200
    detail_client = detail.json()["client"]
    assert detail_client is not None
    assert detail_client["email"] == "ticket-du-jour@example.com"
    assert detail_client["first_name"] == "Camille"
    assert detail_client["last_name"] == "Durand"

    listing = await client.get("/api/pos/transactions", headers=auth_headers)
    assert listing.status_code == 200
    matching = next(t for t in listing.json()["transactions"] if t["id"] == sale["id"])
    assert matching["client"] is not None
    assert matching["client"]["email"] == "ticket-du-jour@example.com"
