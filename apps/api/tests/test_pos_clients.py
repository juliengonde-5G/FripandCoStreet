# Nouveau test (PR7, docs/ARCHITECTURE_PR7.md §3, contrat I3) — client en
# caisse SANS fidelite : telephone facultatif/e-mail facultatif (mais au
# moins un des deux), recherche, creation idempotente, rattachement d'une
# vente des sa creation (hors signature), detachement, ticket « Client :
# Prenom N. », JET sans donnee personnelle.
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.database import async_session, engine
from app.models.client import Client
from app.models.jet import JournalEvent
from app.models.pos import Transaction
from app.services import brevo_contacts
from app.services.client_service import (
    ClientService,
    ContactRequired,
    InvalidPhone,
    mask_phone,
    normalize_phone,
    phone_search_digits,
)
from app.services.fiscal import FiscalService
from app.services.receipt import format_client_label

pytestmark = pytest.mark.anyio


async def _sell(client, auth_headers, amount: str = "10.00", client_id: str | None = None) -> dict:
    body = {
        "client_uuid": str(uuid.uuid4()),
        "items": [{"label": "Robe", "unit_price": amount, "quantity": 1}],
        "payments": [{"method": "cash", "amount": amount, "tendered_amount": amount}],
    }
    if client_id is not None:
        body["client_id"] = client_id
    r = await client.post("/api/pos/transactions", json=body, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()


async def _create_client(client, auth_headers, **payload) -> dict:
    r = await client.post("/api/pos/clients", json=payload, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Normalisation / masquage (fonctions pures)
# ---------------------------------------------------------------------------


def test_normalize_phone_strips_separators_and_keeps_the_dialled_format():
    # Espaces, points, tirets, parentheses, insecables : retires.
    assert normalize_phone("06 12 34 56 78") == "0612345678"
    assert normalize_phone("06.12.34.56.78") == "0612345678"
    assert normalize_phone("06-12-34-56-78") == "0612345678"
    assert normalize_phone(" (06) 12 34 56 78 ") == "0612345678"
    # Aucune conversion d'un format vers un autre : ce que la cliente a
    # dicte est ce qui est stocke.
    assert normalize_phone("+33 6 12 34 56 78") == "+33612345678"
    assert normalize_phone("0033 6 12 34 56 78") == "0033612345678"
    # Le `+` n'est conserve QU'en tete.
    assert normalize_phone("") is None
    assert normalize_phone(None) is None
    assert normalize_phone("   ") is None


def test_normalize_phone_rejects_garbage():
    for raw in ("pas-un-numero", "06 12 34 56 7A", "12345", "++33612345678", "06+12"):
        with pytest.raises(InvalidPhone) as exc:
            normalize_phone(raw)
        assert exc.value.code == "invalid_phone"
        assert exc.value.status_code == 422


def test_phone_search_digits_only_matches_numeric_queries():
    assert phone_search_digits("06 12") == "0612"
    assert phone_search_digits("+33 6 12") == "33612"
    assert phone_search_digits("martin") is None
    assert phone_search_digits("a@b.fr") is None


def test_mask_phone_keeps_only_the_last_two_digits():
    assert mask_phone("0612345678") == "••••••••78"
    assert mask_phone("+33612345678") == "••••••••••78"
    assert mask_phone(None) is None
    assert "12345" not in (mask_phone("0612345678") or "")


def test_format_client_label_is_first_name_plus_initial():
    assert format_client_label("Alice", "Martin") == "Alice M."
    assert format_client_label("Alice", None) == "Alice"
    assert format_client_label("  Alice  ", "  martin ") == "Alice M."
    # Sans prenom, rien n'est imprime.
    assert format_client_label(None, "Martin") is None
    assert format_client_label("", "Martin") is None


# ---------------------------------------------------------------------------
# Service — create_or_get / search / anonymisation / Brevo
# ---------------------------------------------------------------------------


async def test_create_or_get_without_email_creates_a_phone_only_client():
    async with async_session() as db:
        client, created = await ClientService(db).create_or_get(
            phone="06 12 34 56 78", first_name="Alice", user_id=None
        )
        await db.commit()
        assert created is True
        assert client.email is None
        assert client.phone == "0612345678"


async def test_create_or_get_is_idempotent_on_email_and_on_phone():
    async with async_session() as db:
        service = ClientService(db)
        first, created_1 = await service.create_or_get(email="Idem@Example.COM", user_id=None)
        await db.commit()
        assert created_1 is True

        second, created_2 = await service.create_or_get(email="idem@example.com", user_id=None)
        await db.commit()
        assert created_2 is False
        assert second.id == first.id

    async with async_session() as db:
        service = ClientService(db)
        phone_client, created_3 = await service.create_or_get(phone="+33612345678", user_id=None)
        await db.commit()
        assert created_3 is True
        # Meme numero, saisi avec des espaces : meme fiche.
        again, created_4 = await service.create_or_get(phone="+33 6 12 34 56 78", user_id=None)
        await db.commit()
        assert created_4 is False
        assert again.id == phone_client.id


async def test_create_or_get_completes_a_fiche_without_overwriting():
    async with async_session() as db:
        service = ClientService(db)
        created, _ = await service.create_or_get(
            email="a-completer@example.com", first_name="Alice", user_id=None
        )
        await db.commit()
        client_id = created.id

    async with async_session() as db:
        service = ClientService(db)
        again, was_created = await service.create_or_get(
            email="a-completer@example.com",
            phone="06 12 00 00 00",
            last_name="Martin",
            user_id=None,
        )
        await db.commit()
        assert was_created is False
        assert again.id == client_id
        assert again.phone == "0612000000"
        assert again.last_name == "Martin"
        assert again.first_name == "Alice"  # jamais efface


async def test_create_or_get_never_steals_a_contact_from_another_fiche(client, auth_headers):
    """Une cliente dont l'adresse est deja connue, mais dont le numero
    appartient deja a une AUTRE fiche : la saisie ne recopie pas le numero
    (ce serait une violation d'unicite au flush, donc un 500 en caisse).
    Les deux fiches restent distinctes."""
    await _create_client(client, auth_headers, email="partage@example.com")
    await _create_client(client, auth_headers, phone="0655555555", first_name="Autre")

    body = await _create_client(
        client, auth_headers, email="partage@example.com", phone="06 55 55 55 55"
    )
    assert body["created"] is False
    assert body["client"]["phone_masked"] is None

    async with async_session() as db:
        rows = (await db.execute(select(Client))).scalars().all()
        assert len(rows) == 2


async def test_create_or_get_requires_a_contact():
    async with async_session() as db:
        with pytest.raises(ContactRequired) as exc:
            await ClientService(db).create_or_get(first_name="Sans", last_name="Contact")
        assert exc.value.code == "contact_required"
        assert exc.value.status_code == 422


async def test_database_refuses_a_client_without_any_contact():
    """Le garde-fou n'est pas qu'applicatif : la contrainte CHECK posee par
    la migration 0007 refuse la ligne, meme ecrite en SQL direct."""
    async with async_session() as db:
        db.add(Client(first_name="Sans", last_name="Contact"))
        with pytest.raises(IntegrityError, match="ck_clients_contact_required"):
            await db.flush()


async def test_search_covers_email_name_and_phone():
    async with async_session() as db:
        service = ClientService(db)
        await service.create_or_get(
            email="chercheuse@example.com", first_name="Camille", last_name="Durand", user_id=None
        )
        await service.create_or_get(phone="+33 6 99 88 77 66", first_name="Zoe", user_id=None)
        await db.commit()

    async with async_session() as db:
        service = ClientService(db)
        assert [c.first_name for c in await service.search("chercheuse")] == ["Camille"]
        assert [c.first_name for c in await service.search("durand")] == ["Camille"]
        assert [c.first_name for c in await service.search("camil")] == ["Camille"]
        # Telephone : avec espaces, avec `+33`, ou juste les derniers chiffres.
        assert [c.first_name for c in await service.search("+33 6 99 88 77 66")] == ["Zoe"]
        assert [c.first_name for c in await service.search("699887766")] == ["Zoe"]
        assert [c.first_name for c in await service.search("88 77 66")] == ["Zoe"]
        assert await service.search("06 00 00 00 00") == []


async def test_anonymize_erases_the_phone_too():
    async with async_session() as db:
        service = ClientService(db)
        created, _ = await service.create_or_get(
            email="rgpd-tel@example.com", phone="06 12 34 56 78", first_name="Alice", user_id=None
        )
        await db.commit()
        client_id = created.id

    async with async_session() as db:
        client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one()
        await ClientService(db).anonymize(client=client, user_id=None, reason="Demande cliente")
        await db.commit()

    async with async_session() as db:
        refreshed = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one()
        assert refreshed.phone is None
        assert refreshed.first_name is None
        assert refreshed.email.endswith("@anonyme.invalid")
        assert refreshed.anonymized_at is not None


async def test_sync_brevo_is_a_noop_without_email(monkeypatch):
    """Brevo est un carnet d'adresses e-mail : une fiche « telephone seul »
    ne declenche AUCUN appel HTTP et ne marque aucune erreur sur la fiche."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"Brevo ne doit pas être appelé : {request.url}")

    monkeypatch.setattr(brevo_contacts, "_transport", httpx.MockTransport(handler))

    async with async_session() as db:
        service = ClientService(db)
        client, _ = await service.create_or_get(phone="0612345678", user_id=None)
        result = await service.sync_brevo(client, user_id=None)
        await db.commit()
        assert result == {"status": "skipped"}
        assert client.brevo_last_error is None
        assert client.brevo_synced_at is None


# ---------------------------------------------------------------------------
# Routes POS — authentification
# ---------------------------------------------------------------------------


async def test_pos_client_routes_require_a_jwt(client):
    r1 = await client.get("/api/pos/clients/search", params={"q": "alice"})
    r2 = await client.post("/api/pos/clients", json={"phone": "0612345678"})
    r3 = await client.delete(f"/api/pos/transactions/{uuid.uuid4()}/client")
    assert r1.status_code == 401
    assert r2.status_code == 401
    assert r3.status_code == 401


# ---------------------------------------------------------------------------
# POST /pos/clients
# ---------------------------------------------------------------------------


async def test_create_pos_client_with_phone_only(client, auth_headers):
    body = await _create_client(
        client, auth_headers, first_name="Alice", last_name="Martin", phone="06 12 34 56 78"
    )
    assert body["created"] is True
    fiche = body["client"]
    assert fiche["first_name"] == "Alice"
    assert fiche["email_masked"] is None
    # Coordonnees masquees en caisse — jamais le numero complet.
    assert fiche["phone_masked"] == "••••••••78"
    assert "0612345678" not in json.dumps(body)
    assert fiche["visits_count"] == 0
    assert fiche["last_visit_at"] is None


async def test_create_pos_client_twice_returns_the_same_fiche(client, auth_headers):
    first = await _create_client(client, auth_headers, email="Deux.Fois@Example.com")
    second = await _create_client(client, auth_headers, email="deux.fois@example.com")
    assert first["created"] is True
    assert second["created"] is False
    assert second["client"]["id"] == first["client"]["id"]


async def test_create_pos_client_without_contact_is_422_contact_required(client, auth_headers):
    r = await client.post(
        "/api/pos/clients", json={"first_name": "Sans", "last_name": "Contact"}, headers=auth_headers
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "contact_required"
    assert isinstance(body["detail"], str)


async def test_create_pos_client_with_invalid_phone_is_422(client, auth_headers):
    r = await client.post("/api/pos/clients", json={"phone": "pas-un-numero"}, headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_phone"


async def test_create_pos_client_records_consent_only_when_checked(client, auth_headers):
    opted_in = await _create_client(
        client, auth_headers, email="optin@example.com", newsletter_optin=True
    )
    opted_out = await _create_client(client, auth_headers, email="optout@example.com")
    assert opted_in["client"]["newsletter_optin"] is True
    assert opted_out["client"]["newsletter_optin"] is False

    async with async_session() as db:
        from app.models.client import Consent

        consents = (await db.execute(select(Consent))).scalars().all()
        assert len(consents) == 1
        assert consents[0].source.value == "pos"
        assert consents[0].granted is True


# ---------------------------------------------------------------------------
# GET /pos/clients/search
# ---------------------------------------------------------------------------


async def test_search_pos_clients_by_phone_returns_masked_fiche(client, auth_headers):
    await _create_client(
        client, auth_headers, first_name="Zoe", last_name="Blanc", phone="+33 6 99 88 77 66"
    )
    r = await client.get(
        "/api/pos/clients/search", params={"q": "06 99 88"}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    # `06 99 88` ne matche pas `+33699887766` : la recherche porte sur les
    # chiffres tels qu'ils sont stockes.
    assert r.json()["clients"] == []

    r2 = await client.get("/api/pos/clients/search", params={"q": "99 88 77"}, headers=auth_headers)
    assert r2.status_code == 200
    found = r2.json()["clients"]
    assert len(found) == 1
    assert found[0]["last_name"] == "Blanc"
    assert found[0]["phone_masked"] == "••••••••••66"
    assert "33699887766" not in r2.text


async def test_search_pos_clients_requires_two_characters(client, auth_headers):
    r = await client.get("/api/pos/clients/search", params={"q": "a"}, headers=auth_headers)
    assert r.status_code == 422


async def test_search_pos_clients_reports_visits_from_sales_only(
    client, auth_headers, open_drawer
):
    fiche = await _create_client(client, auth_headers, email="visiteuse@example.com")
    client_id = fiche["client"]["id"]

    await _sell(client, auth_headers, "10.00", client_id=client_id)
    second = await _sell(client, auth_headers, "20.00", client_id=client_id)
    # Une annulation n'est pas une visite.
    cancel = await client.post(
        f"/api/pos/transactions/{second['id']}/cancel",
        json={"reason": "Erreur de caisse"},
        headers=auth_headers,
    )
    assert cancel.status_code == 201, cancel.text

    r = await client.get(
        "/api/pos/clients/search", params={"q": "visiteuse"}, headers=auth_headers
    )
    found = r.json()["clients"][0]
    assert found["visits_count"] == 2
    assert found["last_visit_at"] is not None


async def test_search_pos_clients_excludes_anonymized_fiches(client, auth_headers):
    fiche = await _create_client(
        client, auth_headers, email="a-oublier@example.com", first_name="Oubliee"
    )
    client_id = fiche["client"]["id"]
    r = await client.post(
        f"/api/admin/clients/{client_id}/anonymize",
        json={"reason": "Demande cliente"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    search = await client.get(
        "/api/pos/clients/search", params={"q": "oubliee"}, headers=auth_headers
    )
    assert search.json()["clients"] == []


# ---------------------------------------------------------------------------
# POST /pos/transactions avec `client_id` — rattachement HORS signature
# ---------------------------------------------------------------------------


async def test_sale_with_client_id_links_the_fiche(client, auth_headers, open_drawer):
    fiche = await _create_client(
        client, auth_headers, first_name="Alice", last_name="Martin", phone="0612345678"
    )
    sale = await _sell(client, auth_headers, client_id=fiche["client"]["id"])

    assert sale["client"] is not None
    assert sale["client"]["id"] == fiche["client"]["id"]

    async with async_session() as db:
        tx = (
            await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))
        ).scalar_one()
        assert str(tx.client_id) == fiche["client"]["id"]


async def test_sale_with_unknown_client_id_is_422(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "client_id": str(uuid.uuid4()),
            "items": [{"label": "Robe", "unit_price": "10.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "10.00", "tendered_amount": "10.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "client_not_found"


async def test_client_id_is_outside_the_fiscal_signature(client, auth_headers, open_drawer):
    """§3 — « hash identique a une vente sans client (memes montants) ».

    Le payload signe contient l'identifiant et l'horodatage de la vente :
    deux ventes distinctes n'ont donc jamais le meme hash, meme a montants
    egaux. Ce qui se demontre — et qui est la propriete reellement exigee —
    c'est que `client_id` n'entre PAS dans la signature : le hash recalcule
    sur la MEME vente, une fois sa cliente detachee, est rigoureusement
    identique a celui qui a ete scelle quand elle y etait rattachee.
    """
    fiche = await _create_client(client, auth_headers, email="hors-hash@example.com")
    sale = await _sell(client, auth_headers, "10.00", client_id=fiche["client"]["id"])
    signed_hash = sale["hash_chain"]

    async with async_session() as db:
        service = FiscalService(db)
        tx = (
            await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))
        ).scalar_one()
        assert tx.client_id is not None
        payload = await service._transaction_payload(tx, tx.previous_hash)
        # Aucune trace de la cliente dans le payload signe, a aucun niveau.
        assert "client_id" not in json.dumps(payload)
        assert fiche["client"]["id"] not in json.dumps(payload)
        assert service._hmac(payload) == signed_hash

        # Meme vente, cliente detachee : le hash recalcule ne bouge pas.
        tx.client_id = None
        await db.flush()
        payload_without_client = await service._transaction_payload(tx, tx.previous_hash)
        assert payload_without_client == payload
        assert service._hmac(payload_without_client) == signed_hash
        await db.rollback()

    async with async_session() as db:
        assert (await FiscalService(db).verify_chain_integrity())["valid"] is True


async def test_sale_with_client_id_prints_first_name_and_initial_on_the_receipt(
    client, auth_headers, open_drawer
):
    fiche = await _create_client(
        client,
        auth_headers,
        first_name="Alice",
        last_name="Martin",
        email="ticket@example.com",
        phone="0612345678",
    )
    sale = await _sell(client, auth_headers, client_id=fiche["client"]["id"])
    receipt = sale["receipt_text"]

    assert "Client : Alice M." in receipt
    # Jamais de coordonnees sur un ticket remis en main propre.
    assert "ticket@example.com" not in receipt
    assert "0612345678" not in receipt
    assert "Martin" not in receipt


async def test_sale_without_client_has_no_client_line_on_the_receipt(
    client, auth_headers, open_drawer
):
    sale = await _sell(client, auth_headers)
    assert "Client :" not in sale["receipt_text"]
    assert sale["client"] is None


async def test_sale_with_a_client_without_first_name_prints_nothing(
    client, auth_headers, open_drawer
):
    fiche = await _create_client(client, auth_headers, last_name="Martin", phone="0611111111")
    sale = await _sell(client, auth_headers, client_id=fiche["client"]["id"])
    assert "Client :" not in sale["receipt_text"]


async def test_jet_client_linked_carries_no_personal_data(client, auth_headers, open_drawer):
    fiche = await _create_client(
        client,
        auth_headers,
        first_name="Alice",
        last_name="Martin",
        email="jet@example.com",
        phone="0612345678",
    )
    sale = await _sell(client, auth_headers, client_id=fiche["client"]["id"])

    async with async_session() as db:
        events = (
            await db.execute(select(JournalEvent).where(JournalEvent.event_type == "client.linked"))
        ).scalars().all()
        assert len(events) == 1
        assert events[0].payload == {
            "client_id": fiche["client"]["id"],
            "transaction_id": sale["id"],
        }

        # Aucune donnee personnelle nulle part dans le journal (immuable).
        everything = json.dumps(
            [e.payload for e in (await db.execute(select(JournalEvent))).scalars().all()],
            ensure_ascii=False,
        )
        for secret in ("jet@example.com", "0612345678", "Alice", "Martin"):
            assert secret not in everything


# ---------------------------------------------------------------------------
# DELETE /pos/transactions/{id}/client
# ---------------------------------------------------------------------------


async def test_detach_client_from_a_sale(client, auth_headers, open_drawer):
    fiche = await _create_client(client, auth_headers, email="a-detacher@example.com")
    sale = await _sell(client, auth_headers, client_id=fiche["client"]["id"])

    r = await client.delete(f"/api/pos/transactions/{sale['id']}/client", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["client"] is None
    assert r.json()["hash_chain"] == sale["hash_chain"]

    async with async_session() as db:
        tx = (
            await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))
        ).scalar_one()
        assert tx.client_id is None

        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "client.unlinked")
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].payload == {
            "client_id": fiche["client"]["id"],
            "transaction_id": sale["id"],
        }

    # La chaine fiscale reste valide : `client_id` est hors signature.
    async with async_session() as db:
        assert (await FiscalService(db).verify_chain_integrity())["valid"] is True


async def test_detach_client_is_idempotent(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    r = await client.delete(f"/api/pos/transactions/{sale['id']}/client", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["client"] is None

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "client.unlinked")
            )
        ).scalars().all()
        assert events == []


async def test_detach_client_on_unknown_sale_is_404(client, auth_headers):
    r = await client.delete(f"/api/pos/transactions/{uuid.uuid4()}/client", headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# Admin — le telephone est cherchable, visible, exportable
# ---------------------------------------------------------------------------


async def test_admin_clients_search_and_fiche_cover_the_phone(client, auth_headers):
    fiche = await _create_client(
        client, auth_headers, first_name="Alice", last_name="Martin", phone="06 12 34 56 78"
    )
    client_id = fiche["client"]["id"]

    listing = await client.get(
        "/api/admin/clients", params={"q": "34 56 78"}, headers=auth_headers
    )
    assert listing.status_code == 200, listing.text
    rows = listing.json()["clients"]
    assert len(rows) == 1
    assert rows[0]["id"] == client_id
    # Cote back-office, le numero est en clair (c'est la fiche, pas la caisse).
    assert rows[0]["phone"] == "0612345678"
    assert rows[0]["email"] is None

    detail = await client.get(f"/api/admin/clients/{client_id}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["client"]["phone"] == "0612345678"

    export = await client.get(f"/api/admin/clients/{client_id}/export", headers=auth_headers)
    assert export.status_code == 200
    assert export.json()["client"]["phone"] == "0612345678"


# ---------------------------------------------------------------------------
# Migration 0007 — les protections existantes sont intactes
# ---------------------------------------------------------------------------


async def test_existing_triggers_survive_migration_0007():
    """La migration 0007 ne touche a aucun trigger : le registre de
    consentement append-only (0003) et l'immuabilite des transactions
    signees (0002/0003) doivent etre toujours en place au head."""
    async with engine.begin() as conn:
        triggers = set(
            (
                await conn.execute(
                    text(
                        "SELECT tgname FROM pg_trigger "
                        "WHERE NOT tgisinternal AND tgrelid IN "
                        "('consents'::regclass, 'transactions'::regclass)"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {"trg_protect_consent", "trg_protect_signed_transaction"} <= triggers

        # Les index uniques PARTIELS de 0007 sont bien la, et la contrainte
        # CHECK aussi.
        indexes = set(
            (
                await conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'clients'")
                )
            )
            .scalars()
            .all()
        )
        assert {"uq_clients_email_present", "uq_clients_phone_present"} <= indexes

        checks = set(
            (
                await conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'clients'::regclass AND contype = 'c'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "ck_clients_contact_required" in checks


async def test_two_clients_without_email_can_coexist():
    """L'index unique PARTIEL remplace la contrainte UNIQUE : deux fiches
    « telephone seul » cohabitent (ce qu'un UNIQUE classique aurait
    interdit de facto, la colonne etant NOT NULL avant 0007)."""
    async with async_session() as db:
        service = ClientService(db)
        await service.create_or_get(phone="0611111111", user_id=None)
        await service.create_or_get(phone="0622222222", user_id=None)
        await db.commit()

    async with async_session() as db:
        rows = (
            await db.execute(select(Client).where(Client.email.is_(None)))
        ).scalars().all()
        assert len(rows) == 2
