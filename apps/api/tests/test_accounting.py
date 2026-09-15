# Nouveau test (PR4, docs/ARCHITECTURE_PR4.md §7) — ecriture comptable par Z
# (F2), config F1, CSV mensuel (F3, fixture octet pour octet sur le jeu
# d'essai PR2, `docs/JEU_ESSAI_PR2.md`), FEC (F3), `verify_export` (F2/F3),
# JET `accounting.export_created`/`export.downloaded`.
from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.accounting import AccountingExport, AccountingExportLine
from app.models.jet import JournalEvent
from app.models.pos import ZReport
from app.services.accounting_service import AccountingService

pytestmark = pytest.mark.anyio


def _uid() -> str:
    return str(uuid.uuid4())


async def _fake_verify_card(db, tender, client_uuid):
    return SimpleNamespace(
        sumup_checkout_id=tender.checkout_id,
        sumup_transaction_id=f"TXN-{tender.checkout_id}",
        sumup_transaction_code="CODE-1",
        sumup_auth_code="000000",
        sumup_card_brand="VISA",
        sumup_card_last4="4242",
    )


async def _replay_jeu_essai_pr2(client, auth_headers, monkeypatch) -> dict:
    """Rejoue le scenario de `docs/JEU_ESSAI_PR2.md` (Z n°1) : ouverture
    100,00 ; ventes A/B/C/D ; annulation A/B ; mouvement sortie/entree ;
    cloture comptee 137,00. Retourne le Z serialise (JSON)."""
    monkeypatch.setattr("app.services.pos.verify_card_tender", _fake_verify_card)

    async def _fake_refund_card(sumup_transaction_id, amount):
        return {"status": "REFUNDED"}

    monkeypatch.setattr("app.services.refund.refund_card_payment", _fake_refund_card)

    open_r = await client.post(
        "/api/pos/drawer/open", json={"opening_amount": "100.00"}, headers=auth_headers
    )
    assert open_r.status_code == 200, open_r.text

    # Vente A — especes : Robe 25,00 + Chemise 15,00, remis 50,00 -> TTC 40,00
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _uid(),
            "items": [
                {"label": "Robe", "unit_price": "25.00", "quantity": 1},
                {"label": "Chemise", "unit_price": "15.00", "quantity": 1},
            ],
            "payments": [{"method": "cash", "amount": "40.00", "tendered_amount": "50.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    sale_a = r.json()

    # Vente B — CB : Veste 60,00, remise 10% -> TTC 54,00
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _uid(),
            "items": [{"label": "Veste", "unit_price": "60.00", "quantity": 1}],
            "discount": {"type": "percent", "value": "10"},
            "payments": [{"method": "card", "amount": "54.00", "checkout_id": "chk_B"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    sale_b = r.json()

    # Vente C — mixte : Pantalon 30 + Ceinture 10 + Echarpe 10, remise 5,00 €,
    # especes 20,00 (remis 20,00) + CB 25,00 -> TTC 45,00
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _uid(),
            "items": [
                {"label": "Pantalon", "unit_price": "30.00", "quantity": 1},
                {"label": "Ceinture", "unit_price": "10.00", "quantity": 1},
                {"label": "Écharpe", "unit_price": "10.00", "quantity": 1},
            ],
            "discount": {"type": "amount", "value": "5.00"},
            "payments": [
                {"method": "cash", "amount": "20.00", "tendered_amount": "20.00"},
                {"method": "card", "amount": "25.00", "checkout_id": "chk_C"},
            ],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text

    # Vente D — especes : 3 x « Article » a 10,00, remise 10% -> TTC 27,00
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _uid(),
            "items": [
                {"label": "Article", "unit_price": "10.00", "quantity": 1},
                {"label": "Article", "unit_price": "10.00", "quantity": 1},
                {"label": "Article", "unit_price": "10.00", "quantity": 1},
            ],
            "discount": {"type": "percent", "value": "10"},
            "payments": [{"method": "cash", "amount": "27.00", "tendered_amount": "27.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text

    # Annulation de A (especes 40,00)
    r = await client.post(
        f"/api/pos/transactions/{sale_a['id']}/cancel",
        json={"reason": "erreur de saisie"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text

    # Annulation de B (CB 54,00)
    r = await client.post(
        f"/api/pos/transactions/{sale_b['id']}/cancel",
        json={"reason": "client a changé d'avis"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text

    # Mouvements de caisse
    r = await client.post(
        "/api/pos/cash-movements",
        json={"direction": "out", "amount": "20.00", "reason": "bank_deposit"},
        headers=auth_headers,
    )
    assert r.status_code == 201 or r.status_code == 200, r.text
    r = await client.post(
        "/api/pos/cash-movements",
        json={"direction": "in", "amount": "10.00", "reason": "float_top_up"},
        headers=auth_headers,
    )
    assert r.status_code == 201 or r.status_code == 200, r.text

    # Cloture — compte 137,00
    r = await client.post(
        "/api/pos/drawer/close", json={"closing_amount": "137.00"}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# F1 — config
# ---------------------------------------------------------------------------


async def test_accounting_config_defaults_match_f1(client, auth_headers):
    r = await client.get("/api/admin/settings/accounting", headers=auth_headers)
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["journal_code"] == "VTE"
    assert cfg["account_sales"] == "707100"
    assert cfg["account_tva"] == "44571"
    assert cfg["account_cash"] == "531000"
    assert cfg["account_card"] == "512000"
    assert cfg["account_rounding_expense"] == "658000"
    assert cfg["account_rounding_income"] == "758000"


async def test_accounting_config_put_rejects_invalid_journal(client, auth_headers):
    r = await client.put(
        "/api/admin/settings/accounting",
        json={"journal_code": "TROPLONG123"},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_setting"


async def test_accounting_config_put_accepts_defaults_roundtrip(client, auth_headers):
    """Un manager qui re-soumet les valeurs par defaut F1 telles quelles
    (ecran de reglages pre-rempli) ne doit jamais essuyer un 422 — voir le
    commentaire sur `_ACCOUNT_NUMBER_RE` dans `app/api/admin/router.py`."""
    r = await client.get("/api/admin/settings/accounting", headers=auth_headers)
    defaults = r.json()
    r2 = await client.put("/api/admin/settings/accounting", json=defaults, headers=auth_headers)
    assert r2.status_code == 200, r2.text


# ---------------------------------------------------------------------------
# F2 — ecriture par Z, fidele au jeu d'essai PR2 (Z n°1)
# ---------------------------------------------------------------------------


async def test_journal_entry_balanced_on_jeu_essai_z1(client, auth_headers, monkeypatch):
    z = await _replay_jeu_essai_pr2(client, auth_headers, monkeypatch)
    assert z["report_number"] == 1
    # `payment_totals` est serialise en chaines formatees ("47.00") par
    # `FiscalService._money`, pas en float — cf. app/services/fiscal.py.
    assert z["payment_totals"]["cash"]["net"] == "47.00"
    assert z["payment_totals"]["card"]["net"] == "25.00"
    assert z["total_ht"] == 60.0
    assert z["total_tva"] == 12.0

    async with async_session() as db:
        export = (
            await db.execute(
                select(AccountingExport).where(AccountingExport.z_report_id == uuid.UUID(z["id"]))
            )
        ).scalar_one()
        lines = (
            await db.execute(
                select(AccountingExportLine)
                .where(AccountingExportLine.export_id == export.id)
                .order_by(AccountingExportLine.line_number)
            )
        ).scalars().all()

    by_account = {ln.account_number: ln for ln in lines}
    assert Decimal(str(by_account["531000"].debit)) == Decimal("47.00")  # caisse
    assert Decimal(str(by_account["512000"].debit)) == Decimal("25.00")  # CB
    assert Decimal(str(by_account["707100"].credit)) == Decimal("60.00")  # ventes HT
    assert Decimal(str(by_account["44571"].credit)) == Decimal("12.00")  # TVA collectée
    assert "658000" not in by_account and "758000" not in by_account  # pas d'ajustement

    total_debit = sum((Decimal(str(ln.debit)) for ln in lines), Decimal("0"))
    total_credit = sum((Decimal(str(ln.credit)) for ln in lines), Decimal("0"))
    assert total_debit == total_credit == Decimal("72.00")

    # Piece de reference partagee = Z0001 sur toutes les lignes.
    assert {ln.piece_reference for ln in lines} == {"Z0001"}

    # JET accounting.export_created ecrit exactement une fois pour ce Z.
    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "accounting.export_created")
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["z_number"] == 1
    assert events[0].payload["balanced"] is True


async def test_create_export_for_z_is_idempotent(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _uid(),
            "items": [{"label": "Article", "unit_price": "10.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "10.00", "tendered_amount": "10.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201
    r = await client.post(
        "/api/pos/drawer/close", json={"closing_amount": "110.00"}, headers=auth_headers
    )
    z_id = uuid.UUID(r.json()["id"])

    async with async_session() as db:
        z_report = (await db.execute(select(ZReport).where(ZReport.id == z_id))).scalar_one()
        export1, created1 = await AccountingService(db).create_export_for_z(z_report)
        export2, created2 = await AccountingService(db).create_export_for_z(z_report)
        await db.commit()
    assert created1 is False  # deja cree par close_drawer (meme transaction)
    assert created2 is False
    assert export1.id == export2.id

    async with async_session() as db:
        count = len(
            (
                await db.execute(
                    select(AccountingExport).where(AccountingExport.z_report_id == z_id)
                )
            ).scalars().all()
        )
    assert count == 1

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "accounting.export_created")
            )
        ).scalars().all()
    assert len(events) == 1  # jamais reecrit par les appels idempotents


# ---------------------------------------------------------------------------
# F3 — CSV mensuel Pennylane, fixture octet pour octet
# ---------------------------------------------------------------------------


async def test_generate_monthly_csv_matches_expected_fixture(client, auth_headers, monkeypatch):
    z = await _replay_jeu_essai_pr2(client, auth_headers, monkeypatch)
    close_date = z["closed_at"][:10]  # AAAA-MM-JJ (UTC)
    year, month, _ = close_date.split("-")

    async with async_session() as db:
        csv_text = await AccountingService(db).generate_monthly_csv(int(year), int(month))

    lines = csv_text.split("\r\n")
    assert lines[0] == (
        "Date;Code Journal;Numéro de compte;Libellé de compte;Libellé de ligne;"
        "Taux de TVA du compte;Code pays du compte;Libellé de pièce;Numéro de pièce;"
        "Débit et/ou Crédit;Crédit;Famille de catégories;Catégorie;"
        "Identifiant de ligne;Identifiant de lettrage"
    )
    # 4 lignes d'ecriture (caisse, CB, ventes, TVA) + entete + fin (\r\n final
    # -> un element vide en dernier via split).
    data_rows = [ln for ln in lines[1:] if ln]
    assert len(data_rows) == 4

    ecr_date = data_rows[0].split(";")[0]
    piece_label = f"Clôture caisse Z0001 du {ecr_date}"
    expected_rows = [
        f"{ecr_date};VTE;531000;Caisse;Caisse — Z0001;;;{piece_label};Z0001;47,00;0,00;;;;",
        f"{ecr_date};VTE;512000;CB SumUp;CB SumUp — Z0001;;;{piece_label};Z0001;25,00;0,00;;;;",
        f"{ecr_date};VTE;707100;Ventes marchandises;Ventes marchandises — Z0001;;;"
        f"{piece_label};Z0001;0,00;60,00;;;;",
        f"{ecr_date};VTE;44571;TVA collectée 20%;TVA collectée 20% — Z0001;;;"
        f"{piece_label};Z0001;0,00;12,00;;;;",
    ]
    assert data_rows == expected_rows
    assert csv_text.endswith("\r\n")


async def test_download_monthly_csv_route_has_bom_and_logs_jet(client, auth_headers, monkeypatch):
    z = await _replay_jeu_essai_pr2(client, auth_headers, monkeypatch)
    close_date = z["closed_at"][:10]
    year, month, _ = close_date.split("-")

    r = await client.get(
        f"/api/admin/accounting/monthly-csv/{year}/{month}", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"\xef\xbb\xbf")  # BOM UTF-8
    assert "ecritures_" in r.headers["content-disposition"]

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "export.downloaded")
            )
        ).scalars().all()
    kinds = [e.payload["kind"] for e in events]
    assert "accounting_monthly_csv" in kinds


# ---------------------------------------------------------------------------
# F3 — FEC (18 colonnes, EcritureNum sequentiel, equilibre)
# ---------------------------------------------------------------------------


async def test_daily_fec_18_columns_balanced_and_sequential(client, auth_headers, monkeypatch):
    z = await _replay_jeu_essai_pr2(client, auth_headers, monkeypatch)
    close_date = z["closed_at"][:10]

    async with async_session() as db:
        fec = await AccountingService(db).generate_daily_fec(
            __import__("datetime").date.fromisoformat(close_date)
        )
    lines = fec.split("\n")
    header = lines[0].split("\t")
    assert header == [
        "JournalCode", "JournalLib", "EcritureNum", "EcritureDate",
        "CompteNum", "CompteLib", "CompAuxNum", "CompAuxLib",
        "PieceRef", "PieceDate", "EcritureLib",
        "Debit", "Credit", "EcritureLet", "DateLet",
        "ValidDate", "Montantdevise", "Idevise",
    ]
    data_lines = [ln for ln in lines[1:] if ln]
    assert len(data_lines) == 4
    ecriture_nums = [ln.split("\t")[2] for ln in data_lines]
    assert ecriture_nums == ["0001-001", "0001-002", "0001-003", "0001-004"]

    total_debit = Decimal("0")
    total_credit = Decimal("0")
    for ln in data_lines:
        cols = ln.split("\t")
        total_debit += Decimal(cols[11].replace(",", "."))
        total_credit += Decimal(cols[12].replace(",", "."))
    assert total_debit == total_credit == Decimal("72.00")


async def test_download_fec_day_route_logs_jet(client, auth_headers, monkeypatch):
    z = await _replay_jeu_essai_pr2(client, auth_headers, monkeypatch)
    close_date = z["closed_at"][:10]
    r = await client.get(f"/api/admin/accounting/fec/day/{close_date}", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    assert "FEC_" in r.headers["content-disposition"]

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "export.downloaded")
            )
        ).scalars().all()
    kinds = [e.payload["kind"] for e in events]
    assert "fec_day" in kinds


async def test_download_fec_month_route(client, auth_headers, monkeypatch):
    z = await _replay_jeu_essai_pr2(client, auth_headers, monkeypatch)
    close_date = z["closed_at"][:10]
    year, month, _ = close_date.split("-")
    r = await client.get(
        f"/api/admin/accounting/fec/month/{year}/{month}", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert "Z0001" in r.text


# ---------------------------------------------------------------------------
# F2/F3 — verify_export detecte une divergence simulee
# ---------------------------------------------------------------------------


async def test_verify_export_detects_simulated_divergence(client, auth_headers, open_drawer):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": _uid(),
            "items": [{"label": "Article", "unit_price": "10.00", "quantity": 1}],
            "payments": [{"method": "cash", "amount": "10.00", "tendered_amount": "10.00"}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201
    r = await client.post(
        "/api/pos/drawer/close", json={"closing_amount": "110.00"}, headers=auth_headers
    )
    z_id = uuid.UUID(r.json()["id"])

    async with async_session() as db:
        z_report = (await db.execute(select(ZReport).where(ZReport.id == z_id))).scalar_one()
        result = await AccountingService(db).verify_export(z_report)
        assert result["valid"] is True

    # Simule une divergence realiste : le plan de comptes change APRES la
    # creation de l'ecriture (ex. la boutique change de banque et donc de
    # compte 512000). L'ecriture deja scellee reste sur l'ANCIEN compte
    # (immuable — trigger) ; `verify_export` recalcule avec la config
    # COURANTE et doit detecter que ca ne correspond plus.
    r = await client.get("/api/admin/settings/accounting", headers=auth_headers)
    cfg = r.json()
    cfg["account_cash"] = "531099"
    r = await client.put("/api/admin/settings/accounting", json=cfg, headers=auth_headers)
    assert r.status_code == 200, r.text

    async with async_session() as db:
        z_report = (await db.execute(select(ZReport).where(ZReport.id == z_id))).scalar_one()
        result2 = await AccountingService(db).verify_export(z_report)
        assert result2["valid"] is False
        assert result2["reason"] == "lines_mismatch"
        await db.commit()  # verify_export ne fait que flush() — au caller de committer.

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "accounting.mismatch")
            )
        ).scalars().all()
    assert len(events) == 1
