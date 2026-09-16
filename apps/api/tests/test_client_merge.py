# Nouveau test (PR10, docs/ARCHITECTURE_PR10.md §3, contrats L2 et L3) —
# detection des doublons de fiches clientes et fusion.
#
# Le point le plus important du fichier tient en une assertion :
# `test_merge_leaves_sale_hashes_untouched`. La fusion repointe
# `transactions.client_id`, seule colonne mutable hors hash (E3/PR3) ; si
# la chaine fiscale bougeait, la fusion deviendrait une evolution fiscale et
# non une fonction de back-office.
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select, text

from app.core.config import settings
from app.core.database import async_session
from app.models.client import Client, Consent, ConsentPurpose, ConsentSource
from app.models.communication import (
    Communication,
    CommunicationChannel,
    CommunicationKind,
    CommunicationProvider,
    CommunicationStatus,
)
from app.models.jet import JournalEvent
from app.models.pos import Transaction
from app.services import brevo_contacts
from app.services.client_service import ClientService, name_key
from app.services.fiscal import FiscalService
from app.version import CONSENT_POLICY_VERSION

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


async def _new_client(**kwargs) -> uuid.UUID:
    async with async_session() as db:
        row = Client(**kwargs)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def _get(client_id: uuid.UUID) -> Client:
    async with async_session() as db:
        return (await db.execute(select(Client).where(Client.id == client_id))).scalar_one()


# ---------------------------------------------------------------------------
# L2 — `name_key`
# ---------------------------------------------------------------------------


def test_name_key_ignores_case_accents_and_hyphens():
    reference = name_key("Élodie", "Dupont-Martin")
    assert reference == "elodie dupont martin"
    # Meme personne, saisies differentes : accents, casse, tiret, espaces.
    assert name_key("elodie", "dupont martin") == reference
    assert name_key("ELODIE", "DUPONT-MARTIN") == reference
    assert name_key("  Elodie  ", " Dupont   Martin ") == reference


def test_name_key_is_none_without_last_name():
    """Un prenom seul ne suffit jamais a soupconner un doublon : deux
    « Sophie » differentes passent a la caisse dans la meme journee."""
    assert name_key("Sophie", None) is None
    assert name_key("Sophie", "   ") is None
    assert name_key(None, "Martin") == "martin"


# ---------------------------------------------------------------------------
# L2 — candidats
# ---------------------------------------------------------------------------


async def test_candidates_by_email_phone_and_name():
    email_id = await _new_client(email="doublon@example.com")
    phone_id = await _new_client(phone="+33699887766")
    name_id = await _new_client(email="elodie@example.com", first_name="Élodie", last_name="Dupont")

    async with async_session() as db:
        service = ClientService(db)

        by_email = await service.find_duplicate_candidates(email="  Doublon@Example.COM  ")
        assert [c["client"].id for c in by_email] == [email_id]
        assert by_email[0]["reason"] == "email"

        # Le numero est canonise a l'ecriture ET a la recherche : la fiche
        # enregistree en `+33…` se retrouve avec une saisie a la francaise.
        by_phone = await service.find_duplicate_candidates(phone="06 99 88 77 66")
        assert [c["client"].id for c in by_phone] == [phone_id]
        assert by_phone[0]["reason"] == "phone"
        by_phone_intl = await service.find_duplicate_candidates(phone="+33 6 99 88 77 66")
        assert [c["client"].id for c in by_phone_intl] == [phone_id]

        by_name = await service.find_duplicate_candidates(
            first_name="elodie", last_name="DUPONT"
        )
        assert [c["client"].id for c in by_name] == [name_id]
        assert by_name[0]["reason"] == "name"


async def test_candidates_prefer_the_surest_reason_and_exclude_self():
    """Une fiche qui matche a la fois par e-mail et par nom n'est proposee
    qu'une fois, sur le motif le plus sur."""
    target = await _new_client(
        email="double-motif@example.com", first_name="Anne", last_name="Leroy"
    )
    other = await _new_client(email="autre@example.com", first_name="Anne", last_name="Leroy")

    async with async_session() as db:
        found = await ClientService(db).find_duplicate_candidates(
            email="double-motif@example.com",
            first_name="Anne",
            last_name="Leroy",
            exclude_id=other,
        )
    assert [(c["client"].id, c["reason"]) for c in found] == [(target, "email")]


async def test_candidates_ignore_anonymized_and_merged_clients():
    anonymized = await _new_client(email="partie@example.com", last_name="Martin")
    absorbed = await _new_client(email="absorbee@example.com", last_name="Martin")
    kept = await _new_client(email="gardee@example.com", last_name="Martin")

    async with async_session() as db:
        row = (await db.execute(select(Client).where(Client.id == anonymized))).scalar_one()
        await ClientService(db).anonymize(client=row, user_id=None, reason="test")
        winner = (await db.execute(select(Client).where(Client.id == kept))).scalar_one()
        source = (await db.execute(select(Client).where(Client.id == absorbed))).scalar_one()
        await ClientService(db).merge(winner=winner, source=source, user_id=None)
        await db.commit()

    async with async_session() as db:
        found = await ClientService(db).find_duplicate_candidates(last_name="Martin")
    assert [c["client"].id for c in found] == [kept]


async def test_pos_duplicates_route_requires_at_least_one_criterion(client, auth_headers):
    r = await client.get("/api/pos/clients/duplicates", headers=auth_headers)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "criteria_required"

    r = await client.get(
        "/api/pos/clients/duplicates", params={"first_name": "   "}, headers=auth_headers
    )
    assert r.status_code == 422


async def test_pos_duplicates_route_masks_contacts(client, auth_headers, open_drawer):
    existing = await client.post(
        "/api/pos/clients",
        json={"email": "cliente@example.com", "phone": "0699887766", "last_name": "Dupont"},
        headers=auth_headers,
    )
    assert existing.status_code == 201, existing.text
    client_id = existing.json()["client"]["id"]
    await _sell(client, auth_headers, client_id=client_id)

    r = await client.get(
        "/api/pos/clients/duplicates",
        params={"email": "cliente@example.com"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    candidates = r.json()["candidates"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert set(candidate) == {
        "id",
        "first_name",
        "last_name",
        "email_masked",
        "phone_masked",
        "visits_count",
        "last_visit_at",
        "reason",
    }
    assert candidate["email_masked"] == "c***@example.com"
    assert candidate["phone_masked"].endswith("66")
    assert "cliente@example.com" not in json.dumps(candidate)
    assert candidate["visits_count"] == 1
    assert candidate["reason"] == "email"


async def test_pos_duplicates_route_tolerates_an_unfinished_entry(client, auth_headers):
    """La detection est appelee PENDANT la frappe : une adresse encore
    incomplete ne doit pas faire clignoter une erreur sous les doigts."""
    r = await client.get(
        "/api/pos/clients/duplicates", params={"email": "cli"}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["candidates"] == []


# ---------------------------------------------------------------------------
# L2 — groupes (back-office)
# ---------------------------------------------------------------------------


async def test_duplicate_groups_by_name_and_phone(client, auth_headers):
    await _new_client(email="anne1@example.com", first_name="Anne", last_name="Leroy")
    await _new_client(email="anne2@example.com", first_name="ANNE", last_name="LEROY")
    # Trois ecritures du MEME numero. Deux fiches ne peuvent pas porter la
    # meme chaine (index unique partiel `uq_clients_phone_present`, 0007) :
    # c'est precisement sous des ecritures differentes qu'un doublon par
    # telephone survit, et c'est ce que le regroupement doit rattraper.
    await _new_client(email="trio1@example.com", phone="+33611223344")
    await _new_client(email="trio2@example.com", phone="0611223344")
    await _new_client(email="trio3@example.com", phone="611223344")

    r = await client.get("/api/admin/clients/duplicates", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(body["groups"]) == 2

    by_reason = {g["reason"]: g for g in body["groups"]}
    assert len(by_reason["phone"]["clients"]) == 3
    assert len(by_reason["name"]["clients"]) == 2
    # Contacts masques a l'ecran (la carte peut etre ouverte devant du
    # public) + statistiques de visite pour choisir la fiche a conserver.
    sample = by_reason["name"]["clients"][0]
    assert sample["email_masked"].startswith("a***@")
    assert sample["visits_count"] == 0
    assert sample["last_visit_at"] is None
    assert "created_at" in sample


async def test_duplicate_groups_announce_a_pair_once(client, auth_headers):
    """Deux fiches qui partagent a la fois le telephone et le nom forment un
    seul doublon, pas deux."""
    await _new_client(phone="+33622334455", first_name="Marie", last_name="Petit")
    await _new_client(phone="0622334455", first_name="Marie", last_name="Petit")

    r = await client.get("/api/admin/clients/duplicates", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["groups"]) == 1
    assert r.json()["groups"][0]["reason"] == "phone"


# ---------------------------------------------------------------------------
# L3 — fusion
# ---------------------------------------------------------------------------


async def _merge(client, auth_headers, winner_id: str, source_id: str):
    return await client.post(
        f"/api/admin/clients/{winner_id}/merge",
        json={"source_id": source_id},
        headers=auth_headers,
    )


async def test_merge_moves_transactions_consents_and_communications(
    client, auth_headers, open_drawer
):
    winner_id = await _new_client(phone="+33655443322", first_name="Julie")
    source_id = await _new_client(email="julie@example.com", last_name="Bernard")

    # Deux ventes et une annulation sur la fiche a absorber.
    sale1 = await _sell(client, auth_headers, client_id=str(source_id))
    sale2 = await _sell(client, auth_headers, "20.00", client_id=str(source_id))
    refund = await client.post(
        f"/api/pos/transactions/{sale2['id']}/cancel",
        json={"reason": "Article rendu"},
        headers=auth_headers,
    )
    assert refund.status_code == 201, refund.text

    async with async_session() as db:
        # L'annulation n'herite pas de la cliente : on la rattache
        # explicitement pour verifier que la fusion deplace bien les
        # ANNULATIONS autant que les ventes.
        cancellation = (
            await db.execute(
                select(Transaction).where(Transaction.id == uuid.UUID(refund.json()["id"]))
            )
        ).scalar_one()
        await ClientService(db).link_transaction(
            transaction=cancellation,
            client=(
                await db.execute(select(Client).where(Client.id == source_id))
            ).scalar_one(),
            user_id=None,
        )
        db.add(
            Consent(
                client_id=source_id,
                purpose=ConsentPurpose.newsletter,
                granted=True,
                source=ConsentSource.pos,
                policy_version=CONSENT_POLICY_VERSION,
            )
        )
        db.add(
            Communication(
                client_id=source_id,
                kind=CommunicationKind.receipt,
                channel=CommunicationChannel.email,
                recipient="julie@example.com",
                subject="Votre ticket",
                provider=CommunicationProvider.simulated,
                status=CommunicationStatus.simulated,
            )
        )
        await db.commit()

    r = await _merge(client, auth_headers, str(winner_id), str(source_id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["moved"] == {"transactions": 3, "consents": 1, "communications": 1}

    # (3) champs vides completes, jamais d'ecrasement.
    assert body["client"]["id"] == str(winner_id)
    assert body["client"]["first_name"] == "Julie"  # valeur de la conservee
    assert body["client"]["last_name"] == "Bernard"  # reprise de la source
    assert body["client"]["email"] == "julie@example.com"
    assert body["client"]["phone"] == "+33655443322"

    async with async_session() as db:
        moved = (
            await db.execute(select(Transaction).where(Transaction.client_id == winner_id))
        ).scalars().all()
        assert {t["id"] for t in (sale1, sale2)} <= {str(t.id) for t in moved}
        assert (
            await db.execute(select(Transaction).where(Transaction.client_id == source_id))
        ).scalars().all() == []
        assert (
            await db.execute(select(Consent).where(Consent.client_id == winner_id))
        ).scalars().all() != []
        assert (
            await db.execute(select(Communication).where(Communication.client_id == source_id))
        ).scalars().all() == []


async def test_merge_leaves_sale_hashes_untouched(client, auth_headers, open_drawer):
    """Le coeur du contrat : la fusion n'est PAS une evolution fiscale.

    `client_id` est hors payload signe (E3/PR3) — les empreintes des ventes
    et la chaine complete doivent etre identiques avant et apres.
    """
    winner_id = await _new_client(email="gardee@example.com")
    source_id = await _new_client(email="absorbee@example.com")
    await _sell(client, auth_headers, client_id=str(source_id))
    await _sell(client, auth_headers, "33.00", client_id=str(source_id))

    async with async_session() as db:
        before = {
            str(t.id): (t.hash_chain, t.previous_hash, str(t.total_ttc))
            for t in (await db.execute(select(Transaction))).scalars().all()
        }

    r = await _merge(client, auth_headers, str(winner_id), str(source_id))
    assert r.status_code == 200, r.text

    async with async_session() as db:
        after = {
            str(t.id): (t.hash_chain, t.previous_hash, str(t.total_ttc))
            for t in (await db.execute(select(Transaction))).scalars().all()
        }
        assert after == before
        integrity = await FiscalService(db).verify_chain_integrity()
        assert integrity["valid"] is True, integrity


async def test_merge_empties_and_marks_the_source(client, auth_headers):
    winner_id = await _new_client(email="restante@example.com")
    source_id = await _new_client(
        email="videe@example.com",
        phone="+33644556677",
        first_name="Sonia",
        last_name="Girard",
        newsletter_optin=True,
    )

    r = await _merge(client, auth_headers, str(winner_id), str(source_id))
    assert r.status_code == 200, r.text
    # L'opt-in de la source profite a la fiche conservee (registre intact).
    assert r.json()["client"]["newsletter_optin"] is True

    source = await _get(source_id)
    assert source.first_name is None
    assert source.last_name is None
    assert source.phone is None
    assert source.newsletter_optin is False
    assert source.email.endswith("@anonyme.invalid")
    assert source.anonymized_at is not None
    assert source.merged_into_client_id == winner_id
    assert source.merged_at is not None


async def test_merge_does_not_overwrite_existing_values(client, auth_headers):
    winner_id = await _new_client(
        email="conservee@example.com", first_name="Claire", last_name="Noel"
    )
    source_id = await _new_client(
        email="doublon-ecrase@example.com", first_name="Claira", last_name="Noël"
    )

    r = await _merge(client, auth_headers, str(winner_id), str(source_id))
    assert r.status_code == 200, r.text
    assert r.json()["client"]["email"] == "conservee@example.com"
    assert r.json()["client"]["first_name"] == "Claire"
    assert r.json()["client"]["last_name"] == "Noel"


async def test_merge_refuses_same_client(client, auth_headers):
    client_id = await _new_client(email="seule@example.com")
    r = await _merge(client, auth_headers, str(client_id), str(client_id))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "same_client"


async def test_merge_refuses_inactive_clients(client, auth_headers):
    winner_id = await _new_client(email="active@example.com")
    gone_id = await _new_client(email="anonymisee@example.com")
    async with async_session() as db:
        row = (await db.execute(select(Client).where(Client.id == gone_id))).scalar_one()
        await ClientService(db).anonymize(client=row, user_id=None, reason="test")
        await db.commit()

    r = await _merge(client, auth_headers, str(winner_id), str(gone_id))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "client_inactive"

    # Et dans l'autre sens : on ne deverse rien sur une fiche anonymisee.
    r = await _merge(client, auth_headers, str(gone_id), str(winner_id))
    assert r.status_code == 409
    assert r.json()["code"] == "client_inactive"


async def test_merge_refuses_when_the_winner_is_scheduled_for_deletion(client, auth_headers):
    winner_id = await _new_client(email="a-supprimer@example.com")
    source_id = await _new_client(email="doublon@example.com")
    async with async_session() as db:
        row = (await db.execute(select(Client).where(Client.id == winner_id))).scalar_one()
        row.deletion_requested_at = row.created_at
        row.deletion_scheduled_for = row.created_at
        await db.commit()

    r = await _merge(client, auth_headers, str(winner_id), str(source_id))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "deletion_pending"


async def test_merge_cancels_a_deletion_scheduled_on_the_source(client, auth_headers):
    """La demande portait sur une fiche qui n'existe plus en tant que telle :
    ses donnees personnelles viennent d'etre effacees par la fusion."""
    winner_id = await _new_client(email="conservee2@example.com")
    source_id = await _new_client(email="doublon2@example.com")
    async with async_session() as db:
        row = (await db.execute(select(Client).where(Client.id == source_id))).scalar_one()
        row.deletion_requested_at = row.created_at
        row.deletion_scheduled_for = row.created_at
        await db.commit()

    r = await _merge(client, auth_headers, str(winner_id), str(source_id))
    assert r.status_code == 200, r.text

    source = await _get(source_id)
    assert source.deletion_requested_at is None
    assert source.deletion_scheduled_for is None
    assert source.deletion_requested_by_user_id is None


async def test_merge_journal_event_carries_no_personal_data(client, auth_headers):
    winner_id = await _new_client(email="jet-gardee@example.com")
    source_id = await _new_client(
        email="jet-absorbee@example.com", phone="+33677889900", last_name="Fontaine"
    )

    r = await _merge(client, auth_headers, str(winner_id), str(source_id))
    assert r.status_code == 200, r.text

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "client.merged")
            )
        ).scalars().all()
    assert len(events) == 1
    payload = json.dumps(events[0].payload)
    assert "jet-absorbee@example.com" not in payload
    assert "Fontaine" not in payload
    assert "+33677889900" not in payload
    assert events[0].payload["winner_id"] == str(winner_id)
    assert events[0].payload["source_id"] == str(source_id)


async def test_merged_client_disappears_from_search_but_keeps_its_url(client, auth_headers):
    winner_id = await _new_client(email="visible@example.com", last_name="Rousseau")
    source_id = await _new_client(email="invisible@example.com", last_name="Rousseau")

    r = await _merge(client, auth_headers, str(winner_id), str(source_id))
    assert r.status_code == 200, r.text

    listed = await client.get(
        "/api/admin/clients", params={"q": "Rousseau"}, headers=auth_headers
    )
    assert listed.status_code == 200, listed.text
    assert [c["id"] for c in listed.json()["clients"]] == [str(winner_id)]

    # La fiche absorbee repond encore : le front y lit vers quelle fiche
    # rediriger.
    absorbed = await client.get(f"/api/admin/clients/{source_id}", headers=auth_headers)
    assert absorbed.status_code == 200, absorbed.text
    assert absorbed.json()["client"]["merged_into_client_id"] == str(winner_id)
    assert absorbed.json()["client"]["merged_at"] is not None


async def test_merged_client_is_not_found_again_by_create_or_get(client, auth_headers):
    """Retrouver la fiche absorbee par son telephone ressusciterait le
    doublon qu'on vient de resorber."""
    winner_id = await _new_client(email="reprise@example.com")
    source_id = await _new_client(phone="+33688990011")

    r = await _merge(client, auth_headers, str(winner_id), str(source_id))
    assert r.status_code == 200, r.text
    # La conservee n'avait pas de telephone : elle a repris celui de la source.
    assert r.json()["client"]["phone"] == "+33688990011"

    again = await client.post(
        "/api/pos/clients", json={"phone": "06 88 99 00 11"}, headers=auth_headers
    )
    assert again.status_code == 201, again.text
    assert again.json()["client"]["id"] == str(winner_id)
    assert again.json()["created"] is False


async def test_merge_removes_the_source_from_brevo_and_syncs_the_winner(
    client, auth_headers, monkeypatch
):
    monkeypatch.setattr(settings, "BREVO_API_KEY", "ak-test")
    monkeypatch.setattr(settings, "BREVO_LIST_ID", "77")

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "url": str(request.url)})
        if request.url.path.endswith("/remove"):
            return httpx.Response(204)
        return httpx.Response(201, json={"id": 1})

    monkeypatch.setattr(brevo_contacts, "_transport", httpx.MockTransport(handler))

    winner_id = await _new_client(email="brevo-gardee@example.com", newsletter_optin=True)
    source_id = await _new_client(email="brevo-absorbee@example.com")

    r = await _merge(client, auth_headers, str(winner_id), str(source_id))
    assert r.status_code == 200, r.text

    # Retrait du contact absorbe de la liste dediee, puis synchro de la
    # fiche conservee (opt-in => push).
    assert calls[0]["url"].endswith("/contacts/lists/77/contacts/remove")
    assert calls[-1]["url"] == "https://api.brevo.com/v3/contacts"


async def test_merge_survives_a_brevo_outage(client, auth_headers, monkeypatch):
    """Une panne Brevo ne doit pas faire echouer une fusion : la base fait
    foi, le carnet d'adresses se rattrapera."""
    monkeypatch.setattr(settings, "BREVO_API_KEY", "ak-test")
    monkeypatch.setattr(settings, "BREVO_LIST_ID", "77")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("réseau indisponible")

    monkeypatch.setattr(brevo_contacts, "_transport", httpx.MockTransport(handler))

    winner_id = await _new_client(email="panne-gardee@example.com")
    source_id = await _new_client(email="panne-absorbee@example.com")

    r = await _merge(client, auth_headers, str(winner_id), str(source_id))
    assert r.status_code == 200, r.text
    assert (await _get(source_id)).merged_into_client_id == winner_id


async def test_merge_route_404_on_unknown_client(client, auth_headers):
    known = await _new_client(email="connue@example.com")
    r = await _merge(client, auth_headers, str(known), str(uuid.uuid4()))
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# L3 — le registre de consentement reste append-only
# ---------------------------------------------------------------------------


async def test_merge_keeps_every_consent_line_intact():
    """« L'historique des consentements est conserve integralement » : la
    fusion repointe les lignes, elle n'en reecrit et n'en supprime aucune."""
    winner_id = await _new_client(email="registre-gardee@example.com")
    source_id = await _new_client(email="registre-absorbee@example.com")
    async with async_session() as db:
        for granted in (True, False, True):
            db.add(
                Consent(
                    client_id=source_id,
                    purpose=ConsentPurpose.newsletter,
                    granted=granted,
                    source=ConsentSource.admin,
                    policy_version=CONSENT_POLICY_VERSION,
                    note="trace a conserver",
                )
            )
        await db.commit()

    async with async_session() as db:
        winner = (await db.execute(select(Client).where(Client.id == winner_id))).scalar_one()
        source = (await db.execute(select(Client).where(Client.id == source_id))).scalar_one()
        await ClientService(db).merge(winner=winner, source=source, user_id=None)
        await db.commit()

    async with async_session() as db:
        lines = (
            await db.execute(
                select(Consent)
                .where(Consent.client_id == winner_id)
                .order_by(Consent.created_at.asc())
            )
        ).scalars().all()
    assert [c.granted for c in lines] == [True, False, True]
    assert {c.note for c in lines} == {"trace a conserver"}
    assert {c.source for c in lines} == {ConsentSource.admin}


async def test_consent_content_remains_immutable_despite_the_exemption():
    """L'exemption de la migration 0010 porte sur le SEUL rattachement : un
    UPDATE qui toucherait au contenu reste refuse par le trigger, et la
    suppression l'est sans condition."""
    client_id = await _new_client(email="trigger@example.com")
    async with async_session() as db:
        entry = Consent(
            client_id=client_id,
            purpose=ConsentPurpose.newsletter,
            granted=True,
            source=ConsentSource.pos,
            policy_version=CONSENT_POLICY_VERSION,
        )
        db.add(entry)
        await db.commit()
        consent_id = entry.id

    other_id = await _new_client(email="trigger-autre@example.com")

    async with async_session() as db:
        # Le rattachement seul passe (c'est ce dont la fusion a besoin).
        await db.execute(
            text("UPDATE consents SET client_id = :other WHERE id = :id"),
            {"other": other_id, "id": consent_id},
        )
        await db.commit()

    async with async_session() as db:
        with pytest.raises(Exception, match="append-only"):
            await db.execute(
                text("UPDATE consents SET granted = false WHERE id = :id"),
                {"id": consent_id},
            )
    async with async_session() as db:
        with pytest.raises(Exception, match="append-only"):
            await db.execute(
                text("UPDATE consents SET note = 'retouche' WHERE id = :id"),
                {"id": consent_id},
            )
    async with async_session() as db:
        with pytest.raises(Exception, match="append-only"):
            await db.execute(
                text("DELETE FROM consents WHERE id = :id"), {"id": consent_id}
            )
