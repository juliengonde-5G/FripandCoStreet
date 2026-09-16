# Nouveau test (PR11, §3 de docs/ARCHITECTURE_PR11.md) — export CSV des
# abonnes a la newsletter et filtres de la liste des fiches (M4).
#
# Les fiches sont TOUJOURS creees par la vraie route de caisse (consentement
# enregistre au registre append-only, source `pos`) : jamais d'INSERT direct,
# qui produirait une fiche abonnee sans preuve de consentement — exactement ce
# que l'export est cense documenter.
import json

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.jet import JournalEvent

pytestmark = pytest.mark.anyio


async def _create_client(client, auth_headers, **body) -> dict:
    r = await client.post("/api/pos/clients", json=body, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()["client"]


async def _export(client, auth_headers):
    return await client.get(
        "/api/admin/clients/export?optin=newsletter&format=csv", headers=auth_headers
    )


def _rows(csv_text: str) -> list[list[str]]:
    assert csv_text.startswith("﻿")
    lines = [line for line in csv_text.lstrip("﻿").split("\r\n") if line]
    return [line.split(";") for line in lines]


async def _list(client, auth_headers, **params):
    r = await client.get("/api/admin/clients", params=params, headers=auth_headers)
    assert r.status_code == 200, r.text
    return r.json()["clients"]


# ---------------------------------------------------------------------------
# Contenu du fichier
# ---------------------------------------------------------------------------


async def test_export_contains_only_active_subscribers(client, auth_headers):
    abonnee = await _create_client(
        client,
        auth_headers,
        email="alice@example.com",
        phone="0699887766",
        first_name="Alice",
        last_name="Martin",
        newsletter_optin=True,
    )
    # Non abonnee : jamais dans le fichier.
    await _create_client(
        client, auth_headers, email="bob@example.com", last_name="Durand"
    )
    # Anonymisee (RGPD deja execute).
    anonymisee = await _create_client(
        client, auth_headers, email="carole@example.com", newsletter_optin=True
    )
    r = await client.post(
        f"/api/admin/clients/{anonymisee['id']}/anonymize",
        json={"reason": "demande de la cliente"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    # Suppression programmee : elle a demande a partir, on ne l'ajoute pas a
    # une campagne pendant son delai de reflexion.
    partante = await _create_client(
        client, auth_headers, email="dora@example.com", newsletter_optin=True
    )
    r = await client.post(
        f"/api/admin/clients/{partante['id']}/deletion-request", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    # Absorbee par une fusion : son contenu vit sur la fiche conservee.
    absorbee = await _create_client(
        client, auth_headers, email="eve.doublon@example.com", newsletter_optin=True
    )
    r = await client.post(
        f"/api/admin/clients/{abonnee['id']}/merge",
        json={"source_id": absorbee["id"]},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    response = await _export(client, auth_headers)
    assert response.status_code == 200, response.text
    rows = _rows(response.text)

    assert rows[0] == ["email", "prenom", "nom", "telephone", "consentement_le", "source"]
    assert len(rows) == 2
    assert rows[1][0] == "alice@example.com"
    for absente in (
        "bob@example.com",
        "carole@example.com",
        "dora@example.com",
        "eve.doublon@example.com",
    ):
        assert absente not in response.text


async def test_export_columns_and_encoding(client, auth_headers):
    await _create_client(
        client,
        auth_headers,
        email="lea@example.com",
        phone="06 99 88 77 66",
        first_name="Léa",
        last_name="Noël",
        newsletter_optin=True,
    )

    response = await _export(client, auth_headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    # BOM : sans lui, Excel lit « Léa » en « LÃ©a ».
    assert response.content.startswith(b"\xef\xbb\xbf")

    rows = _rows(response.text)
    assert len(rows) == 2
    email, prenom, nom, telephone, consentement_le, source = rows[1]
    assert email == "lea@example.com"
    assert prenom == "Léa"
    assert nom == "Noël"
    assert telephone == "+33699887766" or telephone.endswith("699887766")
    # Date et source de la derniere ligne `newsletter` ACCORDEE du registre.
    assert consentement_le and "/" in consentement_le
    assert source == "pos"


async def test_export_filename_carries_the_day(client, auth_headers):
    response = await _export(client, auth_headers)
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "abonnes_newsletter_" in disposition
    assert disposition.endswith('.csv"')


async def test_empty_export_has_only_its_header(client, auth_headers):
    response = await _export(client, auth_headers)
    assert response.status_code == 200
    assert len(_rows(response.text)) == 1


async def test_export_is_journaled_without_any_address(client, auth_headers):
    await _create_client(
        client, auth_headers, email="alice@example.com", newsletter_optin=True
    )
    assert (await _export(client, auth_headers)).status_code == 200

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "export.downloaded")
            )
        ).scalars().all()
    assert len(events) == 1
    payload = events[0].payload
    assert payload["kind"] == "clients_newsletter_csv"
    assert payload["count"] == 1
    # Le journal est immuable : une adresse qui y tomberait ne pourrait plus
    # jamais en sortir, alors qu'une desinscription doit pouvoir effacer la
    # fiche (art. 17 RGPD).
    assert "alice@example.com" not in json.dumps(payload, ensure_ascii=False)


async def test_export_requires_authentication(client):
    assert (await client.get("/api/admin/clients/export")).status_code == 401


# ---------------------------------------------------------------------------
# Filtres de la liste
# ---------------------------------------------------------------------------


async def test_list_filters_are_additive(client, auth_headers):
    abonnee = await _create_client(
        client,
        auth_headers,
        email="alice@example.com",
        last_name="Martin",
        newsletter_optin=True,
    )
    await _create_client(
        client, auth_headers, email="bob@example.com", last_name="Durand"
    )
    partante = await _create_client(
        client,
        auth_headers,
        email="dora@example.com",
        last_name="Martin",
        newsletter_optin=True,
    )
    r = await client.post(
        f"/api/admin/clients/{partante['id']}/deletion-request", headers=auth_headers
    )
    assert r.status_code == 200, r.text

    assert len(await _list(client, auth_headers)) == 3

    abonnees = await _list(client, auth_headers, optin="newsletter")
    assert {c["id"] for c in abonnees} == {abonnee["id"], partante["id"]}

    suppressions = await _list(client, auth_headers, deletion="pending")
    assert [c["id"] for c in suppressions] == [partante["id"]]

    # Les deux filtres se cumulent, et se cumulent avec la recherche.
    croise = await _list(
        client, auth_headers, optin="newsletter", deletion="pending", q="martin"
    )
    assert [c["id"] for c in croise] == [partante["id"]]


async def test_unknown_filter_value_is_refused(client, auth_headers):
    r = await client.get(
        "/api/admin/clients", params={"optin": "sms"}, headers=auth_headers
    )
    assert r.status_code == 422
