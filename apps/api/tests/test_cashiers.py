# Nouveau test (PR8, docs/ARCHITECTURE_PR8.md §3, contrats J1/J2/J3) —
# vendeuses identifiees par code PIN : schema et triggers de la migration
# 0008, code PIN (bcrypt, suites triviales, rate-limit, desactivation),
# ventes/mouvements/cloture portant la vendeuse, releve en cours de journee,
# ligne « Vendeuse : … » sur le ticket, ventilation par vendeuse sur le Z, et
# non-inclusion de `cashier_id` dans la signature fiscale.
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core import rate_limit as rate_limit_module
from app.core.database import async_session, engine
from app.models.cash_movement import CashMovement
from app.models.cashier import Cashier
from app.models.jet import JournalEvent
from app.models.pos import CashDrawer, Transaction, ZReport
from app.services.cashier_service import UNIDENTIFIED_LABEL, WeakPin, validate_pin
from app.services.fiscal import FiscalService
from app.services.receipt import CASHIER_LINE_PREFIX, CLIENT_LINE_PREFIX, apply_client_line

pytestmark = pytest.mark.anyio

PIN = "7391"
OTHER_PIN = "8462"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_cashier(client, auth_headers, name: str, pin: str | None = PIN) -> dict:
    body: dict = {"display_name": name}
    if pin is not None:
        body["pin"] = pin
    r = await client.post("/api/admin/cashiers", json=body, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()["cashier"]


async def _identify(client, auth_headers, cashier_id: str, pin: str):
    return await client.post(
        "/api/pos/cashiers/identify",
        json={"cashier_id": cashier_id, "pin": pin},
        headers=auth_headers,
    )


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
    return r


async def _set_cashier_required(client, auth_headers, required: bool) -> None:
    r = await client.put(
        "/api/admin/settings/pos", json={"cashier_required": required}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["cashier_required"] is required


async def _jet_types() -> list[str]:
    async with async_session() as db:
        rows = (
            await db.execute(select(JournalEvent).order_by(JournalEvent.seq.asc()))
        ).scalars().all()
        return [row.event_type for row in rows]


# ---------------------------------------------------------------------------
# Code PIN — fonction pure (§3 : `weak_pin`)
# ---------------------------------------------------------------------------


def test_validate_pin_accepts_four_digits():
    assert validate_pin("7391") == "7391"
    assert validate_pin(" 5074 ") == "5074"


def test_validate_pin_rejects_bad_format_and_trivial_sequences():
    for raw in ("", None, "123", "12345", "12a4", "  "):
        with pytest.raises(WeakPin) as exc:
            validate_pin(raw)
        assert exc.value.code == "weak_pin"
        assert exc.value.status_code == 422

    trivial = ["1234", "4321", "0123", "9876", "2580"] + [f"{d}" * 4 for d in range(10)]
    for raw in trivial:
        with pytest.raises(WeakPin):
            validate_pin(raw)


# ---------------------------------------------------------------------------
# Migration 0008 — schema et triggers (§3)
# ---------------------------------------------------------------------------


async def test_migration_0008_added_cashier_columns():
    """Les colonnes du contrat J1 existent et sont toutes NULLABLE (aucune
    vente anterieure a PR8 n'a de vendeuse a declarer)."""
    expected = {
        "transactions": ["cashier_id"],
        "cash_movements": ["cashier_id"],
        "z_reports": ["cashier_id"],
        "cash_drawers": [
            "opened_by_cashier_id",
            "closed_by_cashier_id",
            "current_cashier_id",
        ],
    }
    async with engine.begin() as conn:
        for table, columns in expected.items():
            present = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT column_name, is_nullable FROM information_schema.columns "
                            "WHERE table_name = :t"
                        ),
                        {"t": table},
                    )
                ).all()
            )
            for column in columns:
                assert column in present, (table, column)
                assert present[column] == "YES", (table, column)


async def test_cashiers_display_name_is_unique_case_insensitively(client, auth_headers):
    await _create_cashier(client, auth_headers, "Léa")
    r = await client.post(
        "/api/admin/cashiers", json={"display_name": "  léa  ", "pin": OTHER_PIN}, headers=auth_headers
    )
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "cashier_exists"


async def test_trigger_freezes_cashier_id_on_transactions(client, auth_headers, open_drawer):
    """`cashier_id` est hors signature MAIS gele : contrairement a
    `client_id`, une vente ne se réattribue pas apres coup."""
    cashier = await _create_cashier(client, auth_headers, "Léa")
    await _identify(client, auth_headers, cashier["id"], PIN)
    sale = (await _sell(client, auth_headers)).json()

    other = await _create_cashier(client, auth_headers, "Manon", pin=OTHER_PIN)
    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE transactions SET cashier_id = :c WHERE id = :id"),
                {"c": other["id"], "id": sale["id"]},
            )

    # Et `client_id` reste, lui, mutable (non-regression PR3/E3).
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE transactions SET client_id = NULL WHERE id = :id"), {"id": sale["id"]}
        )


async def test_trigger_freezes_cashier_id_on_cash_movements(client, auth_headers, open_drawer):
    cashier = await _create_cashier(client, auth_headers, "Léa")
    await _identify(client, auth_headers, cashier["id"], PIN)
    r = await client.post(
        "/api/pos/cash-movements",
        json={"direction": "out", "amount": "20.00", "reason": "bank_deposit"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    movement_id = r.json()["id"]

    with pytest.raises(DBAPIError, match="NF525"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE cash_movements SET cashier_id = NULL WHERE id = :id"),
                {"id": movement_id},
            )


# ---------------------------------------------------------------------------
# Identification (§3 : hash bcrypt, OK/KO, rate-limit, desactivation, JET)
# ---------------------------------------------------------------------------


async def test_pin_is_stored_as_a_bcrypt_hash_and_never_returned(client, auth_headers):
    cashier = await _create_cashier(client, auth_headers, "Léa")
    assert "pin" not in cashier and "pin_hash" not in cashier
    assert cashier["has_pin"] is True

    async with async_session() as db:
        row = (
            await db.execute(select(Cashier).where(Cashier.id == uuid.UUID(cashier["id"])))
        ).scalar_one()
        assert row.pin_hash != PIN
        assert row.pin_hash.startswith("$2b$")
        assert PIN not in row.pin_hash


async def test_identify_success_then_failure(client, auth_headers, open_drawer):
    cashier = await _create_cashier(client, auth_headers, "Léa")

    ok = await _identify(client, auth_headers, cashier["id"], PIN)
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"cashier": {"id": cashier["id"], "display_name": "Léa"}}

    ko = await _identify(client, auth_headers, cashier["id"], "1357")
    assert ko.status_code == 401, ko.text
    assert ko.json()["code"] == "invalid_pin"
    assert ko.json()["detail"] == "Code incorrect."


async def test_identify_sets_current_cashier_on_the_open_drawer(client, auth_headers, open_drawer):
    cashier = await _create_cashier(client, auth_headers, "Léa")
    await _identify(client, auth_headers, cashier["id"], PIN)

    current = await client.get("/api/pos/drawer/current", headers=auth_headers)
    assert current.status_code == 200, current.text
    assert current.json()["current_cashier"] == {"id": cashier["id"], "display_name": "Léa"}


async def test_release_clears_the_current_cashier(client, auth_headers, open_drawer):
    cashier = await _create_cashier(client, auth_headers, "Léa")
    await _identify(client, auth_headers, cashier["id"], PIN)

    r = await client.post("/api/pos/cashiers/release", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["released"] is True
    assert r.json()["cashier"]["display_name"] == "Léa"

    current = await client.get("/api/pos/drawer/current", headers=auth_headers)
    assert current.json()["current_cashier"] is None

    # Relever deux fois de suite n'est pas une erreur : l'etat voulu est
    # atteint.
    again = await client.post("/api/pos/cashiers/release", headers=auth_headers)
    assert again.status_code == 200
    assert again.json()["released"] is False


async def test_identify_is_rate_limited_after_five_attempts(client, auth_headers, open_drawer):
    cashier = await _create_cashier(client, auth_headers, "Léa")

    for _ in range(5):
        r = await _identify(client, auth_headers, cashier["id"], "1357")
        assert r.status_code == 401, r.text

    blocked = await _identify(client, auth_headers, cashier["id"], "1357")
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["code"] == "pin_rate_limited"
    assert blocked.json()["retry_after"] >= 1
    assert "Retry-After" in blocked.headers

    # Meme avec le BON code : le blocage porte sur la vendeuse et l'IP.
    still_blocked = await _identify(client, auth_headers, cashier["id"], PIN)
    assert still_blocked.status_code == 429

    # Une autre vendeuse depuis le meme poste n'est pas bloquee (compteur
    # par vendeuse ET par IP).
    other = await _create_cashier(client, auth_headers, "Manon", pin=OTHER_PIN)
    assert (await _identify(client, auth_headers, other["id"], OTHER_PIN)).status_code == 200


async def test_successful_identification_resets_the_attempt_counter(
    client, auth_headers, open_drawer
):
    cashier = await _create_cashier(client, auth_headers, "Léa")
    for _ in range(4):
        assert (await _identify(client, auth_headers, cashier["id"], "1357")).status_code == 401
    assert (await _identify(client, auth_headers, cashier["id"], PIN)).status_code == 200
    for _ in range(4):
        assert (await _identify(client, auth_headers, cashier["id"], "1357")).status_code == 401


async def test_deactivated_cashier_cannot_identify(client, auth_headers, open_drawer):
    cashier = await _create_cashier(client, auth_headers, "Léa")
    await _identify(client, auth_headers, cashier["id"], PIN)

    r = await client.put(
        f"/api/admin/cashiers/{cashier['id']}", json={"active": False}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["cashier"]["active"] is False
    assert r.json()["cashier"]["deactivated_at"] is not None

    # Elle ne tient plus la caisse (sinon les ventes suivantes lui seraient
    # encore attribuees alors qu'elle ne peut plus se reconnecter).
    current = await client.get("/api/pos/drawer/current", headers=auth_headers)
    assert current.json()["current_cashier"] is None

    ko = await _identify(client, auth_headers, cashier["id"], PIN)
    assert ko.status_code == 401
    assert ko.json()["code"] == "invalid_pin"

    # Absente de l'ecran de caisse, presente dans l'admin (reactivable).
    pos_list = await client.get("/api/pos/cashiers", headers=auth_headers)
    assert pos_list.json()["cashiers"] == []
    admin_list = await client.get("/api/admin/cashiers", headers=auth_headers)
    assert [c["display_name"] for c in admin_list.json()["cashiers"]] == ["Léa"]


async def test_cashier_without_pin_cannot_identify(client, auth_headers, open_drawer):
    cashier = await _create_cashier(client, auth_headers, "Léa", pin=None)
    assert cashier["has_pin"] is False

    ko = await _identify(client, auth_headers, cashier["id"], PIN)
    assert ko.status_code == 401
    assert ko.json()["code"] == "invalid_pin"

    r = await client.put(
        f"/api/admin/cashiers/{cashier['id']}/pin", json={"pin": PIN}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["cashier"]["has_pin"] is True
    assert (await _identify(client, auth_headers, cashier["id"], PIN)).status_code == 200


async def test_admin_refuses_a_weak_pin(client, auth_headers):
    r = await client.post(
        "/api/admin/cashiers", json={"display_name": "Léa", "pin": "1234"}, headers=auth_headers
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "weak_pin"

    cashier = await _create_cashier(client, auth_headers, "Léa")
    r = await client.put(
        f"/api/admin/cashiers/{cashier['id']}/pin", json={"pin": "0000"}, headers=auth_headers
    )
    assert r.status_code == 422
    assert r.json()["code"] == "weak_pin"


async def test_jet_never_carries_the_pin(client, auth_headers, open_drawer):
    """Le JET est immuable : un secret qui y tomberait ne pourrait plus
    jamais en sortir."""
    cashier = await _create_cashier(client, auth_headers, "Léa")
    await client.put(
        f"/api/admin/cashiers/{cashier['id']}/pin", json={"pin": OTHER_PIN}, headers=auth_headers
    )
    await _identify(client, auth_headers, cashier["id"], "1357")
    await _identify(client, auth_headers, cashier["id"], OTHER_PIN)
    await client.post("/api/pos/cashiers/release", headers=auth_headers)

    async with async_session() as db:
        events = (
            await db.execute(select(JournalEvent).order_by(JournalEvent.seq.asc()))
        ).scalars().all()
        dump = json.dumps([e.payload for e in events], ensure_ascii=False)

    assert PIN not in dump and OTHER_PIN not in dump and "1357" not in dump
    assert "pin_hash" not in dump and "$2b$" not in dump

    types = [e.event_type for e in events]
    for expected in (
        "cashier.created",
        "cashier.pin_changed",
        "cashier.pin_rejected",
        "cashier.identified",
        "cashier.released",
    ):
        assert expected in types, types

    rejected = next(e for e in events if e.event_type == "cashier.pin_rejected")
    assert rejected.payload["cashier_id"] == cashier["id"]


# ---------------------------------------------------------------------------
# Ventes, mouvements, releve (§3)
# ---------------------------------------------------------------------------


async def test_sale_carries_the_current_cashier(client, auth_headers, open_drawer):
    cashier = await _create_cashier(client, auth_headers, "Léa")
    await _identify(client, auth_headers, cashier["id"], PIN)
    sale = (await _sell(client, auth_headers)).json()

    async with async_session() as db:
        tx = (
            await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))
        ).scalar_one()
        assert str(tx.cashier_id) == cashier["id"]


async def test_sale_without_identification_is_allowed_by_default(
    client, auth_headers, open_drawer
):
    """`pos.cashier_required` vaut false par defaut : la boutique tourne
    exactement comme avant PR8 tant que le manager n'a rien active."""
    sale = await _sell(client, auth_headers)
    assert sale.status_code == 201, sale.text

    async with async_session() as db:
        tx = (
            await db.execute(
                select(Transaction).where(Transaction.id == uuid.UUID(sale.json()["id"]))
            )
        ).scalar_one()
        assert tx.cashier_id is None


async def test_cashier_required_blocks_sales_and_movements(client, auth_headers, open_drawer):
    await _set_cashier_required(client, auth_headers, True)

    sale = await _sell(client, auth_headers)
    assert sale.status_code == 422, sale.text
    assert sale.json()["code"] == "cashier_required"

    movement = await client.post(
        "/api/pos/cash-movements",
        json={"direction": "out", "amount": "20.00", "reason": "bank_deposit"},
        headers=auth_headers,
    )
    assert movement.status_code == 422
    assert movement.json()["code"] == "cashier_required"

    cashier = await _create_cashier(client, auth_headers, "Léa")
    await _identify(client, auth_headers, cashier["id"], PIN)
    assert (await _sell(client, auth_headers)).status_code == 201


async def test_cashier_required_blocks_the_opening_of_the_drawer(client, auth_headers):
    await _set_cashier_required(client, auth_headers, True)

    refused = await client.post(
        "/api/pos/drawer/open", json={"opening_amount": "100.00"}, headers=auth_headers
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "cashier_required"

    # Identification AVANT ouverture : aucun tiroir n'existe encore, le
    # front conserve la vendeuse et la renvoie dans le corps de l'ouverture.
    cashier = await _create_cashier(client, auth_headers, "Léa")
    identified = await _identify(client, auth_headers, cashier["id"], PIN)
    assert identified.status_code == 200, identified.text

    opened = await client.post(
        "/api/pos/drawer/open",
        json={"opening_amount": "100.00", "cashier_id": cashier["id"]},
        headers=auth_headers,
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["opened_by_cashier_id"] == cashier["id"]
    assert opened.json()["current_cashier_id"] == cashier["id"]

    current = await client.get("/api/pos/drawer/current", headers=auth_headers)
    assert current.json()["current_cashier"]["display_name"] == "Léa"
    assert (await _sell(client, auth_headers)).status_code == 201


async def test_handover_switches_the_cashier_on_later_sales(client, auth_headers, open_drawer):
    lea = await _create_cashier(client, auth_headers, "Léa")
    manon = await _create_cashier(client, auth_headers, "Manon", pin=OTHER_PIN)

    await _identify(client, auth_headers, lea["id"], PIN)
    first = (await _sell(client, auth_headers, "10.00")).json()

    await client.post("/api/pos/cashiers/release", headers=auth_headers)
    await _identify(client, auth_headers, manon["id"], OTHER_PIN)
    second = (await _sell(client, auth_headers, "20.00")).json()

    async with async_session() as db:
        rows = {
            str(t.id): t.cashier_id
            for t in (await db.execute(select(Transaction))).scalars().all()
        }
    assert str(rows[first["id"]]) == lea["id"]
    assert str(rows[second["id"]]) == manon["id"]


async def test_refund_and_movement_carry_the_cashier(client, auth_headers, open_drawer):
    cashier = await _create_cashier(client, auth_headers, "Léa")
    await _identify(client, auth_headers, cashier["id"], PIN)

    sale = (await _sell(client, auth_headers)).json()
    refund = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel",
        json={"reason": "Erreur de saisie"},
        headers=auth_headers,
    )
    assert refund.status_code == 201, refund.text

    movement = await client.post(
        "/api/pos/cash-movements",
        json={"direction": "in", "amount": "15.00", "reason": "float_top_up"},
        headers=auth_headers,
    )
    assert movement.status_code == 200, movement.text

    async with async_session() as db:
        refund_tx = (
            await db.execute(
                select(Transaction).where(Transaction.id == uuid.UUID(refund.json()["id"]))
            )
        ).scalar_one()
        assert str(refund_tx.cashier_id) == cashier["id"]
        mv = (
            await db.execute(
                select(CashMovement).where(CashMovement.id == uuid.UUID(movement.json()["id"]))
            )
        ).scalar_one()
        assert str(mv.cashier_id) == cashier["id"]

    # Le ticket d'annulation porte aussi la vendeuse.
    assert f"{CASHIER_LINE_PREFIX}Léa" in refund.json()["receipt_text"]


# ---------------------------------------------------------------------------
# Signature fiscale — `cashier_id` hors payload (§3)
# ---------------------------------------------------------------------------


async def test_cashier_id_is_not_part_of_the_fiscal_signature(client, auth_headers, open_drawer):
    """Le hash d'une vente est le MEME avec ou sans vendeuse : `cashier_id`
    n'entre pas dans `_transaction_payload`."""
    cashier = await _create_cashier(client, auth_headers, "Léa")
    await _identify(client, auth_headers, cashier["id"], PIN)
    sale = (await _sell(client, auth_headers)).json()

    async with async_session() as db:
        tx = (
            await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))
        ).scalar_one()
        assert tx.cashier_id is not None

        fiscal = FiscalService(db)
        payload = await fiscal._transaction_payload(tx, tx.previous_hash)

        # 1. Le payload signe ne mentionne la vendeuse nulle part.
        assert "cashier" not in json.dumps(payload, ensure_ascii=False)
        # 2. Le hash stocke se recalcule a l'identique...
        assert fiscal._hmac(payload) == tx.hash_chain
        # 3. ... et retirer la vendeuse ne le change pas (aucun flush : on
        #    ne touche pas a la ligne, on verifie la fonction de signature).
        with db.no_autoflush:
            tx.cashier_id = None
            assert fiscal._hmac(await fiscal._transaction_payload(tx, tx.previous_hash)) == (
                tx.hash_chain
            )
        await db.rollback()

        assert (await FiscalService(db).verify_chain_integrity())["valid"] is True


# ---------------------------------------------------------------------------
# Ticket (§J2) — ligne « Vendeuse : … » figee a l'emission
# ---------------------------------------------------------------------------


async def test_receipt_carries_the_cashier_line_under_the_date(client, auth_headers, open_drawer):
    cashier = await _create_cashier(client, auth_headers, "Léa")
    await _identify(client, auth_headers, cashier["id"], PIN)
    sale = (await _sell(client, auth_headers)).json()

    lines = sale["receipt_text"].split("\n")
    date_index = next(i for i, line in enumerate(lines) if line.startswith("Date: "))
    assert lines[date_index + 1] == f"{CASHIER_LINE_PREFIX}Léa"


async def test_the_cashier_line_does_not_follow_a_later_handover(
    client, auth_headers, open_drawer
):
    """Contrairement a la ligne « Client : … » (rendue a la lecture), la
    vendeuse est ecrite DANS le texte stocke : le ticket deja remis garde le
    nom de celle qui a encaisse."""
    lea = await _create_cashier(client, auth_headers, "Léa")
    manon = await _create_cashier(client, auth_headers, "Manon", pin=OTHER_PIN)
    await _identify(client, auth_headers, lea["id"], PIN)
    sale = (await _sell(client, auth_headers)).json()

    await client.post("/api/pos/cashiers/release", headers=auth_headers)
    await _identify(client, auth_headers, manon["id"], OTHER_PIN)

    reread = await client.get(
        f"/api/pos/transactions/{sale['id']}/receipt", headers=auth_headers
    )
    assert reread.status_code == 200, reread.text
    assert f"{CASHIER_LINE_PREFIX}Léa" in reread.json()["text"]
    assert "Manon" not in reread.json()["text"]


def test_client_line_is_inserted_below_the_cashier_line():
    """Les deux lignes coexistent dans un ordre stable : Date, Vendeuse,
    Client — y compris apres une relecture (qui reecrit la ligne client)."""
    content = "\n".join(
        ["Ticket #1", "Date: 15/09/2026 10:00", f"{CASHIER_LINE_PREFIX}Léa", "-" * 42]
    )
    rendered = apply_client_line(content, "Camille D.").split("\n")
    assert rendered[1] == "Date: 15/09/2026 10:00"
    assert rendered[2] == f"{CASHIER_LINE_PREFIX}Léa"
    assert rendered[3] == f"{CLIENT_LINE_PREFIX}Camille D."
    # Idempotent : relire deux fois ne duplique ni n'inverse rien.
    assert apply_client_line("\n".join(rendered), "Camille D.").split("\n") == rendered


# ---------------------------------------------------------------------------
# Rapport Z — ventilation par vendeuse (§3)
# ---------------------------------------------------------------------------


async def test_z_report_breaks_sales_down_by_cashier(client, auth_headers, open_drawer):
    lea = await _create_cashier(client, auth_headers, "Léa")
    manon = await _create_cashier(client, auth_headers, "Manon", pin=OTHER_PIN)

    # Une vente sans personne, deux pour Léa, une pour Manon.
    await _sell(client, auth_headers, "5.00")
    await _identify(client, auth_headers, lea["id"], PIN)
    await _sell(client, auth_headers, "10.00")
    await _sell(client, auth_headers, "20.00")
    await client.post("/api/pos/cashiers/release", headers=auth_headers)
    await _identify(client, auth_headers, manon["id"], OTHER_PIN)
    await _sell(client, auth_headers, "30.00")

    z = await client.post(
        "/api/pos/drawer/close", json={"closing_amount": "165.00"}, headers=auth_headers
    )
    assert z.status_code == 200, z.text
    body = z.json()

    assert body["by_cashier"] == [
        {
            "cashier_id": lea["id"],
            "display_name": "Léa",
            "sales_count": 2,
            "sales_total": 30.0,
            "refunds_count": 0,
            "refunds_total": 0.0,
            "net_total": 30.0,
        },
        {
            "cashier_id": manon["id"],
            "display_name": "Manon",
            "sales_count": 1,
            "sales_total": 30.0,
            "refunds_count": 0,
            "refunds_total": 0.0,
            "net_total": 30.0,
        },
        {
            "cashier_id": None,
            "display_name": UNIDENTIFIED_LABEL,
            "sales_count": 1,
            "sales_total": 5.0,
            "refunds_count": 0,
            "refunds_total": 0.0,
            "net_total": 5.0,
        },
    ]
    # La vendeuse qui cloture est notee sur le Z et sur le tiroir.
    assert body["cashier_id"] == manon["id"]
    async with async_session() as db:
        z_row = (
            await db.execute(select(ZReport).where(ZReport.id == uuid.UUID(body["id"])))
        ).scalar_one()
        assert str(z_row.cashier_id) == manon["id"]
        drawer = (
            await db.execute(
                select(CashDrawer).where(CashDrawer.id == uuid.UUID(body["cash_drawer_id"]))
            )
        ).scalar_one()
        assert str(drawer.closed_by_cashier_id) == manon["id"]

    # Meme ventilation a la relecture (elle est recalculee, pas scellee).
    detail = await client.get(f"/api/pos/z-reports/{body['id']}", headers=auth_headers)
    assert detail.json()["by_cashier"] == body["by_cashier"]
    listing = await client.get("/api/pos/z-reports", headers=auth_headers)
    assert listing.json()["z_reports"][0]["by_cashier"] == body["by_cashier"]


async def test_z_report_by_cashier_is_net_of_refunds(client, auth_headers, open_drawer):
    """PR9/K0 — une vendeuse dont l'unique vente est annulee finit a zero net.

    Le brut, lui, ne bouge pas : la vente a bien eu lieu, et c'est ce que
    verifie le rapprochement avec la bande de caisse.
    """
    lea = await _create_cashier(client, auth_headers, "Léa")
    await _identify(client, auth_headers, lea["id"], PIN)
    sale = await _sell(client, auth_headers, "40.00")
    assert sale.status_code == 201, sale.text
    cancelled = await client.post(
        f"/api/pos/transactions/{sale.json()['id']}/cancel",
        json={"reason": "Article défectueux"},
        headers=auth_headers,
    )
    assert cancelled.status_code == 201, cancelled.text

    z = await client.post(
        "/api/pos/drawer/close", json={"closing_amount": "100.00"}, headers=auth_headers
    )
    assert z.status_code == 200, z.text
    assert z.json()["by_cashier"] == [
        {
            "cashier_id": lea["id"],
            "display_name": "Léa",
            "sales_count": 1,
            "sales_total": 40.0,
            "refunds_count": 1,
            "refunds_total": 40.0,
            "net_total": 0.0,
        },
    ]


async def test_z_report_pdf_stays_deterministic_with_cashiers(client, auth_headers, open_drawer):
    cashier = await _create_cashier(client, auth_headers, "Léa")
    await _identify(client, auth_headers, cashier["id"], PIN)
    await _sell(client, auth_headers, "12.00")
    z = await client.post(
        "/api/pos/drawer/close", json={"closing_amount": "112.00"}, headers=auth_headers
    )
    z_id = z.json()["id"]

    first = await client.get(f"/api/pos/z-reports/{z_id}/pdf", headers=auth_headers)
    second = await client.get(f"/api/pos/z-reports/{z_id}/pdf", headers=auth_headers)
    assert first.status_code == 200 and second.status_code == 200
    assert first.content == second.content
    assert first.headers["X-PDF-SHA256"] == second.headers["X-PDF-SHA256"]
    assert b"Ventes par vendeuse" in first.content
    # PR9/K0 — les trois colonnes brut / annulations / net.
    assert b"Annulations" in first.content
    assert b"Net" in first.content


# ---------------------------------------------------------------------------
# Reglage `pos.cashier_required`
# ---------------------------------------------------------------------------


async def test_pos_setting_defaults_to_false_and_round_trips(client, auth_headers):
    r = await client.get("/api/admin/settings/pos", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"cashier_required": False}

    await _set_cashier_required(client, auth_headers, True)
    r = await client.get("/api/admin/settings/pos", headers=auth_headers)
    assert r.json() == {"cashier_required": True}
    assert "config.changed" in await _jet_types()


async def test_cashier_routes_require_authentication(client):
    assert (await client.get("/api/pos/cashiers")).status_code == 401
    assert (await client.get("/api/admin/cashiers")).status_code == 401
    assert (
        await client.post(
            "/api/pos/cashiers/identify", json={"cashier_id": str(uuid.uuid4()), "pin": PIN}
        )
    ).status_code == 401


async def test_identify_unknown_cashier_is_a_404(client, auth_headers):
    r = await _identify(client, auth_headers, str(uuid.uuid4()), PIN)
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found"
    # Aucun compteur de rate-limit pose sur un identifiant inexistant.
    assert not [k for k in rate_limit_module._buckets if k.startswith("cashier_pin:")]
