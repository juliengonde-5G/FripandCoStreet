# Extrait de Vintiz (tests/test_fiscal.py + tests/test_nf525_chain.py),
# adapte a la signature v3 (D3, pas de branche legacy).
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.pos import Transaction, TransactionType
from app.services.fiscal import FiscalService

pytestmark = pytest.mark.anyio


async def _sell(client, auth_headers, amount: str = "10.00"):
    r = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": str(uuid.uuid4()),
            "items": [{"label": "Article", "unit_price": amount, "quantity": 1}],
            "payments": [{"method": "cash", "amount": amount, "tendered_amount": amount}],
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_verify_chain_integrity_valid_after_clean_sales(client, auth_headers, open_drawer):
    await _sell(client, auth_headers, "10.00")
    await _sell(client, auth_headers, "20.00")

    async with async_session() as db:
        result = await FiscalService(db).verify_chain_integrity()
    assert result["valid"] is True
    assert result["checked"] == 2


async def test_first_transaction_chains_to_genesis(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    async with async_session() as db:
        tx = (await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))).scalar_one()
        assert tx.previous_hash == "0"


async def test_verify_chain_detects_transaction_inserted_out_of_chain(client, auth_headers, open_drawer):
    """§8 : `verify_chain_integrity` detecte une transaction inseree hors
    chaine — ici une ligne signee avec un `previous_hash` bidon, inseree
    directement en base (le trigger n'interdit QUE l'UPDATE/DELETE, pas
    l'INSERT : la protection contre une fausse ecriture directe est la
    verification de la chaine elle-meme, pas le trigger)."""
    await _sell(client, auth_headers, "10.00")

    async with async_session() as db:
        rogue = Transaction(
            transaction_number=999,
            transaction_type=TransactionType.sale,
            user_id=(await db.execute(select(Transaction.user_id).limit(1))).scalar_one(),
            client_uuid=uuid.uuid4(),
            tva_rate=Decimal("20.00"),
            total_ht=Decimal("8.33"),
            total_tva=Decimal("1.67"),
            total_ttc=Decimal("10.00"),
            hash_chain="deadbeef" * 8,
            previous_hash="not-the-real-previous-hash",
            receipt_number=999,
        )
        db.add(rogue)
        await db.commit()

    async with async_session() as db:
        result = await FiscalService(db).verify_chain_integrity()
    assert result["valid"] is False
    assert result["reason"] in {"previous_hash_mismatch", "signature_mismatch"}


async def test_verify_chain_fails_with_wrong_signing_key(client, auth_headers, open_drawer, monkeypatch):
    await _sell(client, auth_headers, "10.00")
    from app.core.config import settings

    monkeypatch.setattr(settings, "FISCAL_SIGNING_KEY", "une-autre-cle-totalement-differente-x")

    async with async_session() as db:
        result = await FiscalService(db).verify_chain_integrity()
    assert result["valid"] is False


async def test_signature_version_is_3(client, auth_headers, open_drawer):
    sale = await _sell(client, auth_headers)
    async with async_session() as db:
        tx = (await db.execute(select(Transaction).where(Transaction.id == uuid.UUID(sale["id"])))).scalar_one()
        assert tx.fiscal_signature_version == 3


async def test_admin_fiscal_integrity_endpoint(client, auth_headers, open_drawer):
    await _sell(client, auth_headers)
    await client.post("/api/pos/drawer/close", json={"closing_amount": "110.00"}, headers=auth_headers)

    r = await client.get("/api/admin/fiscal/integrity", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["transactions"]["valid"] is True
    assert body["z_reports"]["valid"] is True
    assert body["jet"]["valid"] is True
