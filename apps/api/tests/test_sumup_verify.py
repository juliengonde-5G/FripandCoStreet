# Tests `app/services/sumup_verify.py` — vérification serveur d'un paiement
# CB avant écriture de vente (D5, §4.5 ARCHITECTURE_PR2.md). La couche HTTP
# SumUp elle-même est déjà couverte par `test_sumup_service.py` : ici on
# monkeypatch directement `SumUpService.get_checkout_status`/`get_transaction`
# pour isoler la logique d'orchestration (attempt trouvé, client_uuid
# identique, statut PAID, montant au centime).
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session
from app.models.payment_attempt import PaymentAttempt, PaymentAttemptStatus
from app.services.sumup_service import SumUpService
from app.services.sumup_verify import (
    CardNotConfirmed,
    CardTenderInput,
    verify_card_tender,
)

pytestmark = pytest.mark.anyio


async def _insert_pending_attempt(
    *,
    client_uuid: uuid.UUID,
    checkout_id: str,
    amount: Decimal = Decimal("10.00"),
    client_transaction_id: str | None = None,
) -> None:
    async with async_session() as db:
        db.add(
            PaymentAttempt(
                client_uuid=client_uuid,
                amount=amount,
                status=PaymentAttemptStatus.pending,
                checkout_id=checkout_id,
                client_transaction_id=client_transaction_id or checkout_id,
                reader_id="reader-1",
                attempt_count=1,
            )
        )
        await db.commit()


async def _get_attempt(checkout_id: str) -> PaymentAttempt:
    async with async_session() as db:
        return (
            await db.execute(
                select(PaymentAttempt).where(PaymentAttempt.checkout_id == checkout_id)
            )
        ).scalar_one()


def _patch_paid(
    monkeypatch,
    *,
    amount: Decimal = Decimal("10.00"),
    expected_client_transaction_id: str | None = None,
):
    async def fake_status(self, checkout_id, *, client_transaction_id=None):
        # PR13 — la relecture doit présenter l'identifiant SumUp de l'essai,
        # jamais notre `checkout_id`.
        if expected_client_transaction_id is not None:
            assert client_transaction_id == expected_client_transaction_id
        return {
            "checkout_id": checkout_id,
            "status": "PAID",
            "amount": float(amount),
            "sumup_transaction_id": "txn-1",
            "sumup_transaction_code": "TC1",
            "sumup_auth_code": "AUTH1",
            "sumup_card_brand": "visa",
            "sumup_card_last4": "4242",
        }

    async def fake_transaction(self, **kwargs):
        return {
            "id": "txn-1",
            "transaction_code": "TC1-FULL",
            "auth_code": "AUTH1-FULL",
            "card": {"type": "visa", "last_4_digits": "4242"},
        }

    monkeypatch.setattr(SumUpService, "get_checkout_status", fake_status)
    monkeypatch.setattr(SumUpService, "get_transaction", fake_transaction)


async def test_verify_card_tender_success(monkeypatch):
    client_uuid = uuid.uuid4()
    checkout_id = str(client_uuid)
    await _insert_pending_attempt(
        client_uuid=client_uuid, checkout_id=checkout_id, client_transaction_id="ctid_sumup"
    )
    _patch_paid(monkeypatch, expected_client_transaction_id="ctid_sumup")

    async with async_session() as db:
        result = await verify_card_tender(
            db, CardTenderInput(checkout_id=checkout_id, amount=Decimal("10.00")), client_uuid
        )
        await db.commit()

    assert result.sumup_checkout_id == checkout_id
    # get_transaction() est la source canonique — ses champs l'emportent
    # sur ceux (déjà présents) du poll de statut.
    assert result.sumup_transaction_code == "TC1-FULL"
    assert result.sumup_auth_code == "AUTH1-FULL"
    assert result.sumup_card_brand == "visa"
    assert result.sumup_card_last4 == "4242"

    attempt = await _get_attempt(checkout_id)
    assert attempt.status == PaymentAttemptStatus.paid
    assert attempt.sumup_transaction_code == "TC1-FULL"


async def test_verify_card_tender_checkout_not_found(monkeypatch):
    _patch_paid(monkeypatch)
    async with async_session() as db:
        with pytest.raises(CardNotConfirmed):
            await verify_card_tender(
                db,
                CardTenderInput(checkout_id="does-not-exist", amount=Decimal("10.00")),
                uuid.uuid4(),
            )


async def test_verify_card_tender_client_uuid_mismatch(monkeypatch):
    client_uuid = uuid.uuid4()
    other_uuid = uuid.uuid4()
    checkout_id = f"ctid-{client_uuid}"
    await _insert_pending_attempt(client_uuid=client_uuid, checkout_id=checkout_id)
    _patch_paid(monkeypatch)

    async with async_session() as db:
        with pytest.raises(CardNotConfirmed):
            await verify_card_tender(
                db,
                CardTenderInput(checkout_id=checkout_id, amount=Decimal("10.00")),
                other_uuid,
            )

    attempt = await _get_attempt(checkout_id)
    assert attempt.status == PaymentAttemptStatus.pending  # rien écrit


async def test_verify_card_tender_status_not_paid(monkeypatch):
    client_uuid = uuid.uuid4()
    checkout_id = f"ctid-{client_uuid}"
    await _insert_pending_attempt(client_uuid=client_uuid, checkout_id=checkout_id)

    async def fake_status(self, checkout_id, *, client_transaction_id=None):
        return {"checkout_id": checkout_id, "status": "PENDING"}

    monkeypatch.setattr(SumUpService, "get_checkout_status", fake_status)

    async with async_session() as db:
        with pytest.raises(CardNotConfirmed):
            await verify_card_tender(
                db, CardTenderInput(checkout_id=checkout_id, amount=Decimal("10.00")), client_uuid
            )

    attempt = await _get_attempt(checkout_id)
    assert attempt.status == PaymentAttemptStatus.pending


async def test_verify_card_tender_amount_mismatch(monkeypatch):
    client_uuid = uuid.uuid4()
    checkout_id = f"ctid-{client_uuid}"
    await _insert_pending_attempt(client_uuid=client_uuid, checkout_id=checkout_id, amount=Decimal("10.00"))
    # SumUp confirme un montant DIFFÉRENT de celui de la ligne de paiement.
    _patch_paid(monkeypatch, amount=Decimal("9.99"))

    async with async_session() as db:
        with pytest.raises(CardNotConfirmed, match="Montant"):
            await verify_card_tender(
                db, CardTenderInput(checkout_id=checkout_id, amount=Decimal("10.00")), client_uuid
            )

    attempt = await _get_attempt(checkout_id)
    assert attempt.status == PaymentAttemptStatus.pending
