# Tests `app/services/sumup_service.py` — aucune base de données, aucun
# appel réseau réel : tout passe par un `httpx.MockTransport` injecté dans
# `SumUpService._transport`, comme le test équivalent de l'application
# source (`tests/test_sumup_robustness.py`).
from __future__ import annotations

import logging
from decimal import Decimal

import httpx
import pytest

import app.services.sumup_service as svc_mod
from app.services.sumup_service import (
    SumUpService,
    _friendly_error,
    _normalize_txn_status,
    _reader_error_recoverability,
    is_test_api_key,
    redact_sumup_error,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Retry constants monkeypatchées à 0 — aucun vrai sleep pendant les tests."""
    monkeypatch.setattr(svc_mod, "RETRY_WAIT_MULTIPLIER", 0.0)
    monkeypatch.setattr(svc_mod, "RETRY_WAIT_MIN", 0.0)
    monkeypatch.setattr(svc_mod, "RETRY_WAIT_MAX", 0.0)


def _make_service(handler, *, configured: bool = True) -> SumUpService:
    s = SumUpService()
    s.api_key = "sup_sk_live_abc123" if configured else ""
    s.merchant_code = "MTEST" if configured else ""
    s.reader_id = "reader-1" if configured else ""
    s._api_base = "https://api.sumup.com"  # racine — voir SumUpService._url
    s._transport = httpx.MockTransport(handler)
    return s


# ---------------------------------------------------------------------------
# is_configured / is_test_api_key
# ---------------------------------------------------------------------------


def test_is_configured_requires_key_merchant_and_reader():
    s = SumUpService()
    s.api_key, s.merchant_code, s.reader_id = "", "", ""
    assert s.is_configured is False
    s.api_key = "sup_sk_live_x"
    assert s.is_configured is False  # merchant + reader manquants
    s.merchant_code = "M1"
    assert s.is_configured is False  # reader manquant
    s.reader_id = "r1"
    assert s.is_configured is True


def test_is_test_api_key():
    assert is_test_api_key("sup_sk_test_abc123") is True
    assert is_test_api_key("sup_sk_live_abc123") is False
    assert is_test_api_key("") is False


# ---------------------------------------------------------------------------
# SUMUP_API_BASE pilote l'hôte de TOUTES les familles d'appels (v0.1 readers/
# checkouts, v2.1 transactions, v1.0 refunds) — condition nécessaire pour
# rejouer le flux complet (push → poll → PAID → refund) contre un faux
# serveur local en test de bout en bout (persona comptable). Avant ce
# correctif, `_reader_checkout_status`, `refund_transaction` et
# `get_transaction` câblaient `https://api.sumup.com` en dur.
# ---------------------------------------------------------------------------


async def test_all_call_families_target_configured_api_base(monkeypatch):
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        path = request.url.path
        if path.endswith("/status") and "/readers/" in path:
            return httpx.Response(200, json={"data": {"status": "ONLINE"}})
        if path.endswith("/checkout"):
            return httpx.Response(202, json={"data": {"client_transaction_id": "ctid-fake"}})
        if path.endswith("/terminate"):
            return httpx.Response(202)
        if path.endswith("/refunds"):
            return httpx.Response(204)
        if path.endswith("/transactions"):
            return httpx.Response(200, json={"id": "txn-fake", "status": "SUCCESSFUL"})
        if "/readers/" in path:
            return httpx.Response(200, json={"status": "paired", "name": "Solo"})
        return httpx.Response(404, json={})

    monkeypatch.setattr(svc_mod.settings, "SUMUP_API_BASE", "http://fake.local")
    s = SumUpService()  # construit APRÈS le monkeypatch : lit settings dans __init__
    assert s._api_base == "http://fake.local"
    s.api_key = "sup_sk_live_abc123"
    s.merchant_code = "MTEST"
    s.reader_id = "reader-1"
    s._transport = httpx.MockTransport(handler)

    # 1) pré-vol (v0.1 readers/{id} + /status)
    await s.ping_reader()
    # 2) push (v0.1 readers/{id}/checkout)
    await s._push_to_reader(amount=Decimal("5.00"), checkout_id="chk-fake")
    # 3) poll statut (v2.1 transactions)
    status = await s.get_checkout_status("chk-fake", client_transaction_id="ctid-fake")
    assert status["status"] == "PAID"
    # 4) annulation (v0.1 readers/{id}/terminate)
    await s.cancel_checkout("ctid-fake")
    # 5) remboursement (v1.0 payments/{id}/refunds)
    await s.refund_transaction("txn-fake", amount=Decimal("5.00"))
    # lookup transaction (v2.1 transactions, même famille que le poll)
    await s.get_transaction(transaction_id="txn-fake")

    assert len(seen_urls) >= 6
    assert all(url.startswith("http://fake.local/") for url in seen_urls), seen_urls
    assert any(u.startswith("http://fake.local/v0.1/") for u in seen_urls)
    assert any(u.startswith("http://fake.local/v2.1/") for u in seen_urls)
    assert any(u.startswith("http://fake.local/v1.0/") for u in seen_urls)


# ---------------------------------------------------------------------------
# Push reader — succès
# ---------------------------------------------------------------------------


async def test_push_to_reader_success():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(202, json={"data": {"client_transaction_id": "ctid-1"}})

    s = _make_service(handler)
    result = await s._push_to_reader(amount=Decimal("12.50"), checkout_id="chk-1")
    assert result["status"] == "PENDING"
    # Notre identifiant reste la cle de la caisse…
    assert result["checkout_id"] == "chk-1"
    # …et celui de SumUp, lu dans la reponse, est le seul relisable.
    assert result["client_transaction_id"] == "ctid-1"
    assert seen["path"].endswith("/readers/reader-1/checkout")
    assert seen["auth"] == "Bearer sup_sk_live_abc123"


async def test_push_to_reader_sends_amount_in_cents():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(202, json={"data": {}})

    s = _make_service(handler)
    await s._push_to_reader(amount=Decimal("19.99"), checkout_id="chk-2")
    assert captured["body"]["total_amount"]["value"] == 1999
    assert captured["body"]["total_amount"]["currency"] == "EUR"
    # La Readers API n'accepte pas d'identifiant impose : l'envoyer quand
    # meme, c'est se condamner a poller un id que SumUp ignore (PR13).
    assert "client_transaction_id" not in captured["body"]


# ---------------------------------------------------------------------------
# PR13 — l'identifiant relu chez SumUp est CELUI DE SUMUP
#
# Regression de l'incident du 18/09 : le push envoyait notre identifiant et
# le poll le redemandait a la Transactions API, qui ne l'a jamais connu →
# 404 en boucle, caisse bloquee sur « attente retour du TPE » alors que la
# carte etait passee.
# ---------------------------------------------------------------------------


async def test_poll_uses_the_client_transaction_id_returned_by_sumup():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/checkout"):
            return httpx.Response(201, json={"data": {"client_transaction_id": "ctid_X"}})
        seen["query"] = dict(request.url.params)
        # Le faux SumUp se comporte comme le vrai : il ne connait QUE
        # l'identifiant qu'il a emis.
        if request.url.params.get("client_transaction_id") != "ctid_X":
            return httpx.Response(404, json={"error_code": "NOT_FOUND"})
        return httpx.Response(200, json={"id": "txn-9", "status": "SUCCESSFUL", "amount": 5.0})

    s = _make_service(handler)
    push = await s._push_to_reader(amount=Decimal("5.00"), checkout_id="chk-X")
    assert push["client_transaction_id"] == "ctid_X"

    status = await s.get_checkout_status(
        push["checkout_id"], client_transaction_id=push["client_transaction_id"]
    )
    assert seen["query"]["client_transaction_id"] == "ctid_X"
    assert status["status"] == "PAID"
    # La reponse reste libellee avec NOTRE identifiant : c'est la cle de
    # `PaymentAttempt` et celle que la caisse et le journal manipulent.
    assert status["checkout_id"] == "chk-X"


async def test_push_without_client_transaction_id_falls_back_and_warns(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201)  # corps vide : SumUp n'a rien rendu

    s = _make_service(handler)
    with caplog.at_level(logging.WARNING, logger="fripco"):
        result = await s._push_to_reader(amount=Decimal("5.00"), checkout_id="chk-Y")
    assert result["status"] == "PENDING"
    assert result["checkout_id"] == "chk-Y"
    assert result["client_transaction_id"] == "chk-Y"  # repli documente
    assert any("client_transaction_id" in r.getMessage() for r in caplog.records)


async def test_push_records_the_sumup_identifier_in_the_exchange_log():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"data": {"client_transaction_id": "ctid_Z"}})

    s = _make_service(handler)
    await s._push_to_reader(amount=Decimal("5.00"), checkout_id="chk-Z")
    record = s.exchanges[-1]
    assert record.checkout_id == "chk-Z"
    assert record.client_transaction_id == "ctid_Z"


async def test_get_checkout_status_without_sumup_identifier_queries_our_own():
    """Essai anterieur au correctif : on n'a que notre identifiant. Il part
    tel quel (SumUp repondra 404 → PENDING, cf. §4 du contrat)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(404, json={})

    s = _make_service(handler)
    result = await s.get_checkout_status("chk-legacy")
    assert seen["query"]["client_transaction_id"] == "chk-legacy"
    assert result["status"] == "PENDING"
    assert result["checkout_id"] == "chk-legacy"


# ---------------------------------------------------------------------------
# Push reader — échecs récupérables / définitifs
# ---------------------------------------------------------------------------


async def test_push_to_reader_busy_is_recoverable_with_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error_code": "READER_BUSY"})

    s = _make_service(handler)
    result = await s._push_to_reader(amount=Decimal("10.00"), checkout_id="chk-3")
    assert result["status"] == "FAILED"
    assert result["recoverable"] is True
    assert result["retry_after"] == 5
    assert result["error_code"] == "READER_BUSY"


async def test_push_to_reader_offline_is_recoverable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error_code": "READER_OFFLINE"})

    s = _make_service(handler)
    result = await s._push_to_reader(amount=Decimal("10.00"), checkout_id="chk-4")
    assert result["recoverable"] is True
    assert result["error_code"] == "READER_OFFLINE"


async def test_push_to_reader_bad_key_is_definitive():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error_code": "UNAUTHORIZED"})

    s = _make_service(handler)
    result = await s._push_to_reader(amount=Decimal("10.00"), checkout_id="chk-5")
    assert result["status"] == "FAILED"
    assert result["recoverable"] is False


async def test_push_to_reader_network_error_is_recoverable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no net", request=request)

    s = _make_service(handler)
    result = await s._push_to_reader(amount=Decimal("10.00"), checkout_id="chk-6")
    assert result["status"] == "FAILED"
    assert result["recoverable"] is True


# ---------------------------------------------------------------------------
# ping_reader — pré-vol
# ---------------------------------------------------------------------------


async def test_ping_reader_unconfigured():
    s = _make_service(lambda r: httpx.Response(200, json={}), configured=False)
    result = await s.ping_reader()
    assert result == {
        "configured": False, "paired": False, "online": False, "ready": False,
        "status": "unconfigured",
        "message": "Non configuré (SUMUP_API_KEY, SUMUP_MERCHANT_CODE, SUMUP_READER_ID manquant(s))",
    }


async def test_ping_reader_offline():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"data": {"status": "OFFLINE"}})
        return httpx.Response(200, json={"status": "paired", "name": "Solo"})

    s = _make_service(handler)
    result = await s.ping_reader()
    assert result["configured"] is True
    assert result["paired"] is True
    assert result["online"] is False
    assert result["ready"] is False


async def test_ping_reader_online():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"data": {"status": "ONLINE", "battery_level": 80}})
        return httpx.Response(200, json={"status": "paired", "name": "Solo"})

    s = _make_service(handler)
    result = await s.ping_reader()
    assert result["online"] is True
    assert result["ready"] is True
    assert result["battery_level"] == 80


async def test_ping_reader_not_paired():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "expired"})

    s = _make_service(handler)
    result = await s.ping_reader()
    assert result["paired"] is False
    assert result["ready"] is False
    assert "expiré" in result["message"]


# ---------------------------------------------------------------------------
# get_checkout_status / _reader_checkout_status
# ---------------------------------------------------------------------------


async def test_get_checkout_status_pending_when_not_tapped_yet():
    s = _make_service(lambda r: httpx.Response(404, json={}))
    result = await s.get_checkout_status("ctid-7")
    assert result["status"] == "PENDING"


async def test_get_checkout_status_paid():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "txn-1",
                "status": "SUCCESSFUL",
                "transaction_code": "TC1",
                "auth_code": "AUTH1",
                "amount": 12.5,
                "currency": "EUR",
                "card": {"type": "visa", "last_4_digits": "4242"},
            },
        )

    s = _make_service(handler)
    result = await s.get_checkout_status("ctid-8")
    assert result["status"] == "PAID"
    assert result["sumup_transaction_id"] == "txn-1"
    assert result["sumup_transaction_code"] == "TC1"
    assert result["sumup_auth_code"] == "AUTH1"
    assert result["sumup_card_brand"] == "visa"
    assert result["sumup_card_last4"] == "4242"
    assert result["amount"] == 12.5


async def test_get_checkout_status_unconfigured():
    s = _make_service(lambda r: httpx.Response(200, json={}), configured=False)
    result = await s.get_checkout_status("ctid-9")
    assert result["status"] == "FAILED"


# ---------------------------------------------------------------------------
# cancel_checkout / terminate_reader_checkout
# ---------------------------------------------------------------------------


async def test_cancel_checkout_terminated():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/terminate")
        return httpx.Response(202)

    s = _make_service(handler)
    ok = await s.cancel_checkout("ctid-10")
    assert ok is True


async def test_cancel_checkout_not_waiting():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422)

    s = _make_service(handler)
    ok = await s.cancel_checkout("ctid-11")
    assert ok is False


async def test_terminate_reader_checkout_unconfigured():
    s = _make_service(lambda r: httpx.Response(202), configured=False)
    result = await s.terminate_reader_checkout()
    assert result["ok"] is False
    assert result["status"] == "unconfigured"


# ---------------------------------------------------------------------------
# refund_transaction
# ---------------------------------------------------------------------------


async def test_refund_transaction_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/refunds" in request.url.path
        return httpx.Response(204)

    s = _make_service(handler)
    result = await s.refund_transaction("txn-1", amount=Decimal("10.00"))
    assert result["ok"] is True
    assert result["status"] == "refunded"


async def test_refund_transaction_failure_maps_friendly_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error_code": "CONFLICT", "message": "already refunded"})

    s = _make_service(handler)
    result = await s.refund_transaction("txn-1")
    assert result["ok"] is False
    assert "remboursable" in result["message"]


async def test_refund_transaction_unconfigured():
    s = _make_service(lambda r: httpx.Response(204), configured=False)
    result = await s.refund_transaction("txn-1")
    assert result["ok"] is False
    assert result["status"] == "unconfigured"


# ---------------------------------------------------------------------------
# get_transaction
# ---------------------------------------------------------------------------


async def test_get_transaction_by_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("id") == "txn-42"
        return httpx.Response(200, json={"id": "txn-42", "auth_code": "AUTH42"})

    s = _make_service(handler)
    txn = await s.get_transaction(transaction_id="txn-42")
    assert txn == {"id": "txn-42", "auth_code": "AUTH42"}


async def test_get_transaction_no_identifier_returns_none():
    s = _make_service(lambda r: httpx.Response(200, json={}))
    assert await s.get_transaction() is None


# ---------------------------------------------------------------------------
# Retry avec backoff (constantes monkeypatchées à 0, pas de vrai sleep)
# ---------------------------------------------------------------------------


async def test_send_retries_transient_connect_error_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    s = _make_service(handler)
    async with s._client(5) as client:
        resp = await s._send(client, "get_status", "GET", "https://api.sumup.com/v0.1/x")
    assert resp.status_code == 200
    assert calls["n"] == 3


async def test_post_read_timeout_is_not_retried():
    """Un POST susceptible d'avoir déjà atteint le TPE n'est jamais rejoué."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow", request=request)

    s = _make_service(handler)
    with pytest.raises(httpx.ReadTimeout):
        async with s._client(5) as client:
            await s._send(client, "reader_push", "POST", "https://api.sumup.com/v0.1/x")
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Normalisation de statut / erreurs récupérables
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("PAID", "PAID"),
        ("SUCCESSFUL", "PAID"),
        ("FAILED", "FAILED"),
        ("EXPIRED", "FAILED"),
        ("CANCELLED", "CANCELLED"),
        ("CANCELED", "CANCELLED"),
        ("PENDING", "PENDING"),
        (None, "PENDING"),
        ("WHATEVER", "PENDING"),
    ],
)
def test_normalize_txn_status(raw, expected):
    assert _normalize_txn_status(raw) == expected


def test_reader_error_recoverability():
    assert _reader_error_recoverability(422, "READER_BUSY") == (True, 5)
    assert _reader_error_recoverability(422, "READER_OFFLINE") == (True, None)
    assert _reader_error_recoverability(401, "UNAUTHORIZED") == (False, None)
    assert _reader_error_recoverability(503, None) == (True, None)


def test_friendly_error_prefers_sumup_error_code():
    msg = _friendly_error(422, '{"error_code": "READER_BUSY"}')
    assert "terminal" in msg.lower()


# ---------------------------------------------------------------------------
# describe() — jamais de secret en clair
# ---------------------------------------------------------------------------


def test_describe_masks_merchant_and_reader():
    s = SumUpService()
    s.api_key = "sup_sk_live_verysecretvalue"
    s.merchant_code = "MABCDEFGH"
    s.reader_id = "reader-abcdefgh"
    d = s.describe()
    assert d["configured"] is True
    assert "verysecretvalue" not in str(d)
    assert d["merchant_code_masked"] != s.merchant_code
    assert d["reader_id_masked"] != s.reader_id


# ---------------------------------------------------------------------------
# redact_sumup_error — PCI-DSS req. 3 / RGPD
# ---------------------------------------------------------------------------


def test_redact_visa_pan():
    out = redact_sumup_error("Card declined: 4111111111111111 invalid")
    assert "4111111111111111" not in out
    assert "<PAN_REDACTED>" in out


def test_redact_pan_with_spaces_and_dashes():
    assert "4111 1111" not in redact_sumup_error("card 4111 1111 1111 1111 invalid")
    assert "4111-1111" not in redact_sumup_error("card 4111-1111-1111-1111 expired")


def test_redact_cvv():
    out = redact_sumup_error("cvv: 1234 rejected")
    assert "1234" not in out
    assert "<CVV_REDACTED>" in out


def test_redact_bearer_token():
    out = redact_sumup_error("Authorization: Bearer sup_sk_live_abcdefghijklmnop")
    assert "abcdefghijklmnop" not in out


def test_redact_api_key():
    out = redact_sumup_error("key=sup_sk_live_abcdefghijklmnopqrstuvwx leaked")
    assert "abcdefghijklmnopqrstuvwx" not in out
    assert "<API_KEY_REDACTED>" in out


def test_redact_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    out = redact_sumup_error(f"token={jwt}")
    assert jwt not in out
    assert "<JWT_REDACTED>" in out


def test_keep_short_numbers_intact():
    out = redact_sumup_error("HTTP 422 amount=12.50 attempt=3")
    assert "422" in out
    assert "12.50" in out


async def test_never_logs_api_key_in_send_error(caplog):
    """Le journal du service ne contient jamais la clé API en clair."""
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Authorization: Bearer sup_sk_live_verysecretvalue123456")

    s = _make_service(handler)
    with caplog.at_level(logging.WARNING, logger="fripco"):
        async with s._client(5) as client:
            await s._send(client, "ping", "GET", "https://api.sumup.com/v0.1/x")

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "sup_sk_live_verysecretvalue123456" not in log_text
    assert s.api_key not in log_text
