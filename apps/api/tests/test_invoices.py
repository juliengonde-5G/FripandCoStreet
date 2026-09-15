# Nouveau test (PR8, docs/ARCHITECTURE_PR8.md §3 « Factures », contrat J5) —
# facture B2B et avoir : numerotation sequentielle par annee sous le verrou
# fiscal, validation du SIRET, unicite, avoir a l'annulation, PDF
# deterministe, presence dans l'archive de cloture et dans l'export fiscal,
# et intangibilite face a l'anonymisation RGPD d'une cliente.
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.invoice import Invoice, InvoiceKind
from app.models.jet import JournalEvent

pytestmark = pytest.mark.anyio

# SIRET valides (clef de Luhn correcte) — numeros de test, jamais ceux d'une
# entreprise reelle.
SIRET_OK = "73282932000074"
SIRET_OK_2 = "55208131766522"
# Meme longueur, clef de Luhn FAUSSE (dernier chiffre modifie).
SIRET_KO = "73282932000075"

COMPANY = {
    "company_name": "Atelier Dupont SARL",
    "siret": SIRET_OK,
    "vat_number": "FR40303265045",
    "address_line1": "12 rue des Carmes",
    "postal_code": "76000",
    "city": "Rouen",
}


def _uid() -> str:
    return str(uuid.uuid4())


async def _sell(client, auth_headers, amount: str = "30.00") -> dict:
    response = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _uid(),
            "items": [{"label": "Veste", "unit_price": amount, "quantity": 1}],
            "payments": [{"method": "cash", "amount": amount, "tendered_amount": amount}],
        },
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _issue(client, auth_headers, transaction_id: str, **overrides):
    payload = {**COMPANY, **overrides}
    return await client.post(
        f"/api/pos/transactions/{transaction_id}/invoice", json=payload, headers=auth_headers
    )


# ---------------------------------------------------------------------------
# Numerotation
# ---------------------------------------------------------------------------


async def test_invoice_number_is_sequential_per_year(client, auth_headers, open_drawer):
    year = datetime.now(timezone.utc).year
    first = await _issue(client, auth_headers, (await _sell(client, auth_headers))["id"])
    second = await _issue(client, auth_headers, (await _sell(client, auth_headers))["id"])
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["invoice"]["invoice_number"] == f"F-{year}-0001"
    assert second.json()["invoice"]["invoice_number"] == f"F-{year}-0002"


async def test_two_concurrent_invoices_get_distinct_consecutive_numbers(
    client, auth_headers, open_drawer
):
    """Deux emissions CONCURRENTES (deux requetes HTTP, donc deux sessions
    DB distinctes via `asyncio.gather`) sur deux ventes differentes : le
    verrou fiscal `pg_advisory_xact_lock(5252026)` serialise l'attribution,
    les deux numeros sont donc distincts ET consecutifs — jamais deux fois
    le meme, jamais de trou."""
    sale_a = await _sell(client, auth_headers)
    sale_b = await _sell(client, auth_headers)

    r1, r2 = await asyncio.gather(
        _issue(client, auth_headers, sale_a["id"]),
        _issue(client, auth_headers, sale_b["id"], company_name="Brasserie du Port"),
    )
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text

    year = datetime.now(timezone.utc).year
    numbers = sorted([r1.json()["invoice"]["invoice_number"], r2.json()["invoice"]["invoice_number"]])
    assert numbers == [f"F-{year}-0001", f"F-{year}-0002"]


# ---------------------------------------------------------------------------
# Validation et unicite
# ---------------------------------------------------------------------------


async def test_invalid_siret_is_refused_422(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    response = await _issue(client, auth_headers, sale["id"], siret=SIRET_KO)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_siret"

    too_short = await _issue(client, auth_headers, sale["id"], siret="1234")
    assert too_short.status_code == 422
    assert too_short.json()["code"] == "invalid_siret"

    async with async_session() as db:
        assert (await db.execute(select(Invoice))).scalars().all() == []


async def test_invalid_vat_number_is_refused_422(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    response = await _issue(client, auth_headers, sale["id"], vat_number="FR123")
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_vat_number"


async def test_vat_number_is_optional(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    response = await _issue(client, auth_headers, sale["id"], vat_number=None)
    assert response.status_code == 201, response.text
    assert response.json()["invoice"]["vat_number"] is None


async def test_siret_is_normalized_from_spaced_input(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    response = await _issue(client, auth_headers, sale["id"], siret="732 829 320 00074")
    assert response.status_code == 201, response.text
    assert response.json()["invoice"]["siret"] == SIRET_OK


async def test_second_invoice_on_same_sale_is_409(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    assert (await _issue(client, auth_headers, sale["id"])).status_code == 201
    second = await _issue(client, auth_headers, sale["id"], siret=SIRET_OK_2)
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "invoice_exists"


async def test_invoice_on_a_refund_is_409_not_a_sale(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    cancel = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel",
        json={"reason": "Erreur de saisie"},
        headers=auth_headers,
    )
    assert cancel.status_code == 201, cancel.text
    response = await _issue(client, auth_headers, cancel.json()["id"])
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "not_a_sale"


async def test_invoice_on_cancelled_sale_is_409(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    cancel = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel",
        json={"reason": "Cliente a change d'avis"},
        headers=auth_headers,
    )
    assert cancel.status_code == 201, cancel.text
    response = await _issue(client, auth_headers, sale["id"])
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "transaction_cancelled"


# ---------------------------------------------------------------------------
# Lecture + ticket
# ---------------------------------------------------------------------------


async def test_get_invoice_for_transaction_and_404_without(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    missing = await client.get(
        f"/api/pos/transactions/{sale['id']}/invoice", headers=auth_headers
    )
    assert missing.status_code == 404, missing.text

    issued = await _issue(client, auth_headers, sale["id"])
    assert issued.status_code == 201
    found = await client.get(f"/api/pos/transactions/{sale['id']}/invoice", headers=auth_headers)
    assert found.status_code == 200, found.text
    invoice = found.json()["invoice"]
    assert invoice["kind"] == "invoice"
    assert invoice["transaction_number"] == sale["transaction_number"]
    assert invoice["company_name"] == COMPANY["company_name"]
    # Totaux recopies de la vente, en chaines a deux decimales.
    assert invoice["total_ttc"] == "30.00"
    assert invoice["total_ht"] != invoice["total_ttc"]
    assert invoice["original_invoice_id"] is None


async def test_transaction_carries_invoice_number_and_receipt_shows_it(
    client, auth_headers, open_drawer
):
    sale = await _sell(client, auth_headers)
    before = await client.get(f"/api/pos/transactions/{sale['id']}", headers=auth_headers)
    assert before.json()["invoice_number"] is None
    assert "Facture :" not in before.json()["receipt_text"]

    issued = await _issue(client, auth_headers, sale["id"])
    number = issued.json()["invoice"]["invoice_number"]

    after = await client.get(f"/api/pos/transactions/{sale['id']}", headers=auth_headers)
    assert after.status_code == 200, after.text
    assert after.json()["invoice_number"] == number
    # La ligne est posee AU RENDU : le ticket stocke, lui, est immuable.
    assert f"Facture : {number}" in after.json()["receipt_text"]

    receipt = await client.get(
        f"/api/pos/transactions/{sale['id']}/receipt", headers=auth_headers
    )
    assert f"Facture : {number}" in receipt.json()["text"]


# ---------------------------------------------------------------------------
# Avoir
# ---------------------------------------------------------------------------


async def test_cancelling_an_invoiced_sale_issues_a_credit_note(
    client, auth_headers, open_drawer
):
    sale = await _sell(client, auth_headers)
    issued = await _issue(client, auth_headers, sale["id"])
    invoice = issued.json()["invoice"]

    cancel = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel",
        json={"reason": "Article defectueux"},
        headers=auth_headers,
    )
    assert cancel.status_code == 201, cancel.text
    refund = cancel.json()

    year = datetime.now(timezone.utc).year
    credit = await client.get(
        f"/api/pos/transactions/{refund['id']}/invoice", headers=auth_headers
    )
    assert credit.status_code == 200, credit.text
    credit_note = credit.json()["invoice"]
    assert credit_note["kind"] == "credit_note"
    assert credit_note["invoice_number"] == f"A-{year}-0001"
    assert credit_note["original_invoice_id"] == invoice["id"]
    # Coordonnees recopiees de la facture d'origine.
    assert credit_note["company_name"] == invoice["company_name"]
    assert credit_note["siret"] == invoice["siret"]
    assert credit_note["total_ttc"] == invoice["total_ttc"]
    # La facture d'origine reste intacte et lisible.
    original = await client.get(
        f"/api/pos/transactions/{sale['id']}/invoice", headers=auth_headers
    )
    assert original.json()["invoice"]["invoice_number"] == invoice["invoice_number"]


async def test_cancelling_a_non_invoiced_sale_creates_no_credit_note(
    client, auth_headers, open_drawer
):
    sale = await _sell(client, auth_headers)
    cancel = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel",
        json={"reason": "Erreur de caisse"},
        headers=auth_headers,
    )
    assert cancel.status_code == 201, cancel.text
    async with async_session() as db:
        assert (await db.execute(select(Invoice))).scalars().all() == []


async def test_partial_refund_of_an_invoiced_sale_is_refused(client, auth_headers, open_drawer):
    """L'avoir couvre l'annulation TOTALE (§2 du contrat). Le service
    d'annulation ne sait aujourd'hui produire qu'une annulation totale : on
    exerce donc le garde-fou au niveau du service facture, en lui presentant
    une annulation qui ne solde pas la vente."""
    from app.models.pos import Transaction
    from app.services.invoice_service import InvoiceService, InvoicedPartialRefund

    sale = await _sell(client, auth_headers, "40.00")
    assert (await _issue(client, auth_headers, sale["id"])).status_code == 201

    async with async_session() as db:
        original = (
            await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))
        ).scalar_one()
        partial = Transaction(
            transaction_number=9999,
            transaction_type=original.transaction_type,
            user_id=original.user_id,
            total_ht=10.0,
            total_tva=2.0,
            total_ttc=12.0,
            tva_rate=original.tva_rate,
            discount_amount=0,
            hash_chain="",
            previous_hash="",
            receipt_number=9999,
        )
        with pytest.raises(InvoicedPartialRefund) as excinfo:
            await InvoiceService(db).credit_note_for_cancellation(original, partial)
        assert excinfo.value.code == "invoiced_partial_refund"
        assert excinfo.value.status_code == 409


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


async def test_invoice_pdf_is_deterministic_and_seals_its_sha256(
    client, auth_headers, open_drawer
):
    sale = await _sell(client, auth_headers)
    invoice = (await _issue(client, auth_headers, sale["id"])).json()["invoice"]

    r1 = await client.get(f"/api/pos/invoices/{invoice['id']}/pdf", headers=auth_headers)
    r2 = await client.get(f"/api/pos/invoices/{invoice['id']}/pdf", headers=auth_headers)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.headers["content-type"] == "application/pdf"
    assert r1.content == r2.content
    sha = hashlib.sha256(r1.content).hexdigest()
    assert r1.headers["x-pdf-sha256"] == sha
    assert r2.headers["x-pdf-sha256"] == sha

    async with async_session() as db:
        row = (
            await db.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice["id"])))
        ).scalar_one()
        # Empreinte posee UNE fois, au premier telechargement.
        assert row.pdf_sha256 == sha


async def test_invoice_pdf_carries_legal_mentions_never_conforme_nf525(
    client, auth_headers, open_drawer
):
    sale = await _sell(client, auth_headers)
    invoice = (await _issue(client, auth_headers, sale["id"])).json()["invoice"]
    response = await client.get(
        f"/api/pos/invoices/{invoice['id']}/pdf", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    # Flux de contenu non compresse (`pageCompression=0`) : le texte est
    # lisible dans les octets, a l'echappement PDF pres.
    raw = response.content.decode("latin-1")
    assert "indemnit" in raw  # indemnité forfaitaire de recouvrement
    assert "40" in raw
    assert "livraison" in raw  # TVA exigible à la livraison
    assert "L.441-10" in raw
    assert "conforme NF525" not in raw
    assert invoice["invoice_number"] in raw


async def test_invoice_pdf_logs_export_downloaded(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    invoice = (await _issue(client, auth_headers, sale["id"])).json()["invoice"]
    await client.get(f"/api/pos/invoices/{invoice['id']}/pdf", headers=auth_headers)

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "export.downloaded")
            )
        ).scalars().all()
        assert [e.payload["kind"] for e in events] == ["invoice_pdf"]
        assert events[0].payload["invoice_number"] == invoice["invoice_number"]


async def test_invoice_pdf_mismatch_is_refused_and_alerts(client, auth_headers, open_drawer):
    """Empreinte scellee != empreinte du document servi : on refuse de
    servir la facture (500 `pdf_mismatch`) et l'incident est journalise —
    ET COMMITE, malgre l'echec de la requete."""
    sale = await _sell(client, auth_headers)
    invoice = (await _issue(client, auth_headers, sale["id"])).json()["invoice"]

    async with async_session() as db:
        # On pose une empreinte qui ne correspond a aucun rendu possible :
        # c'est l'unique UPDATE que le trigger d'immuabilite tolere.
        await db.execute(
            Invoice.__table__.update()
            .where(Invoice.__table__.c.id == uuid.UUID(invoice["id"]))
            .values(pdf_sha256="0" * 64)
        )
        await db.commit()

    response = await client.get(
        f"/api/pos/invoices/{invoice['id']}/pdf", headers=auth_headers
    )
    assert response.status_code == 500, response.text
    assert response.json()["code"] == "pdf_mismatch"

    async with async_session() as db:
        alerts = (
            await db.execute(
                select(JournalEvent).where(
                    JournalEvent.event_type == "system.integrity_alert"
                )
            )
        ).scalars().all()
        assert len(alerts) == 1
        assert alerts[0].payload["invoice_number"] == invoice["invoice_number"]


async def test_pdf_survives_a_change_of_shop_settings(client, auth_headers, open_drawer):
    """Une facture doit rester reproductible A VIE : le PDF se rend depuis
    le bloc vendeur FIGÉ à l'émission (`seller_snapshot`), jamais depuis les
    réglages courants. Sans cela, un simple changement de nom commercial
    après le premier téléchargement rendrait un document différent — et
    l'empreinte scellée ne correspondrait plus (500 `pdf_mismatch`) sur une
    facture pourtant intacte."""
    sale = await _sell(client, auth_headers)
    invoice = (await _issue(client, auth_headers, sale["id"])).json()["invoice"]
    assert invoice["seller"]["name"]

    first = await client.get(f"/api/pos/invoices/{invoice['id']}/pdf", headers=auth_headers)
    assert first.status_code == 200, first.text

    settings = await client.put(
        "/api/admin/settings/shop",
        json={
            "name": "Frip & Co Street — enseigne renommée",
            "address_line1": "99 avenue Nouvelle",
            "postal_code": "76100",
            "city": "Rouen",
            "phone": "0200000000",
            "siret": SIRET_OK_2,
            "vat_number": "FR40303265045",
        },
        headers=auth_headers,
    )
    assert settings.status_code == 200, settings.text

    again = await client.get(f"/api/pos/invoices/{invoice['id']}/pdf", headers=auth_headers)
    assert again.status_code == 200, again.text
    assert again.content == first.content
    assert again.headers["x-pdf-sha256"] == first.headers["x-pdf-sha256"]
    # Et le nouveau nom n'apparaît pas sur la facture déjà émise.
    assert "enseigne renomm" not in again.content.decode("latin-1")


async def test_credit_note_copies_the_seller_snapshot_of_the_invoice(
    client, auth_headers, open_drawer
):
    """L'avoir est émis par le même vendeur, tel qu'il était identifié au
    moment de la vente — pas tel que les réglages le décrivent le jour de
    l'annulation."""
    sale = await _sell(client, auth_headers)
    invoice = (await _issue(client, auth_headers, sale["id"])).json()["invoice"]

    await client.put(
        "/api/admin/settings/shop",
        json={"name": "Autre enseigne", "postal_code": "76000", "city": "Rouen"},
        headers=auth_headers,
    )
    cancel = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel",
        json={"reason": "Retour marchandise"},
        headers=auth_headers,
    )
    assert cancel.status_code == 201, cancel.text
    credit_note = (
        await client.get(
            f"/api/pos/transactions/{cancel.json()['id']}/invoice", headers=auth_headers
        )
    ).json()["invoice"]
    assert credit_note["seller"] == invoice["seller"]
    assert credit_note["seller"]["name"] != "Autre enseigne"


async def test_trigger_refuses_updating_the_seller_snapshot(client, auth_headers, open_drawer):
    """Le bloc vendeur fait partie des colonnes gelées par
    `fripco_protect_invoice` : une fois la facture émise, il ne bouge plus,
    même par une écriture SQL directe."""
    import sqlalchemy.exc

    sale = await _sell(client, auth_headers)
    invoice = (await _issue(client, auth_headers, sale["id"])).json()["invoice"]

    async with async_session() as db:
        with pytest.raises(sqlalchemy.exc.DBAPIError) as excinfo:
            await db.execute(
                Invoice.__table__.update()
                .where(Invoice.__table__.c.id == uuid.UUID(invoice["id"]))
                .values(seller_snapshot={"name": "Enseigne falsifiée"})
            )
            await db.commit()
        assert "facture immuable" in str(excinfo.value)


async def test_credit_note_pdf_mentions_the_original_invoice(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    invoice = (await _issue(client, auth_headers, sale["id"])).json()["invoice"]
    cancel = await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel",
        json={"reason": "Retour marchandise"},
        headers=auth_headers,
    )
    credit_note = (
        await client.get(
            f"/api/pos/transactions/{cancel.json()['id']}/invoice", headers=auth_headers
        )
    ).json()["invoice"]

    response = await client.get(
        f"/api/pos/invoices/{credit_note['id']}/pdf", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    raw = response.content.decode("latin-1")
    assert "AVOIR" in raw
    assert invoice["invoice_number"] in raw


# ---------------------------------------------------------------------------
# JET
# ---------------------------------------------------------------------------


async def test_jet_invoice_events_carry_identifiers_only(client, auth_headers, open_drawer):
    """Le JET est immuable : il porte des IDENTIFIANTS, jamais du contenu.
    La raison sociale et le SIRET ne sont pas des donnees personnelles, mais
    ils n'ont pas a s'y retrouver figes pour autant."""
    sale = await _sell(client, auth_headers)
    invoice = (await _issue(client, auth_headers, sale["id"])).json()["invoice"]
    await client.post(
        f"/api/pos/transactions/{sale['id']}/cancel",
        json={"reason": "Retour marchandise"},
        headers=auth_headers,
    )

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent)
                .where(JournalEvent.event_type.in_(["invoice.issued", "invoice.credit_note_issued"]))
                .order_by(JournalEvent.seq.asc())
            )
        ).scalars().all()
    assert [e.event_type for e in events] == ["invoice.issued", "invoice.credit_note_issued"]

    issued = events[0]
    assert set(issued.payload) == {"invoice_id", "transaction_id", "invoice_number"}
    assert issued.payload["invoice_number"] == invoice["invoice_number"]

    blob = json.dumps([e.payload for e in events], ensure_ascii=False)
    assert COMPANY["company_name"] not in blob
    assert COMPANY["siret"] not in blob
    assert COMPANY["address_line1"] not in blob
    assert COMPANY["vat_number"] not in blob


# ---------------------------------------------------------------------------
# Liste admin
# ---------------------------------------------------------------------------


async def test_admin_lists_invoices_of_the_year(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    invoice = (await _issue(client, auth_headers, sale["id"])).json()["invoice"]
    year = datetime.now(timezone.utc).year

    response = await client.get(f"/api/admin/invoices?year={year}", headers=auth_headers)
    assert response.status_code == 200, response.text
    numbers = [i["invoice_number"] for i in response.json()["invoices"]]
    assert numbers == [invoice["invoice_number"]]

    other_year = await client.get(f"/api/admin/invoices?year={year - 1}", headers=auth_headers)
    assert other_year.json()["invoices"] == []


async def test_invoice_routes_require_authentication(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    assert (await client.post(f"/api/pos/transactions/{sale['id']}/invoice", json=COMPANY)).status_code == 401
    assert (await client.get(f"/api/pos/transactions/{sale['id']}/invoice")).status_code == 401
    assert (await client.get("/api/admin/invoices?year=2026")).status_code == 401


# ---------------------------------------------------------------------------
# Archive de cloture, export fiscal, RGPD
# ---------------------------------------------------------------------------


async def test_closure_archive_and_fiscal_export_contain_the_invoice(
    client, auth_headers, open_drawer
):
    sale = await _sell(client, auth_headers)
    invoice = (await _issue(client, auth_headers, sale["id"])).json()["invoice"]

    close = await client.post(
        "/api/pos/drawer/close", json={"closing_amount": "130.00"}, headers=auth_headers
    )
    assert close.status_code == 200, close.text

    # `period_end` ne doit jamais etre dans le futur (cloture future
    # interdite) : `now` suffit, l'appel HTTP qui suit avance l'horloge.
    now = datetime.now(timezone.utc)
    period_start = (now - timedelta(days=1)).isoformat()
    period_end = now.isoformat()

    closure = await client.post(
        "/api/admin/fiscal-closures",
        json={"closure_type": "manual", "period_start": period_start, "period_end": period_end},
        headers=auth_headers,
    )
    assert closure.status_code == 201, closure.text
    archive = await client.get(
        f"/api/admin/fiscal-closures/{closure.json()['id']}/archive", headers=auth_headers
    )
    assert archive.status_code == 200, archive.text
    snapshot = json.loads(gzip.decompress(archive.content))
    assert [i["invoice_number"] for i in snapshot["invoices"]] == [invoice["invoice_number"]]
    assert snapshot["invoices"][0]["siret"] == SIRET_OK
    assert snapshot["invoices"][0]["total_ttc"] == "30.00"

    export = await client.get(
        "/api/admin/fiscal-export",
        # `params=` et pas une URL formatee : le « + » du decalage horaire
        # ISO serait sinon decode cote serveur comme une espace.
        params={"from": period_start, "to": period_end, "format": "json"},
        headers=auth_headers,
    )
    assert export.status_code == 200, export.text
    body = json.loads(export.content)
    assert [i["invoice_number"] for i in body["invoices"]] == [invoice["invoice_number"]]
    assert body["totals"]["invoices_count"] == 1
    assert body["totals"]["credit_notes_count"] == 0


async def test_anonymizing_a_client_leaves_invoices_untouched(
    client, auth_headers, open_drawer
):
    """Le client professionnel n'est PAS une fiche `clients` : anonymiser
    une cliente (RGPD art. 17) ne touche a aucune facture — pas meme celle
    d'une vente a laquelle elle serait rattachee."""
    sale = await _sell(client, auth_headers)
    attach = await client.post(
        f"/api/pos/transactions/{sale['id']}/client",
        json={
            "email": "cliente@example.com",
            "first_name": "Marie",
            "send_receipt": False,
        },
        headers=auth_headers,
    )
    assert attach.status_code == 200, attach.text
    client_id = attach.json()["client"]["id"]
    invoice = (await _issue(client, auth_headers, sale["id"])).json()["invoice"]

    anonymized = await client.post(
        f"/api/admin/clients/{client_id}/anonymize",
        json={"reason": "Demande de suppression"},
        headers=auth_headers,
    )
    assert anonymized.status_code == 200, anonymized.text

    after = await client.get(f"/api/pos/transactions/{sale['id']}/invoice", headers=auth_headers)
    assert after.status_code == 200, after.text
    assert after.json()["invoice"] == invoice

    async with async_session() as db:
        row = (
            await db.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice["id"])))
        ).scalar_one()
        assert row.company_name == COMPANY["company_name"]
        assert row.siret == SIRET_OK
        assert row.address_line1 == COMPANY["address_line1"]
        assert row.kind == InvoiceKind.invoice
