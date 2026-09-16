# Nouveau test (PR10, docs/ARCHITECTURE_PR10.md, contrat L4) — historique
# d'achats d'une cliente tel qu'il s'affiche EN CAISSE : « elle est deja
# venue 3 fois, dont la semaine derniere ». Ventes seulement, annulations
# signalees et retirees du cumul, montants en chaines a deux decimales,
# apercu des articles borne a cinq lignes, 404 sur une fiche inconnue,
# anonymisee ou absorbee par une fusion.
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.client import Client
from app.services.client_service import ClientService

pytestmark = pytest.mark.anyio


async def _create_client(client, auth_headers, **payload) -> dict:
    r = await client.post("/api/pos/clients", json=payload, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()["client"]


async def _sell(
    client,
    auth_headers,
    *,
    client_id: str | None = None,
    items: list[dict] | None = None,
    amount: str = "25.00",
) -> dict:
    items = items or [{"label": "Robe", "unit_price": amount, "quantity": 1}]
    total = sum(
        (float(i["unit_price"]) * i.get("quantity", 1)) for i in items
    )
    total_str = f"{total:.2f}"
    body = {
        "client_uuid": str(uuid.uuid4()),
        "items": items,
        "payments": [
            {"method": "cash", "amount": total_str, "tendered_amount": total_str}
        ],
    }
    if client_id is not None:
        body["client_id"] = client_id
    r = await client.post("/api/pos/transactions", json=body, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()


async def _cancel(client, auth_headers, sale_id: str) -> dict:
    r = await client.post(
        f"/api/pos/transactions/{sale_id}/cancel",
        json={"reason": "cliente insatisfaite"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Forme de la reponse
# ---------------------------------------------------------------------------


async def test_history_is_sorted_most_recent_first_with_string_amounts(
    client, auth_headers, open_drawer
):
    fiche = await _create_client(
        client, auth_headers, email="camille@example.com", first_name="Camille"
    )
    first = await _sell(client, auth_headers, client_id=fiche["id"], amount="12.50")
    second = await _sell(client, auth_headers, client_id=fiche["id"], amount="30.00")

    r = await client.get(
        f"/api/pos/clients/{fiche['id']}/history", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["client_id"] == fiche["id"]
    assert body["visits_count"] == 2
    assert body["last_visit_at"] is not None
    # Montants : des chaines a deux decimales, jamais des flottants.
    assert body["total_spent"] == "42.50"
    assert isinstance(body["total_spent"], str)

    numbers = [t["transaction_number"] for t in body["transactions"]]
    assert numbers == [second["transaction_number"], first["transaction_number"]]
    recent = body["transactions"][0]
    assert recent["total_ttc"] == "30.00"
    assert recent["items_count"] == 1
    assert recent["items"] == [
        {"label": "Robe", "quantity": 1, "unit_price": "30.00"}
    ]
    assert recent["refunded"] is False


async def test_history_flags_cancelled_sales_and_excludes_them_from_total(
    client, auth_headers, open_drawer
):
    """Une annulation n'est pas une vente : elle n'apparait pas dans la
    liste, elle marque la vente d'origine et sort du cumul."""
    fiche = await _create_client(client, auth_headers, email="lea@example.com")
    kept = await _sell(client, auth_headers, client_id=fiche["id"], amount="20.00")
    cancelled = await _sell(client, auth_headers, client_id=fiche["id"], amount="35.00")
    await _cancel(client, auth_headers, cancelled["id"])

    body = (
        await client.get(
            f"/api/pos/clients/{fiche['id']}/history", headers=auth_headers
        )
    ).json()

    types = {t["transaction_number"] for t in body["transactions"]}
    assert types == {kept["transaction_number"], cancelled["transaction_number"]}
    by_number = {t["transaction_number"]: t for t in body["transactions"]}
    assert by_number[cancelled["transaction_number"]]["refunded"] is True
    assert by_number[kept["transaction_number"]]["refunded"] is False
    # 20,00 € seulement : la vente annulee ne compte pas.
    assert body["total_spent"] == "20.00"
    # Le compteur de visites, lui, compte les ventes (l'annulation n'en est
    # pas une, mais la vente annulee reste une venue en boutique).
    assert body["visits_count"] == 2


async def test_history_limit_caps_the_list_but_not_the_counters(
    client, auth_headers, open_drawer
):
    fiche = await _create_client(client, auth_headers, email="nora@example.com")
    for _ in range(4):
        await _sell(client, auth_headers, client_id=fiche["id"], amount="10.00")

    body = (
        await client.get(
            f"/api/pos/clients/{fiche['id']}/history?limit=2", headers=auth_headers
        )
    ).json()
    assert len(body["transactions"]) == 2
    # Compteurs et cumul portent sur TOUT l'historique.
    assert body["visits_count"] == 4
    assert body["total_spent"] == "40.00"

    # Borne haute du contrat (limit <= 20).
    too_big = await client.get(
        f"/api/pos/clients/{fiche['id']}/history?limit=50", headers=auth_headers
    )
    assert too_big.status_code == 422


async def test_history_keeps_only_the_first_five_item_lines(client, auth_headers, open_drawer):
    fiche = await _create_client(client, auth_headers, email="zoe@example.com")
    items = [
        {"label": f"Piece {n}", "unit_price": "5.00", "quantity": 1} for n in range(7)
    ]
    await _sell(client, auth_headers, client_id=fiche["id"], items=items)

    body = (
        await client.get(
            f"/api/pos/clients/{fiche['id']}/history", headers=auth_headers
        )
    ).json()
    ticket = body["transactions"][0]
    # `items_count` porte le TOTAL : c'est lui qui permet a la caisse
    # d'afficher « … et 2 autres articles ».
    assert ticket["items_count"] == 7
    # Cinq vraies lignes, et rien d'autre : pas de pseudo-ligne « … ».
    assert len(ticket["items"]) == 5
    assert [i["label"] for i in ticket["items"]] == [
        "Piece 0", "Piece 1", "Piece 2", "Piece 3", "Piece 4"
    ]
    assert all(i["quantity"] == 1 for i in ticket["items"])
    assert all(i["unit_price"] == "5.00" for i in ticket["items"])


async def test_history_of_a_first_time_client_is_empty(client, auth_headers):
    fiche = await _create_client(client, auth_headers, email="premiere@example.com")
    body = (
        await client.get(
            f"/api/pos/clients/{fiche['id']}/history", headers=auth_headers
        )
    ).json()
    assert body["transactions"] == []
    assert body["visits_count"] == 0
    assert body["last_visit_at"] is None
    assert body["total_spent"] == "0.00"


# ---------------------------------------------------------------------------
# 404 — fiche inconnue, anonymisee, absorbee
# ---------------------------------------------------------------------------


async def test_history_404_on_unknown_client(client, auth_headers):
    r = await client.get(
        f"/api/pos/clients/{uuid.uuid4()}/history", headers=auth_headers
    )
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


async def test_history_404_on_anonymized_client(client, auth_headers, open_drawer):
    fiche = await _create_client(client, auth_headers, email="effacee@example.com")
    await _sell(client, auth_headers, client_id=fiche["id"], amount="10.00")

    async with async_session() as db:
        row = (
            await db.execute(select(Client).where(Client.id == uuid.UUID(fiche["id"])))
        ).scalar_one()
        await ClientService(db).anonymize(
            client=row, user_id=None, reason="Demande cliente"
        )
        await db.commit()

    r = await client.get(
        f"/api/pos/clients/{fiche['id']}/history", headers=auth_headers
    )
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


async def test_history_404_on_merged_client(client, auth_headers, open_drawer):
    """Une fiche absorbee n'a plus d'historique propre : ses ventes ont ete
    repointees sur la fiche conservee, c'est celle-la qu'il faut ouvrir."""
    keeper = await _create_client(client, auth_headers, email="gardee@example.com")
    absorbed = await _create_client(client, auth_headers, email="absorbee@example.com")

    async with async_session() as db:
        row = (
            await db.execute(
                select(Client).where(Client.id == uuid.UUID(absorbed["id"]))
            )
        ).scalar_one()
        row.merged_into_client_id = uuid.UUID(keeper["id"])
        row.merged_at = datetime.now(timezone.utc)
        await db.commit()

    r = await client.get(
        f"/api/pos/clients/{absorbed['id']}/history", headers=auth_headers
    )
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# Fiche back-office : `items` et `refunded` sur chaque transaction
# ---------------------------------------------------------------------------


async def test_admin_full_client_exposes_items_and_refunded(
    client, auth_headers, open_drawer
):
    fiche = await _create_client(client, auth_headers, email="fiche@example.com")
    sale = await _sell(
        client,
        auth_headers,
        client_id=fiche["id"],
        items=[
            {"label": "Manteau", "unit_price": "40.00", "quantity": 1},
            {"label": "Écharpe", "unit_price": "8.00", "quantity": 2},
        ],
    )
    await _cancel(client, auth_headers, sale["id"])

    body = (
        await client.get(f"/api/admin/clients/{fiche['id']}", headers=auth_headers)
    ).json()
    by_number = {t["transaction_number"]: t for t in body["transactions"]}
    original = by_number[sale["transaction_number"]]
    assert original["refunded"] is True
    # Le detail back-office n'est pas tronque a cinq lignes.
    assert [i["label"] for i in original["items"]] == ["Manteau", "Écharpe"]
    assert original["items"][1] == {
        "label": "Écharpe",
        "quantity": 2,
        "unit_price": "8.00",
    }
