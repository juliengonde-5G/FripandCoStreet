# Tests `app/api/pos/cb_router.py` — les 5 routes CB SumUp du contrat PR2
# (§4.5/§5). Monte `cb_router` sur l'app FastAPI partagée par `conftest.py`
# (agent A monte ce même routeur dans `app/main.py` via un import protégé —
# pas encore fait au moment d'écrire ces tests, donc on le monte nous-mêmes
# ici, sans toucher à `app/main.py`).
#
# La couche HTTP SumUp est mockée via `SumUpService._transport`
# (`httpx.MockTransport`) — on monkeypatche `SumUpService.__init__` pour que
# CHAQUE instance créée par le routeur (il en crée une par requête) reçoive
# le même transport de test, la même config, et le pré-vol du reader.
from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

import app.services.sumup_service as svc_mod
from app.api.pos import cb_router as cb_router_mod
from app.api.pos.cb_router import router as cb_router
from app.core.database import async_session
from app.main import app as fastapi_app
from app.models.jet import JournalEvent
from app.models.payment_attempt import PaymentAttempt, PaymentAttemptStatus
from app.services.sumup_service import SumUpService

pytestmark = pytest.mark.anyio

if not any(
    getattr(r, "path", "").startswith("/api/pos/payments/cb") for r in fastapi_app.routes
):
    fastapi_app.include_router(cb_router, prefix="/api")


@pytest.fixture(autouse=True)
def _reset_ping_cache():
    cb_router_mod._reset_ping_cache()
    yield
    cb_router_mod._reset_ping_cache()


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(svc_mod, "RETRY_WAIT_MULTIPLIER", 0.0)
    monkeypatch.setattr(svc_mod, "RETRY_WAIT_MIN", 0.0)
    monkeypatch.setattr(svc_mod, "RETRY_WAIT_MAX", 0.0)


def _configure_sumup(monkeypatch, handler, *, configured: bool = True) -> None:
    """Fait en sorte que chaque `SumUpService()` créé par le routeur soit
    configuré et route son HTTP par ``handler`` (comme Vintiz : le service
    est instancié à la demande dans chaque endpoint, pas injecté)."""
    original_init = SumUpService.__init__

    def patched_init(self):
        original_init(self)
        if configured:
            self.api_key = "sup_sk_live_abc123"
            self.merchant_code = "MTEST"
            self.reader_id = "reader-1"
        else:
            self.api_key = ""
            self.merchant_code = ""
            self.reader_id = ""
        self._transport = httpx.MockTransport(handler)

    monkeypatch.setattr(SumUpService, "__init__", patched_init)


def _online_reader_handler(push_response: httpx.Response | None = None):
    """Handler générique : pairing OK, live ONLINE, push -> `push_response`."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/status") and "/readers/" in path:
            return httpx.Response(200, json={"data": {"status": "ONLINE"}})
        if path.endswith("/checkout"):
            return push_response or httpx.Response(202, json={"data": {}})
        if "/readers/" in path:
            return httpx.Response(200, json={"status": "paired", "name": "Solo"})
        if path.endswith("/transactions"):
            return httpx.Response(404)  # pas encore tapé
        return httpx.Response(404, json={})

    return handler


async def _last_event(event_type: str) -> list[JournalEvent]:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(JournalEvent)
                .where(JournalEvent.event_type == event_type)
                .order_by(JournalEvent.seq.asc())
            )
        ).scalars().all()
        return list(rows)


async def _get_attempt(checkout_id: str) -> PaymentAttempt:
    async with async_session() as db:
        return (
            await db.execute(
                select(PaymentAttempt).where(PaymentAttempt.checkout_id == checkout_id)
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


async def test_status_unconfigured(client, auth_headers, monkeypatch):
    _configure_sumup(monkeypatch, _online_reader_handler(), configured=False)
    resp = await client.get("/api/pos/payments/cb/status", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["reader_online"] is False


async def test_status_online(client, auth_headers, monkeypatch):
    _configure_sumup(monkeypatch, _online_reader_handler())
    resp = await client.get("/api/pos/payments/cb/status", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["reader_online"] is True


async def test_status_requires_auth(client):
    resp = await client.get("/api/pos/payments/cb/status")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /initiate — push OK
# ---------------------------------------------------------------------------


async def test_initiate_push_ok(client, auth_headers, monkeypatch):
    _configure_sumup(monkeypatch, _online_reader_handler())
    client_uuid = str(uuid.uuid4())
    resp = await client.post(
        "/api/pos/payments/cb/initiate",
        json={"amount": "12.50", "client_uuid": client_uuid},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["checkout_id"] == client_uuid

    attempt = await _get_attempt(client_uuid)
    assert attempt.status == PaymentAttemptStatus.pending
    assert attempt.amount == Decimal("12.50")

    events = await _last_event(cb_router_mod.EVENT_CB_INITIATED)
    assert any(e.payload.get("checkout_id") == client_uuid for e in events)


async def test_initiate_duplicate_pending_returns_409(client, auth_headers, monkeypatch):
    _configure_sumup(monkeypatch, _online_reader_handler())
    client_uuid = str(uuid.uuid4())
    first = await client.post(
        "/api/pos/payments/cb/initiate",
        json={"amount": "5.00", "client_uuid": client_uuid},
        headers=auth_headers,
    )
    assert first.status_code == 200

    second = await client.post(
        "/api/pos/payments/cb/initiate",
        json={"amount": "5.00", "client_uuid": client_uuid},
        headers=auth_headers,
    )
    assert second.status_code == 409
    body = second.json()
    assert body["code"] == "attempt_pending"
    assert body["checkout_id"] == client_uuid


# ---------------------------------------------------------------------------
# POST /initiate — reader hors ligne / non configuré → 409 reader_unavailable
# ---------------------------------------------------------------------------


async def test_initiate_unconfigured_returns_409_reader_unavailable(client, auth_headers, monkeypatch):
    _configure_sumup(monkeypatch, _online_reader_handler(), configured=False)
    resp = await client.post(
        "/api/pos/payments/cb/initiate",
        json={"amount": "5.00", "client_uuid": str(uuid.uuid4())},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "reader_unavailable"


async def test_initiate_offline_reader_returns_409_reader_unavailable(client, auth_headers, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/status") and "/readers/" in path:
            return httpx.Response(200, json={"data": {"status": "OFFLINE"}})
        if "/readers/" in path:
            return httpx.Response(200, json={"status": "paired", "name": "Solo"})
        return httpx.Response(404, json={})

    _configure_sumup(monkeypatch, handler)
    resp = await client.post(
        "/api/pos/payments/cb/initiate",
        json={"amount": "5.00", "client_uuid": str(uuid.uuid4())},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "reader_unavailable"

    # Aucun essai n'est créé : il n'existe pas encore de checkout_id à tracer.
    async with async_session() as db:
        count = len((await db.execute(select(PaymentAttempt))).scalars().all())
    assert count == 0


async def test_initiate_test_key_refused_in_production(client, auth_headers, monkeypatch):
    def patched_init(self):
        self.api_key = "sup_sk_test_abc123"
        self.merchant_code = "MTEST"
        self.reader_id = "reader-1"
        self._api_base = "https://api.sumup.com/v0.1"
        self._transport = httpx.MockTransport(_online_reader_handler())

    monkeypatch.setattr(SumUpService, "__init__", patched_init)
    monkeypatch.setattr(cb_router_mod.settings, "ENVIRONMENT", "production")
    try:
        resp = await client.post(
            "/api/pos/payments/cb/initiate",
            json={"amount": "5.00", "client_uuid": str(uuid.uuid4())},
            headers=auth_headers,
        )
    finally:
        monkeypatch.setattr(cb_router_mod.settings, "ENVIRONMENT", "test")
    assert resp.status_code == 409
    assert resp.json()["code"] == "test_key_in_production"


# ---------------------------------------------------------------------------
# POST /initiate — push refusé par SumUp (definitive) → attempt failed + JET
# ---------------------------------------------------------------------------


async def test_initiate_push_failure_records_failed_attempt(client, auth_headers, monkeypatch):
    _configure_sumup(
        monkeypatch,
        _online_reader_handler(push_response=httpx.Response(401, json={"error_code": "UNAUTHORIZED"})),
    )
    client_uuid = str(uuid.uuid4())
    resp = await client.post(
        "/api/pos/payments/cb/initiate",
        json={"amount": "5.00", "client_uuid": client_uuid},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "payment_failed"

    attempt = await _get_attempt(client_uuid)
    assert attempt.status == PaymentAttemptStatus.failed
    assert attempt.error_message

    events = await _last_event(cb_router_mod.EVENT_CB_FAILED)
    assert any(e.payload.get("checkout_id") == client_uuid for e in events)


# ---------------------------------------------------------------------------
# GET /{checkout_id}/status — pending -> paid, un seul JET payment.cb_paid
# ---------------------------------------------------------------------------


async def test_status_poll_pending_then_paid_emits_jet_once(client, auth_headers, monkeypatch):
    state = {"paid": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/status") and "/readers/" in path:
            return httpx.Response(200, json={"data": {"status": "ONLINE"}})
        if path.endswith("/checkout"):
            return httpx.Response(202, json={"data": {}})
        if "/readers/" in path:
            return httpx.Response(200, json={"status": "paired", "name": "Solo"})
        if path.endswith("/transactions"):
            if not state["paid"]:
                return httpx.Response(404)  # pas encore tapé
            return httpx.Response(
                200,
                json={
                    "id": "txn-1",
                    "status": "SUCCESSFUL",
                    "transaction_code": "TC1",
                    "auth_code": "AUTH1",
                    "amount": 7.0,
                    "currency": "EUR",
                    "card": {"type": "mastercard", "last_4_digits": "9999"},
                },
            )
        return httpx.Response(404, json={})

    _configure_sumup(monkeypatch, handler)
    client_uuid = str(uuid.uuid4())
    init = await client.post(
        "/api/pos/payments/cb/initiate",
        json={"amount": "7.00", "client_uuid": client_uuid},
        headers=auth_headers,
    )
    assert init.status_code == 200
    checkout_id = init.json()["checkout_id"]

    # 1) Toujours en attente — le client n'a pas encore tapé sa carte.
    poll1 = await client.get(f"/api/pos/payments/cb/{checkout_id}/status", headers=auth_headers)
    assert poll1.json()["status"] == "pending"

    # 2) Le client tape sa carte.
    state["paid"] = True
    poll2 = await client.get(f"/api/pos/payments/cb/{checkout_id}/status", headers=auth_headers)
    assert poll2.json()["status"] == "paid"
    assert poll2.json()["transaction_code"] == "TC1"
    assert poll2.json()["card_brand"] == "mastercard"
    assert poll2.json()["last4"] == "9999"

    # 3) Un poll supplémentaire ne re-sonde plus SumUp (l'attempt est déjà
    # terminal) — même résultat, mais pas de second événement JET.
    poll3 = await client.get(f"/api/pos/payments/cb/{checkout_id}/status", headers=auth_headers)
    assert poll3.json()["status"] == "paid"

    events = await _last_event(cb_router_mod.EVENT_CB_PAID)
    matching = [e for e in events if e.payload.get("checkout_id") == checkout_id]
    assert len(matching) == 1  # une seule fois par transition (§4.5)

    attempt = await _get_attempt(checkout_id)
    assert attempt.status == PaymentAttemptStatus.paid
    assert attempt.sumup_card_last4 == "9999"


async def test_status_poll_failed(client, auth_headers, monkeypatch):
    state = {"resp": httpx.Response(404)}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/status") and "/readers/" in path:
            return httpx.Response(200, json={"data": {"status": "ONLINE"}})
        if path.endswith("/checkout"):
            return httpx.Response(202, json={"data": {}})
        if "/readers/" in path:
            return httpx.Response(200, json={"status": "paired", "name": "Solo"})
        if path.endswith("/transactions"):
            return state["resp"]
        return httpx.Response(404, json={})

    _configure_sumup(monkeypatch, handler)
    client_uuid = str(uuid.uuid4())
    init = await client.post(
        "/api/pos/payments/cb/initiate",
        json={"amount": "3.00", "client_uuid": client_uuid},
        headers=auth_headers,
    )
    checkout_id = init.json()["checkout_id"]

    state["resp"] = httpx.Response(
        200,
        json={"id": "txn-2", "status": "FAILED"},
    )
    poll = await client.get(f"/api/pos/payments/cb/{checkout_id}/status", headers=auth_headers)
    assert poll.json()["status"] == "failed"

    attempt = await _get_attempt(checkout_id)
    assert attempt.status == PaymentAttemptStatus.failed


async def test_status_not_found(client, auth_headers, monkeypatch):
    _configure_sumup(monkeypatch, _online_reader_handler())
    resp = await client.get("/api/pos/payments/cb/does-not-exist/status", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# DELETE /{checkout_id} — annulation
# ---------------------------------------------------------------------------


async def test_cancel_pending_payment(client, auth_headers, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/status") and "/readers/" in path:
            return httpx.Response(200, json={"data": {"status": "ONLINE"}})
        if path.endswith("/checkout"):
            return httpx.Response(202, json={"data": {}})
        if path.endswith("/terminate"):
            return httpx.Response(202)
        if "/readers/" in path:
            return httpx.Response(200, json={"status": "paired", "name": "Solo"})
        return httpx.Response(404, json={})

    _configure_sumup(monkeypatch, handler)
    client_uuid = str(uuid.uuid4())
    init = await client.post(
        "/api/pos/payments/cb/initiate",
        json={"amount": "4.00", "client_uuid": client_uuid},
        headers=auth_headers,
    )
    checkout_id = init.json()["checkout_id"]

    resp = await client.delete(f"/api/pos/payments/cb/{checkout_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True

    attempt = await _get_attempt(checkout_id)
    assert attempt.status == PaymentAttemptStatus.cancelled

    events = await _last_event(cb_router_mod.EVENT_CB_CANCELLED)
    assert any(e.payload.get("checkout_id") == checkout_id for e in events)


async def test_cancel_not_found(client, auth_headers, monkeypatch):
    _configure_sumup(monkeypatch, _online_reader_handler())
    resp = await client.delete("/api/pos/payments/cb/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /{checkout_id}/retry
# ---------------------------------------------------------------------------


async def test_retry_after_failure_creates_new_attempt(client, auth_headers, monkeypatch):
    state = {"fail_push": True}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/status") and "/readers/" in path:
            return httpx.Response(200, json={"data": {"status": "ONLINE"}})
        if path.endswith("/checkout"):
            if state["fail_push"]:
                return httpx.Response(422, json={"error_code": "READER_BUSY"})
            return httpx.Response(202, json={"data": {}})
        if "/readers/" in path:
            return httpx.Response(200, json={"status": "paired", "name": "Solo"})
        return httpx.Response(404, json={})

    _configure_sumup(monkeypatch, handler)
    client_uuid = str(uuid.uuid4())
    init = await client.post(
        "/api/pos/payments/cb/initiate",
        json={"amount": "6.00", "client_uuid": client_uuid},
        headers=auth_headers,
    )
    assert init.status_code == 409
    checkout_id = init.json()["checkout_id"]

    state["fail_push"] = False
    retry = await client.post(f"/api/pos/payments/cb/{checkout_id}/retry", headers=auth_headers)
    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert body["status"] == "pending"
    assert body["checkout_id"] != checkout_id  # nouveau checkout_id (contrainte d'unicité)

    new_attempt = await _get_attempt(body["checkout_id"])
    assert new_attempt.attempt_count == 2
    assert new_attempt.client_uuid == uuid.UUID(client_uuid)
    assert new_attempt.status == PaymentAttemptStatus.pending

    old_attempt = await _get_attempt(checkout_id)
    assert old_attempt.status == PaymentAttemptStatus.failed  # inchangé


async def test_retry_not_retryable_when_pending(client, auth_headers, monkeypatch):
    _configure_sumup(monkeypatch, _online_reader_handler())
    client_uuid = str(uuid.uuid4())
    init = await client.post(
        "/api/pos/payments/cb/initiate",
        json={"amount": "6.00", "client_uuid": client_uuid},
        headers=auth_headers,
    )
    checkout_id = init.json()["checkout_id"]

    resp = await client.post(f"/api/pos/payments/cb/{checkout_id}/retry", headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == "not_retryable"
