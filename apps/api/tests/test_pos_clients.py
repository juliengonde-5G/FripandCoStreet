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

from app.core.config import settings
from app.core.database import async_session, engine
from app.models.client import Client
from app.models.jet import JournalEvent
from app.models.pos import Transaction
from app.models.receipt import Receipt
from app.services import brevo_contacts, email_gateway
from app.services.client_service import (
    ClientService,
    ContactRequired,
    InvalidPhone,
    mask_phone,
    normalize_phone,
    phone_digits,
    phone_search_digits,
)
from app.services.fiscal import FiscalService
from app.services.receipt import apply_client_line, format_client_label

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


def test_normalize_phone_canonicalises_the_three_french_forms():
    """La meme ligne dictee de trois facons donne UNE seule fiche."""
    # Espaces, points, tirets, parentheses, insecables : retires.
    for raw in (
        "06 12 34 56 78",
        "06.12.34.56.78",
        "06-12-34-56-78",
        " (06) 12 34 56 78 ",
        "+33 6 12 34 56 78",
        "0033 6 12 34 56 78",
    ):
        assert normalize_phone(raw) == "+33612345678", raw


def test_normalize_phone_keeps_foreign_numbers_and_raw_digits():
    # Deja international : conserve tel quel, quel que soit le pays.
    assert normalize_phone("+41 79 123 45 67") == "+41791234567"
    # Etranger compose sans indicatif : chiffres bruts, aucune invention
    # d'un `+33` qui serait faux.
    assert normalize_phone("1234567") == "1234567"
    assert normalize_phone("0033 6 12 34 56 78") == "+33612345678"
    # Un `0X…` qui n'a pas 10 chiffres n'est pas un numero francais.
    assert normalize_phone("012345678") == "012345678"
    assert normalize_phone("") is None
    assert normalize_phone(None) is None
    assert normalize_phone("   ") is None


def test_normalize_phone_rejects_garbage():
    for raw in (
        "pas-un-numero",
        "06 12 34 56 7A",
        "12345",  # moins de 6 chiffres
        "1234567890123456",  # plus de 15 chiffres (E.164)
        "++33612345678",
        "06+12",
    ):
        with pytest.raises(InvalidPhone) as exc:
            normalize_phone(raw)
        assert exc.value.code == "invalid_phone"
        assert exc.value.status_code == 422


def test_phone_search_digits_strips_what_does_not_identify_the_line():
    # Les quatre facons de taper la meme ligne francaise se rejoignent.
    assert (
        phone_search_digits("06 99 88")
        == phone_search_digits("+33 6 99 88")
        == phone_search_digits("0033699 88")
        == phone_search_digits("699 88")
        == "69988"
    )
    # Etranger : cherche par ses propres chiffres.
    assert phone_search_digits("41 79 12") == "417912"
    assert phone_search_digits("79 123 45") == "7912345"
    # Pas un numero -> la recherche reste sur nom/e-mail.
    assert phone_search_digits("martin") is None
    assert phone_search_digits("a@b.fr") is None
    assert phone_search_digits("33") is None


def test_phone_digits_drops_the_plus():
    assert phone_digits("+33699887766") == "33699887766"
    assert phone_digits("0612345678") == "0612345678"
    assert phone_digits(None) == ""


def test_mask_phone_keeps_only_the_last_two_digits():
    assert mask_phone("+33612345678") == "••••••••••78"
    assert mask_phone("0612345678") == "••••••••78"
    assert mask_phone(None) is None
    assert "12345" not in (mask_phone("+33612345678") or "")


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
        # Canonise a l'ecriture : une seule forme en base.
        assert client.phone == "+33612345678"


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
        assert again.phone == "+33612000000"
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
        # Telephone : les quatre facons de taper la meme ligne retrouvent la
        # fiche, quelle que soit la forme sous laquelle elle a ete saisie.
        for q in ("06 99 88 77 66", "+33 6 99 88 77 66", "0033 699 88 77 66", "699887766"):
            assert [c.first_name for c in await service.search(q)] == ["Zoe"], q
        assert [c.first_name for c in await service.search("06 99 88")] == ["Zoe"]
        assert [c.first_name for c in await service.search("88 77 66")] == ["Zoe"]
        assert await service.search("06 00 00 00 00") == []


async def test_search_finds_a_foreign_number_by_its_digits():
    async with async_session() as db:
        service = ClientService(db)
        await service.create_or_get(phone="+41 79 123 45 67", first_name="Heidi", user_id=None)
        await db.commit()

    async with async_session() as db:
        service = ClientService(db)
        assert [c.phone for c in await service.search("+41 79 123 45 67")] == ["+41791234567"]
        assert [c.first_name for c in await service.search("41 79 12")] == ["Heidi"]
        assert [c.first_name for c in await service.search("79 123 45")] == ["Heidi"]


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
    assert fiche["phone_masked"] == "••••••••••78"
    assert "+33612345678" not in json.dumps(body)
    assert "612345678" not in json.dumps(body)
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
    """La fiche est enregistree en `+33…` ; la vendeuse tape le numero a la
    francaise, ou n'importe laquelle des autres formes, et la retrouve."""
    await _create_client(
        client, auth_headers, first_name="Zoe", last_name="Blanc", phone="+33 6 99 88 77 66"
    )

    for q in ("06 99 88", "+33 6 99 88", "0033699 88", "699 88", "99 88 77"):
        r = await client.get("/api/pos/clients/search", params={"q": q}, headers=auth_headers)
        assert r.status_code == 200, r.text
        found = r.json()["clients"]
        assert len(found) == 1, q
        assert found[0]["last_name"] == "Blanc"
        assert found[0]["phone_masked"] == "••••••••••66"
        # Jamais le numero complet dans la reponse.
        assert "33699887766" not in r.text

    # Une autre ligne ne repond pas.
    other = await client.get(
        "/api/pos/clients/search", params={"q": "06 00 00 00 00"}, headers=auth_headers
    )
    assert other.json()["clients"] == []


async def test_search_pos_clients_finds_a_foreign_number(client, auth_headers):
    await _create_client(client, auth_headers, first_name="Heidi", phone="+41 79 123 45 67")
    r = await client.get("/api/pos/clients/search", params={"q": "79 123 45"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert [c["first_name"] for c in r.json()["clients"]] == ["Heidi"]


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
    assert "+33612345678" not in receipt
    assert "612345678" not in receipt
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
        for secret in ("jet@example.com", "+33612345678", "612345678", "Alice", "Martin"):
            assert secret not in everything


# ---------------------------------------------------------------------------
# Le nom imprime suit le rattachement — a la relecture, au renvoi par
# e-mail et a la reimpression (le contenu stocke, lui, est immuable)
# ---------------------------------------------------------------------------


def _fake_brevo(calls: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(201, json={"messageId": "fake-1"})

    return httpx.MockTransport(handler)


async def _send_receipt_by_email(client, auth_headers, sale_id, monkeypatch) -> dict:
    """Renvoie le ticket par e-mail vers un faux Brevo et rend le corps
    exact qui lui a ete transmis."""
    monkeypatch.setattr(settings, "BREVO_API_KEY", "ak-test")
    monkeypatch.setattr(settings, "BREVO_ANONYMOUS_TRACKING", True)
    calls: list[dict] = []
    monkeypatch.setattr(email_gateway, "_transport", _fake_brevo(calls))

    r = await client.post(
        f"/api/pos/transactions/{sale_id}/receipt/email",
        json={"email": "renvoi@example.com"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "brevo", r.json()
    assert len(calls) == 1
    return calls[0]


async def test_detaching_a_client_removes_the_name_everywhere_it_is_read(
    client, auth_headers, open_drawer, monkeypatch
):
    """Le bug : `receipts.content` est IMMUABLE (trigger
    `trg_protect_receipt`), le detachement ne pouvait donc pas le
    reecrire — relecture, renvoi par e-mail et reimpression continuaient
    de porter « Client : Alice M. ». C'est desormais le RENDU qui suit le
    rattachement courant."""
    fiche = await _create_client(
        client, auth_headers, first_name="Alice", last_name="Martin", email="detache@example.com"
    )
    sale = await _sell(client, auth_headers, client_id=fiche["client"]["id"])
    assert "Client : Alice M." in sale["receipt_text"]

    detach = await client.delete(
        f"/api/pos/transactions/{sale['id']}/client", headers=auth_headers
    )
    assert detach.status_code == 200, detach.text
    assert "Client :" not in detach.json()["receipt_text"]

    # 1. relecture du texte
    read = await client.get(f"/api/pos/transactions/{sale['id']}/receipt", headers=auth_headers)
    assert read.status_code == 200, read.text
    assert "Client :" not in read.json()["text"]
    assert "Alice" not in read.json()["text"]

    # 2. detail de la vente (panneau Tickets)
    detail = await client.get(f"/api/pos/transactions/{sale['id']}", headers=auth_headers)
    assert "Client :" not in detail.json()["receipt_text"]

    # 3. renvoi par e-mail (corps reellement transmis au fournisseur)
    payload = await _send_receipt_by_email(client, auth_headers, sale["id"], monkeypatch)
    assert "Client :" not in payload["textContent"]
    assert "Alice" not in payload["textContent"]
    assert "Alice" not in payload["htmlContent"]

    # 4. reimpression ESC/POS (tablette)
    escpos = await client.get(
        f"/api/pos/transactions/{sale['id']}/escpos", headers=auth_headers
    )
    assert escpos.status_code == 200, escpos.text
    assert b"Client :" not in escpos.content
    assert b"Alice" not in escpos.content


async def test_attaching_a_client_afterwards_adds_the_name_everywhere_it_is_read(
    client, auth_headers, open_drawer, monkeypatch
):
    """Symetrique : le rattachement a posteriori (PR3, apres le paiement)
    fait apparaitre « Client : … » sur le ticket relu, renvoye et
    reimprime."""
    sale = await _sell(client, auth_headers)
    assert "Client :" not in sale["receipt_text"]

    attach = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={
            "email": "apres-coup@example.com",
            "first_name": "Camille",
            "last_name": "Durand",
            "send_receipt": False,
        },
        headers=auth_headers,
    )
    assert attach.status_code == 200, attach.text

    read = await client.get(f"/api/pos/transactions/{sale['id']}/receipt", headers=auth_headers)
    assert "Client : Camille D." in read.json()["text"]
    # Toujours pas de coordonnees sur un ticket.
    assert "apres-coup@example.com" not in read.json()["text"]
    assert "Durand" not in read.json()["text"]

    detail = await client.get(f"/api/pos/transactions/{sale['id']}", headers=auth_headers)
    assert "Client : Camille D." in detail.json()["receipt_text"]

    payload = await _send_receipt_by_email(client, auth_headers, sale["id"], monkeypatch)
    assert "Client : Camille D." in payload["textContent"]

    escpos = await client.get(
        f"/api/pos/transactions/{sale['id']}/escpos", headers=auth_headers
    )
    assert "Client : Camille D.".encode("cp437", "replace") in escpos.content


async def test_stored_receipt_content_is_never_rewritten(client, auth_headers, open_drawer):
    """La trace reste intacte : c'est le rendu qui bouge, pas la ligne en
    base (elle est protegee par `trg_protect_receipt` et part telle quelle
    dans l'archive fiscale)."""
    fiche = await _create_client(
        client, auth_headers, first_name="Alice", last_name="Martin", email="trace@example.com"
    )
    sale = await _sell(client, auth_headers, client_id=fiche["client"]["id"])

    async with async_session() as db:
        stored = (
            await db.execute(
                select(Receipt).where(Receipt.transaction_id == uuid.UUID(sale["id"]))
            )
        ).scalar_one()
        frozen = stored.content
    assert "Client : Alice M." in frozen

    r = await client.delete(f"/api/pos/transactions/{sale['id']}/client", headers=auth_headers)
    assert r.status_code == 200, r.text

    async with async_session() as db:
        after = (
            await db.execute(
                select(Receipt).where(Receipt.transaction_id == uuid.UUID(sale["id"]))
            )
        ).scalar_one()
        assert after.content == frozen  # aucune reecriture en base


def test_apply_client_line_is_idempotent_and_surgical():
    ticket = "\n".join(
        ["FRIP & CO STREET", "Ticket #42", "Date: 15/09/2026 18:30", "-" * 42, "Total TTC"]
    )
    with_client = apply_client_line(ticket, "Alice M.")
    assert with_client.split("\n")[3] == "Client : Alice M."
    # Poser deux fois ne duplique pas la ligne.
    assert apply_client_line(with_client, "Alice M.") == with_client
    # Changer de cliente remplace la ligne, ne l'ajoute pas.
    assert apply_client_line(with_client, "Zoe B.").count("Client : ") == 1
    # Retirer la cliente rend le ticket d'origine, au caractere pres.
    assert apply_client_line(with_client, None) == ticket


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
    # Cote back-office, le numero est en clair (c'est la fiche, pas la
    # caisse) et sous sa forme canonique `+33…`.
    assert rows[0]["phone"] == "+33612345678"
    assert rows[0]["email"] is None

    detail = await client.get(f"/api/admin/clients/{client_id}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["client"]["phone"] == "+33612345678"

    export = await client.get(f"/api/admin/clients/{client_id}/export", headers=auth_headers)
    assert export.status_code == 200
    assert export.json()["client"]["phone"] == "+33612345678"


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
