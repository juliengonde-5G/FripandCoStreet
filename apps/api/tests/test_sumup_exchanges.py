# Tests PR9 — journal des echanges SumUp (contrat K1) et routes admin de
# lecture/purge/analyse (contrat K2), docs/ARCHITECTURE_PR9.md §3.
#
# La couche HTTP SumUp est mockee via `SumUpService._transport`
# (`httpx.MockTransport`), exactement comme `tests/test_cb_router.py` : on
# exerce le VRAI chemin reseau du service (retry transport compris), seul le
# serveur d'en face est simule.
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import func, select

import app.services.sumup_service as svc_mod
from app.core.database import async_session
from app.models.jet import JournalEvent
from app.models.payment_attempt import PaymentAttempt, PaymentAttemptStatus
from app.models.sumup_exchange import SumUpExchange
from app.services.sumup_exchange_log import persist, purge
from app.services.sumup_service import MAX_RESPONSE_PAYLOAD_BYTES, SumUpService

pytestmark = pytest.mark.anyio

# Une vraie forme de cle SumUp, pour verifier qu'elle ne ressort NULLE PART
# du journal (elle est reconnue par `_API_KEY_RE`).
LEAKED_API_KEY = "sup_sk_live_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Rejeux transport instantanes — sinon chaque test attend le backoff."""
    monkeypatch.setattr(svc_mod, "RETRY_WAIT_MULTIPLIER", 0.0)
    monkeypatch.setattr(svc_mod, "RETRY_WAIT_MIN", 0.0)
    monkeypatch.setattr(svc_mod, "RETRY_WAIT_MAX", 0.0)


def _service(handler) -> SumUpService:
    """Service configure dont tout le HTTP part dans `handler`."""
    svc = SumUpService()
    svc.api_key = LEAKED_API_KEY
    svc.merchant_code = "MTEST"
    svc.reader_id = "reader-1"
    svc._transport = httpx.MockTransport(handler)
    return svc


def _ok_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/status") and "/readers/" in path:
        return httpx.Response(200, json={"data": {"status": "ONLINE"}})
    if path.endswith("/checkout"):
        return httpx.Response(202, json={"data": {}})
    if path.endswith("/terminate"):
        return httpx.Response(202, json={})
    if path.endswith("/refunds"):
        return httpx.Response(204)
    if "/readers/" in path:
        return httpx.Response(200, json={"status": "paired", "name": "Solo"})
    if path.endswith("/transactions"):
        return httpx.Response(200, json={"items": [{"status": "SUCCESSFUL", "amount": 10.0}]})
    return httpx.Response(404, json={})


# ---------------------------------------------------------------------------
# K1 — chaque methode publique produit un ExchangeRecord
# ---------------------------------------------------------------------------


async def test_ping_reader_records_exchanges():
    svc = _service(_ok_handler)
    await svc.ping_reader()
    # Appairage puis etat live : deux appels, donc deux traces.
    assert len(svc.exchanges) == 2
    assert {e.operation for e in svc.exchanges} == {"ping_reader"}
    assert all(e.method == "GET" and e.is_error is False for e in svc.exchanges)
    assert all(e.response_status == 200 for e in svc.exchanges)
    # Le chemin seul, jamais la query.
    assert all("?" not in e.url_path for e in svc.exchanges)


async def test_push_to_reader_records_exchange():
    svc = _service(_ok_handler)
    checkout = str(uuid.uuid4())
    await svc._push_to_reader(amount=Decimal("12.50"), client_transaction_id=checkout)
    record = svc.exchanges[-1]
    assert record.operation == "push_to_reader"
    assert record.method == "POST"
    assert record.checkout_id == checkout
    assert record.client_transaction_id == checkout
    assert record.response_status == 202
    # Le montant part bien en unites mineures dans la trace.
    assert record.request_payload["total_amount"]["value"] == 1250


async def test_checkout_status_and_terminate_and_refund_and_lookup():
    svc = _service(_ok_handler)
    checkout = str(uuid.uuid4())
    await svc.get_checkout_status(checkout)
    await svc.cancel_checkout(checkout)
    await svc.refund_transaction("txn-1", amount=Decimal("5.00"))
    await svc.get_transaction(transaction_id="txn-1")
    operations = [e.operation for e in svc.exchanges]
    assert operations == [
        "reader_checkout_status",
        "terminate_reader",
        "refund",
        "get_transaction",
    ]
    # Le lookup passe par la query : les parametres utiles sont conserves
    # dans le payload, le chemin reste nu.
    lookup = svc.exchanges[-1]
    assert lookup.request_payload == {"_params": {"id": "txn-1"}}
    assert lookup.url_path.endswith("/transactions")


async def test_drain_exchanges_empties_the_list():
    svc = _service(_ok_handler)
    await svc.get_transaction(transaction_id="txn-1")
    drained = svc.drain_exchanges()
    assert len(drained) == 1
    assert svc.exchanges == []


# ---------------------------------------------------------------------------
# K1 — redaction et troncature
# ---------------------------------------------------------------------------


async def test_exchange_never_contains_api_key_nor_authorization_header():
    def handler(request: httpx.Request) -> httpx.Response:
        # SumUp nous renvoie (par accident) l'en-tete qu'on lui a envoye et
        # une cle API dans son message d'erreur : ni l'un ni l'autre ne doit
        # survivre au passage dans le journal.
        return httpx.Response(
            401,
            json={
                "message": f"invalid key {LEAKED_API_KEY}",
                "authorization": request.headers.get("authorization"),
                "customer_email": "cliente@example.test",
            },
        )

    svc = _service(handler)
    await svc.get_transaction(transaction_id="txn-1")
    record = svc.exchanges[-1]
    serialized = repr(record)
    assert LEAKED_API_KEY not in serialized
    # Le mot « Bearer » peut subsister ; le jeton, lui, jamais.
    assert f"Bearer {LEAKED_API_KEY}" not in serialized
    assert "cliente@example.test" not in serialized
    assert record.response_payload["authorization"] == "<REDACTED>"
    assert record.response_payload["customer_email"] == "<REDACTED>"
    assert record.is_error is True
    assert record.error_type == "http_4xx"


async def test_response_payload_is_truncated_to_4_kb():
    def handler(request: httpx.Request) -> httpx.Response:
        # Chaque chaine est deja bornee a 1000 caracteres par la redaction :
        # c'est le VOLUME du document qui doit declencher la troncature.
        return httpx.Response(200, json={"blob": ["x" * 100] * 200})

    svc = _service(handler)
    await svc.get_transaction(transaction_id="txn-1")
    payload = svc.exchanges[-1].response_payload
    assert payload["_truncated"] is True
    assert len(payload["_excerpt"].encode("utf-8")) <= MAX_RESPONSE_PAYLOAD_BYTES


async def test_retry_count_counts_replayed_transport_errors():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"items": []})

    svc = _service(handler)
    await svc.get_transaction(transaction_id="txn-1")
    record = svc.exchanges[-1]
    # Trois essais avant la reponse : c'est exactement ce qu'on veut lire au
    # debogage (« le terminal a fini par repondre, mais au 3e essai »).
    assert record.retry_count == 3
    assert record.is_error is False


async def test_transport_failure_is_recorded_then_reraised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("injoignable", request=request)

    svc = _service(handler)
    result = await svc._push_to_reader(
        amount=Decimal("10.00"), client_transaction_id="abc"
    )
    assert result["status"] == "FAILED"
    record = svc.exchanges[-1]
    assert record.is_error is True
    assert record.error_type == "timeout"
    assert record.response_status is None
    assert record.retry_count >= 1


# ---------------------------------------------------------------------------
# K1 — persistance et purge
# ---------------------------------------------------------------------------


async def test_persist_writes_records_and_empties_the_list():
    svc = _service(_ok_handler)
    await svc.ping_reader()
    async with async_session() as db:
        written = await persist(db, svc.exchanges, request_id="req-42")
        await db.commit()
    assert written == 2
    assert svc.exchanges == []
    async with async_session() as db:
        rows = (await db.execute(select(SumUpExchange))).scalars().all()
    assert len(rows) == 2
    assert all(row.request_id == "req-42" for row in rows)


async def test_persist_never_fails_the_caller_on_sql_error():
    """Une trace de debogage qui ne s'ecrit pas ne doit pas perdre une vente."""
    svc = _service(_ok_handler)
    await svc.get_transaction(transaction_id="txn-1")
    # `checkout_id` est limite a 100 caracteres en base et n'est PAS tronque
    # cote service : la ligne sera refusee par PostgreSQL au flush.
    svc.exchanges[-1].checkout_id = "x" * 200

    async with async_session() as db:
        written = await persist(db, svc.exchanges)
        assert written == 0
        # La session doit rester utilisable : c'est tout l'interet du
        # savepoint. Le « paiement » qui suit passe normalement.
        attempt = PaymentAttempt(
            client_uuid=uuid.uuid4(),
            amount=Decimal("10.00"),
            status=PaymentAttemptStatus.paid,
            checkout_id="checkout-ok",
        )
        db.add(attempt)
        await db.commit()

    async with async_session() as db:
        assert (await db.execute(select(func.count(SumUpExchange.id)))).scalar_one() == 0
        assert (
            await db.execute(select(func.count(PaymentAttempt.id)))
        ).scalar_one() == 1


async def test_purge_removes_only_exchanges_older_than_retention():
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        for age_days in (1, 30, 200):
            db.add(
                SumUpExchange(
                    operation="ping_reader",
                    method="GET",
                    url_path="/v0.1/merchants/MTEST/readers/reader-1",
                    created_at=now - timedelta(days=age_days),
                )
            )
        await db.commit()

    async with async_session() as db:
        deleted = await purge(db, 90)
        await db.commit()
    assert deleted == 1

    async with async_session() as db:
        remaining = (await db.execute(select(func.count(SumUpExchange.id)))).scalar_one()
    assert remaining == 2


async def test_purge_clamps_an_out_of_range_retention():
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        db.add(
            SumUpExchange(
                operation="ping_reader",
                method="GET",
                url_path="/x",
                created_at=now - timedelta(days=10),
            )
        )
        await db.commit()
    async with async_session() as db:
        # 1 jour est hors bornes : ramene a 7, la ligne de 10 jours part.
        deleted = await purge(db, 1)
        await db.commit()
    assert deleted == 1


# ---------------------------------------------------------------------------
# K2 — routes admin
# ---------------------------------------------------------------------------


async def _seed_exchanges() -> None:
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        db.add_all(
            [
                SumUpExchange(
                    operation="ping_reader",
                    method="GET",
                    url_path="/v0.1/merchants/MTEST/readers/reader-1",
                    response_status=200,
                    duration_ms=12,
                    created_at=now - timedelta(hours=2),
                ),
                SumUpExchange(
                    operation="push_to_reader",
                    method="POST",
                    url_path="/v0.1/merchants/MTEST/readers/reader-1/checkout",
                    response_status=500,
                    is_error=True,
                    error_type="http_5xx",
                    error_message="SumUp indisponible",
                    checkout_id="checkout-A",
                    duration_ms=900,
                    created_at=now - timedelta(hours=1),
                ),
                SumUpExchange(
                    operation="push_to_reader",
                    method="POST",
                    url_path="/v0.1/merchants/MTEST/readers/reader-1/checkout",
                    is_error=True,
                    error_type="timeout",
                    error_message="terminal muet",
                    checkout_id="checkout-B",
                    retry_count=3,
                    created_at=now,
                ),
            ]
        )
        await db.commit()


async def test_admin_routes_require_auth(client):
    assert (await client.get("/api/admin/sumup-exchanges")).status_code == 401
    assert (await client.delete("/api/admin/sumup-exchanges")).status_code == 401
    assert (await client.get("/api/admin/payment-failures")).status_code == 401


async def test_list_exchanges_most_recent_first(client, auth_headers):
    await _seed_exchanges()
    resp = await client.get("/api/admin/sumup-exchanges", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert [e["checkout_id"] for e in body["exchanges"]] == [
        "checkout-B",
        "checkout-A",
        None,
    ]


async def test_list_exchanges_filters(client, auth_headers):
    await _seed_exchanges()

    only_failed = (
        await client.get(
            "/api/admin/sumup-exchanges?only_failed=true", headers=auth_headers
        )
    ).json()
    assert only_failed["total"] == 2

    by_operation = (
        await client.get(
            "/api/admin/sumup-exchanges?operation=ping_reader", headers=auth_headers
        )
    ).json()
    assert by_operation["total"] == 1

    by_error_type = (
        await client.get(
            "/api/admin/sumup-exchanges?error_type=timeout", headers=auth_headers
        )
    ).json()
    assert by_error_type["total"] == 1
    assert by_error_type["exchanges"][0]["retry_count"] == 3

    by_checkout = (
        await client.get(
            "/api/admin/sumup-exchanges?checkout_id=checkout-A", headers=auth_headers
        )
    ).json()
    assert by_checkout["total"] == 1

    # `total` compte les lignes filtrees, pas celles renvoyees.
    limited = (
        await client.get("/api/admin/sumup-exchanges?limit=1", headers=auth_headers)
    ).json()
    assert limited["total"] == 3
    assert len(limited["exchanges"]) == 1


async def test_list_exchanges_date_bounds(client, auth_headers):
    await _seed_exchanges()
    since = quote((datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat())
    body = (
        await client.get(
            f"/api/admin/sumup-exchanges?from={since}", headers=auth_headers
        )
    ).json()
    assert body["total"] == 2

    bad = await client.get(
        "/api/admin/sumup-exchanges?from=pas-une-date", headers=auth_headers
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "invalid_date"


async def test_delete_exchanges_purges_and_journals(client, auth_headers):
    await _seed_exchanges()
    resp = await client.delete("/api/admin/sumup-exchanges", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 3}

    async with async_session() as db:
        assert (await db.execute(select(func.count(SumUpExchange.id)))).scalar_one() == 0
        event = (
            await db.execute(
                select(JournalEvent).where(
                    JournalEvent.event_type == "sumup_exchanges.purged"
                )
            )
        ).scalar_one()
    assert event.payload == {"count": 3}


async def test_payment_failures_on_a_known_dataset(client, auth_headers):
    await _seed_exchanges()
    async with async_session() as db:
        db.add_all(
            [
                PaymentAttempt(
                    client_uuid=uuid.uuid4(),
                    amount=Decimal("10.00"),
                    status=PaymentAttemptStatus.paid,
                    checkout_id="c-paid",
                ),
                PaymentAttempt(
                    client_uuid=uuid.uuid4(),
                    amount=Decimal("20.00"),
                    status=PaymentAttemptStatus.failed,
                    checkout_id="c-failed-1",
                    error_message="Terminal injoignable",
                ),
                PaymentAttempt(
                    client_uuid=uuid.uuid4(),
                    amount=Decimal("30.00"),
                    status=PaymentAttemptStatus.failed,
                    checkout_id="c-failed-2",
                    error_message="Terminal injoignable",
                ),
                PaymentAttempt(
                    client_uuid=uuid.uuid4(),
                    amount=Decimal("40.00"),
                    status=PaymentAttemptStatus.cancelled,
                    checkout_id="c-cancelled",
                ),
            ]
        )
        await db.commit()

    body = (
        await client.get("/api/admin/payment-failures?days=7", headers=auth_headers)
    ).json()
    assert body["period_days"] == 7
    assert body["attempts"] == {"pending": 0, "paid": 1, "failed": 2, "cancelled": 1}
    assert body["top_errors"] == [{"error_message": "Terminal injoignable", "count": 2}]
    assert body["exchanges_by_error_type"] == {
        "transport": 0,
        "timeout": 1,
        "http_4xx": 0,
        "http_5xx": 1,
        "decode": 0,
    }
    assert body["by_operation"] == [
        {"operation": "push_to_reader", "count": 2, "errors": 2},
        {"operation": "ping_reader", "count": 1, "errors": 0},
    ]
    # File vide (aucun incident mis en file) : des zeros, jamais une erreur.
    assert body["retries"] == {
        "queued": 0,
        "succeeded": 0,
        "exhausted": 0,
        "abandoned": 0,
    }
