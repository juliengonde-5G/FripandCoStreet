# Tests de la file des paiements carte échoués (PR9, docs/ARCHITECTURE_PR9.md,
# contrat K3) : mise en file d'une cause récupérable, 409 enrichie, réessai
# manuel, épuisement, abandon, et surtout la règle PR2 qui ne bouge pas —
# aucune vente n'est créée tant que le paiement n'est pas `paid`.
#
# La couche HTTP SumUp est mockée via `SumUpService._transport`
# (`httpx.MockTransport`), comme dans `test_cb_router.py` : on exerce le vrai
# chemin réseau du service (retries transport compris), pas une doublure de
# la logique métier.
from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

import app.services.sumup_service as svc_mod
from app.api.pos import cb_router as cb_router_mod
from app.core.database import async_session
from app.models.failed_payment import FailedPayment, FailedPaymentStatus
from app.models.jet import JournalEvent
from app.models.payment_attempt import PaymentAttempt, PaymentAttemptStatus
from app.models.pos import Transaction
from app.services import failed_payment_service
from app.services.jet import (
    EVENT_PAYMENT_ABANDONED,
    EVENT_PAYMENT_FAILED_QUEUED,
    EVENT_PAYMENT_RETRIES_EXHAUSTED,
    EVENT_PAYMENT_RETRY_STARTED,
    EVENT_PAYMENT_RETRY_SUCCEEDED,
)
from app.services.sumup_service import SumUpService

pytestmark = pytest.mark.anyio

# Clés autorisées dans les payloads JET de la file (K3) : IDENTIFIANTS,
# MONTANTS, et le motif d'abandon. Aucun nom, aucun e-mail, aucun message
# d'erreur brut — le JET est immuable, ce qui y tombe n'en sort plus.
ALLOWED_JET_KEYS = {
    "failed_payment_id",
    "attempt_id",
    "checkout_id",
    "transaction_id",
    "amount",
    "error_type",
    "retry_count",
    "reason",
}


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


def _configure_sumup(monkeypatch, handler) -> None:
    """Chaque `SumUpService()` créé par un endpoint est configuré et routé
    vers `handler` (le service est instancié à la demande, pas injecté)."""
    original_init = SumUpService.__init__

    def patched_init(self):
        original_init(self)
        self.api_key = "sup_sk_live_abc123"
        self.merchant_code = "MTEST"
        self.reader_id = "reader-1"
        self._transport = httpx.MockTransport(handler)

    monkeypatch.setattr(SumUpService, "__init__", patched_init)


def _handler(state: dict):
    """Faux SumUp piloté par `state`, mutable d'une étape du test à l'autre.

    - `state["push"]` : réponse du push reader (None = accepté 202, une
      exception = le réseau lâche avant qu'on lise la réponse) ;
    - `state["paid"]` : la carte a été tapée → la Transactions API répond ;
    - `state["txn_status"]` : statut renvoyé par la Transactions API quand
      on la relit sans que `paid` soit posé (« FAILED », par exemple).

    Compte aussi les appels (`pushes`, `terminates`, `lookups`) : la
    question « a-t-on présenté le montant une seconde fois ? » se vérifie
    au nombre d'envois, pas au discours du service.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/status") and "/readers/" in path:
            return httpx.Response(200, json={"data": {"status": "ONLINE"}})
        if path.endswith("/checkout"):
            state["pushes"] = state.get("pushes", 0) + 1
            push = state.get("push")
            if isinstance(push, Exception):
                raise push
            return push if push is not None else httpx.Response(202, json={"data": {}})
        if path.endswith("/terminate"):
            state["terminates"] = state.get("terminates", 0) + 1
            return httpx.Response(202, json={})
        if "/readers/" in path:
            return httpx.Response(200, json={"status": "paired", "name": "Solo"})
        if path.endswith("/transactions"):
            state["lookups"] = state.get("lookups", 0) + 1
            if not state.get("paid"):
                if state.get("txn_status"):
                    return httpx.Response(
                        200, json={"id": "txn-0", "status": state["txn_status"]}
                    )
                return httpx.Response(404)  # carte pas encore tapée
            return httpx.Response(
                200,
                json={
                    "id": "txn-1",
                    "status": "SUCCESSFUL",
                    "transaction_code": "TC1",
                    "auth_code": "AUTH1",
                    "amount": state.get("amount", 12.50),
                    "currency": "EUR",
                    "card": {"type": "VISA", "last_4_digits": "4242"},
                },
            )
        return httpx.Response(404, json={})

    return handler


async def _events(event_type: str) -> list[JournalEvent]:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(JournalEvent)
                .where(JournalEvent.event_type == event_type)
                .order_by(JournalEvent.seq.asc())
            )
        ).scalars().all()
        return [row for row in rows]


async def _queue() -> list[FailedPayment]:
    async with async_session() as db:
        rows = (
            await db.execute(select(FailedPayment).order_by(FailedPayment.created_at.asc()))
        ).scalars().all()
        return [row for row in rows]


async def _only_queued() -> FailedPayment:
    rows = await _queue()
    assert len(rows) == 1, f"attendu 1 ligne en file, trouvé {len(rows)}"
    return rows[0]


async def _attempts(client_uuid: str) -> list[PaymentAttempt]:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(PaymentAttempt)
                .where(PaymentAttempt.client_uuid == uuid.UUID(client_uuid))
                .order_by(PaymentAttempt.attempt_count.asc())
            )
        ).scalars().all()
        return [row for row in rows]


async def _initiate_failure(client, auth_headers, state, *, amount: str = "12.50") -> tuple[str, dict]:
    """Lance un paiement qui échoue et retourne (client_uuid, corps de la 409)."""
    client_uuid = str(uuid.uuid4())
    resp = await client.post(
        "/api/pos/payments/cb/initiate",
        json={"amount": amount, "client_uuid": client_uuid},
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    return client_uuid, resp.json()


# ---------------------------------------------------------------------------
# Mise en file
# ---------------------------------------------------------------------------


async def test_recoverable_failure_queues_and_enriches_409(client, auth_headers, monkeypatch):
    # 503 SumUp : le terminal n'a rien vu passer, c'est rattrapable.
    state = {"push": httpx.Response(503, json={"error_code": "INTERNAL_ERROR"})}
    _configure_sumup(monkeypatch, _handler(state))

    client_uuid, body = await _initiate_failure(client, auth_headers, state)

    assert body["code"] == "payment_failed"
    assert body["recoverable"] is True
    assert body["failed_payment_id"]

    queued = await _only_queued()
    assert str(queued.id) == body["failed_payment_id"]
    assert queued.status == FailedPaymentStatus.pending
    assert queued.error_type == "http_5xx"
    assert queued.amount == Decimal("12.50")
    assert queued.retry_count == 0
    assert queued.max_retries == 3
    assert str(queued.client_uuid) == client_uuid
    assert queued.transaction_id is None

    events = await _events(EVENT_PAYMENT_FAILED_QUEUED)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["failed_payment_id"] == body["failed_payment_id"]
    assert payload["checkout_id"] == client_uuid
    assert payload["amount"] == "12.50"
    assert payload["error_type"] == "http_5xx"
    assert set(payload) <= ALLOWED_JET_KEYS


async def test_transport_failure_is_queued_as_transport(client, auth_headers, monkeypatch):
    # Le TPE n'est même pas joint (Wi-Fi coupé) : httpx lève, le service
    # renvoie un échec récupérable sans statut HTTP.
    state = {"push": httpx.ConnectError("réseau injoignable")}
    _configure_sumup(monkeypatch, _handler(state))

    _client_uuid, body = await _initiate_failure(client, auth_headers, state)

    assert body["recoverable"] is True
    queued = await _only_queued()
    assert queued.error_type == "transport"
    assert queued.last_error


async def test_declined_card_is_not_queued(client, auth_headers, monkeypatch):
    # 400 sans code récupérable = refus définitif : la cliente change de
    # carte, il n'y a rien à rejouer.
    state = {"push": httpx.Response(400, json={"error_code": "DECLINED"})}
    _configure_sumup(monkeypatch, _handler(state))

    _client_uuid, body = await _initiate_failure(client, auth_headers, state)

    assert body["code"] == "payment_failed"
    assert body["recoverable"] is False
    assert body["failed_payment_id"] is None
    assert await _queue() == []
    assert await _events(EVENT_PAYMENT_FAILED_QUEUED) == []


async def test_409_bodies_carry_queue_fields_at_the_root(client, auth_headers, monkeypatch):
    """Forme exacte des 409 CB : la caisse lit tout à la racine du corps.

    Même parti pris que le 429 du code PIN (`retry_after` à la racine) —
    rien n'est imbriqué sous `detail`, qui reste la phrase française
    affichée telle quelle.
    """
    state = {"push": httpx.Response(503, json={"error_code": "INTERNAL_ERROR"})}
    _configure_sumup(monkeypatch, _handler(state))
    _client_uuid, body = await _initiate_failure(client, auth_headers, state)
    failed_payment_id = body["failed_payment_id"]

    assert isinstance(body["detail"], str)
    assert body["code"] == "payment_failed"
    assert body["recoverable"] is True
    assert body["retry_count"] == 0
    assert body["max_retries"] == 3

    # Un refus de carte garde la MÊME forme : seules les valeurs changent.
    state["push"] = httpx.Response(400, json={"error_code": "DECLINED"})
    _other_uuid, declined = await _initiate_failure(client, auth_headers, state)
    assert set(declined) == set(body)
    assert declined["recoverable"] is False
    assert declined["failed_payment_id"] is None
    assert declined["retry_count"] is None
    assert declined["max_retries"] is None

    # Réessais épuisés : mêmes champs de file à la racine.
    state["push"] = httpx.Response(503, json={"error_code": "INTERNAL_ERROR"})
    for _ in range(3):
        resp = await client.post(
            f"/api/pos/payments/cb/retry-failed/{failed_payment_id}", headers=auth_headers
        )
        assert resp.status_code == 409
        assert resp.json()["max_retries"] == 3

    exhausted = await client.post(
        f"/api/pos/payments/cb/retry-failed/{failed_payment_id}", headers=auth_headers
    )
    assert exhausted.status_code == 409
    assert exhausted.json() == {
        "detail": "Réessais épuisés — choisissez un autre moyen de paiement.",
        "code": "retries_exhausted",
        "failed_payment_id": failed_payment_id,
        "retry_count": 3,
        "max_retries": 3,
    }


# ---------------------------------------------------------------------------
# Réessai manuel
# ---------------------------------------------------------------------------


async def test_retry_failed_creates_new_checkout_and_increments(
    client, auth_headers, monkeypatch
):
    state = {"push": httpx.Response(503, json={"error_code": "INTERNAL_ERROR"})}
    _configure_sumup(monkeypatch, _handler(state))
    client_uuid, body = await _initiate_failure(client, auth_headers, state)

    # Le terminal répond de nouveau : le réessai pousse un NOUVEAU checkout.
    state["push"] = None
    resp = await client.post(
        f"/api/pos/payments/cb/retry-failed/{body['failed_payment_id']}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    retried = resp.json()
    assert retried["status"] == "pending"
    assert retried["checkout_id"] == f"{client_uuid}:r2"
    assert retried["failed_payment_id"] == body["failed_payment_id"]
    assert retried["retry_count"] == 1
    assert retried["failed_payment"]["retry_count"] == 1
    assert retried["failed_payment"]["status"] == "pending"

    attempts = await _attempts(client_uuid)
    assert [a.attempt_count for a in attempts] == [1, 2]
    assert attempts[0].status == PaymentAttemptStatus.failed
    assert attempts[1].status == PaymentAttemptStatus.pending

    # Une seule ligne en file : un réessai fait avancer un compteur, il
    # n'empile pas les incidents.
    queued = await _only_queued()
    assert queued.retry_count == 1

    started = await _events(EVENT_PAYMENT_RETRY_STARTED)
    assert len(started) == 1
    assert started[0].payload["retry_count"] == 1
    assert started[0].payload["checkout_id"] == f"{client_uuid}:r2"
    assert set(started[0].payload) <= ALLOWED_JET_KEYS

    # Toujours aucune vente : le paiement n'est que `pending`.
    async with async_session() as db:
        assert (await db.execute(select(Transaction))).scalars().all() == []


async def test_retry_failed_requires_auth(client, auth_headers, monkeypatch):
    state = {"push": httpx.Response(503, json={"error_code": "INTERNAL_ERROR"})}
    _configure_sumup(monkeypatch, _handler(state))
    _client_uuid, body = await _initiate_failure(client, auth_headers, state)

    resp = await client.post(
        f"/api/pos/payments/cb/retry-failed/{body['failed_payment_id']}"
    )
    assert resp.status_code == 401


async def test_retry_failed_unknown_id_returns_404(client, auth_headers, monkeypatch):
    _configure_sumup(monkeypatch, _handler({}))
    resp = await client.post(
        f"/api/pos/payments/cb/retry-failed/{uuid.uuid4()}", headers=auth_headers
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


async def test_fourth_retry_is_refused_and_queue_is_exhausted(
    client, auth_headers, monkeypatch
):
    state = {"push": httpx.Response(503, json={"error_code": "INTERNAL_ERROR"})}
    _configure_sumup(monkeypatch, _handler(state))
    _client_uuid, body = await _initiate_failure(client, auth_headers, state)
    failed_payment_id = body["failed_payment_id"]

    # 3 réessais offerts, tous en échec.
    for expected_retry in (1, 2, 3):
        resp = await client.post(
            f"/api/pos/payments/cb/retry-failed/{failed_payment_id}", headers=auth_headers
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "payment_failed"
        assert resp.json()["retry_count"] == expected_retry
        assert resp.json()["failed_payment"]["retry_count"] == expected_retry

    queued = await _only_queued()
    assert queued.status == FailedPaymentStatus.exhausted
    assert queued.retry_count == 3
    assert queued.resolved_at is not None

    exhausted = await _events(EVENT_PAYMENT_RETRIES_EXHAUSTED)
    assert len(exhausted) == 1
    assert set(exhausted[0].payload) <= ALLOWED_JET_KEYS

    # Le 4ᵉ appel est refusé net.
    resp = await client.post(
        f"/api/pos/payments/cb/retry-failed/{failed_payment_id}", headers=auth_headers
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "retries_exhausted"

    # Toujours aucune vente écrite.
    async with async_session() as db:
        assert (await db.execute(select(Transaction))).scalars().all() == []


# ---------------------------------------------------------------------------
# Réconciliation avant un nouveau push — anti double débit
#
# Le cas redouté : l'envoi au terminal ARRIVE chez SumUp, mais la réponse se
# perd. On croit l'essai raté alors que la cliente a peut-être déjà payé.
# Repousser à l'aveugle, c'est la débiter deux fois.
# ---------------------------------------------------------------------------


async def test_ambiguous_timeout_then_original_paid_does_not_push_again(
    client, auth_headers, monkeypatch
):
    state = {"push": httpx.ReadTimeout("la réponse ne revient pas")}
    _configure_sumup(monkeypatch, _handler(state))
    client_uuid, body = await _initiate_failure(client, auth_headers, state)
    assert state["pushes"] == 1
    queued = await _only_queued()
    assert queued.error_type == "timeout"

    # En réalité le terminal avait encaissé : la Transactions API le dit.
    state["paid"] = True
    state["push"] = None  # un push réussirait — il ne doit pas avoir lieu
    resp = await client.post(
        f"/api/pos/payments/cb/retry-failed/{body['failed_payment_id']}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "paid"
    assert payload["reconciled"] is True
    # Le checkout rendu est celui d'ORIGINE : c'est lui qui porte l'argent.
    assert payload["checkout_id"] == client_uuid
    assert payload["retry_count"] == 0
    assert payload["last4"] == "4242"

    # Aucun second envoi au terminal.
    assert state["pushes"] == 1
    assert state.get("terminates", 0) == 0

    attempts = await _attempts(client_uuid)
    assert len(attempts) == 1
    assert attempts[0].status == PaymentAttemptStatus.paid
    assert attempts[0].sumup_transaction_code == "TC1"

    queued = await _only_queued()
    assert queued.status == FailedPaymentStatus.succeeded
    assert queued.retry_count == 0
    assert queued.resolved_at is not None

    assert len(await _events(EVENT_PAYMENT_RETRY_SUCCEEDED)) == 1
    # Rien n'a été relancé : pas d'événement de réessai.
    assert await _events(EVENT_PAYMENT_RETRY_STARTED) == []
    assert len(await _events("payment.cb_paid")) == 1

    # Et la vente s'écrit sur ce checkout d'origine, sans second débit.
    async with async_session() as db:
        assert (await db.execute(select(Transaction))).scalars().all() == []


async def test_ambiguous_timeout_then_original_still_active_terminates_then_pushes(
    client, auth_headers, monkeypatch
):
    state = {"push": httpx.ReadTimeout("la réponse ne revient pas")}
    _configure_sumup(monkeypatch, _handler(state))
    client_uuid, body = await _initiate_failure(client, auth_headers, state)

    # Le terminal peut encore afficher le montant (SumUp ne connaît pas
    # encore de transaction) : on coupe son écran avant de repousser.
    state["push"] = None
    resp = await client.post(
        f"/api/pos/payments/cb/retry-failed/{body['failed_payment_id']}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert resp.json()["checkout_id"] == f"{client_uuid}:r2"

    assert state["lookups"] >= 1
    assert state["terminates"] == 1
    assert state["pushes"] == 2

    queued = await _only_queued()
    assert queued.status == FailedPaymentStatus.pending
    assert queued.retry_count == 1


async def test_ambiguous_timeout_then_original_failed_pushes_without_terminate(
    client, auth_headers, monkeypatch
):
    state = {"push": httpx.ReadTimeout("la réponse ne revient pas")}
    _configure_sumup(monkeypatch, _handler(state))
    _client_uuid, body = await _initiate_failure(client, auth_headers, state)

    # SumUp répond cette fois : l'essai d'origine a bien échoué, rien
    # n'attend sur le terminal — on repousse directement.
    state["txn_status"] = "FAILED"
    state["push"] = None
    resp = await client.post(
        f"/api/pos/payments/cb/retry-failed/{body['failed_payment_id']}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert state["lookups"] >= 1
    assert state.get("terminates", 0) == 0
    assert state["pushes"] == 2


async def test_unambiguous_failure_skips_reconciliation(client, auth_headers, monkeypatch):
    # 503 : SumUp a RÉPONDU non, aucun paiement n'a pu démarrer. Inutile
    # d'aller relire quoi que ce soit avant de repousser.
    state = {"push": httpx.Response(503, json={"error_code": "INTERNAL_ERROR"})}
    _configure_sumup(monkeypatch, _handler(state))
    _client_uuid, body = await _initiate_failure(client, auth_headers, state)

    state["push"] = None
    resp = await client.post(
        f"/api/pos/payments/cb/retry-failed/{body['failed_payment_id']}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert state.get("lookups", 0) == 0
    assert state.get("terminates", 0) == 0
    assert state["pushes"] == 2


async def test_historical_retry_also_reconciles_before_pushing(
    client, auth_headers, monkeypatch
):
    """Le réessai historique partage la mécanique : même garde-fou."""
    state = {"push": httpx.ReadTimeout("la réponse ne revient pas")}
    _configure_sumup(monkeypatch, _handler(state))
    client_uuid, _body = await _initiate_failure(client, auth_headers, state)

    state["paid"] = True
    state["push"] = None
    resp = await client.post(
        f"/api/pos/payments/cb/{client_uuid}/retry", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "paid"
    assert resp.json()["reconciled"] is True
    assert resp.json()["checkout_id"] == client_uuid
    assert state["pushes"] == 1

    queued = await _only_queued()
    assert queued.status == FailedPaymentStatus.succeeded


# ---------------------------------------------------------------------------
# Résolution : polling `paid`, puis vente
# ---------------------------------------------------------------------------


async def test_paid_then_sale_closes_queue_and_links_transaction(
    client, auth_headers, open_drawer, monkeypatch
):
    state = {"push": httpx.Response(503, json={"error_code": "INTERNAL_ERROR"})}
    _configure_sumup(monkeypatch, _handler(state))
    client_uuid, body = await _initiate_failure(client, auth_headers, state)
    failed_payment_id = body["failed_payment_id"]

    state["push"] = None
    retry_resp = await client.post(
        f"/api/pos/payments/cb/retry-failed/{failed_payment_id}", headers=auth_headers
    )
    assert retry_resp.status_code == 200
    checkout_id = retry_resp.json()["checkout_id"]

    # La cliente tape sa carte : le polling de la caisse constate `paid`.
    state["paid"] = True
    poll = await client.get(
        f"/api/pos/payments/cb/{checkout_id}/status", headers=auth_headers
    )
    assert poll.status_code == 200, poll.text
    assert poll.json()["status"] == "paid"

    queued = await _only_queued()
    assert queued.status == FailedPaymentStatus.succeeded
    assert queued.resolved_at is not None
    # La vente n'existe pas encore : la file se referme, mais rien ne la
    # rattache tant que la caisse n'a pas validé le panier (règle PR2).
    assert queued.transaction_id is None
    async with async_session() as db:
        assert (await db.execute(select(Transaction))).scalars().all() == []

    succeeded = await _events(EVENT_PAYMENT_RETRY_SUCCEEDED)
    assert len(succeeded) == 1
    assert succeeded[0].payload["failed_payment_id"] == failed_payment_id
    assert set(succeeded[0].payload) <= ALLOWED_JET_KEYS

    # Puis la vente est écrite, avec vérification serveur du paiement.
    sale = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": client_uuid,
            "items": [{"label": "Robe", "unit_price": "12.50", "quantity": 1}],
            "payments": [
                {"method": "card", "amount": "12.50", "checkout_id": checkout_id}
            ],
        },
        headers=auth_headers,
    )
    assert sale.status_code == 201, sale.text
    transaction_id = sale.json()["id"]

    queued = await _only_queued()
    assert queued.status == FailedPaymentStatus.succeeded
    assert str(queued.transaction_id) == transaction_id
    # Pas de second événement : la résolution n'est journalisée qu'une fois.
    assert len(await _events(EVENT_PAYMENT_RETRY_SUCCEEDED)) == 1


async def test_no_sale_is_written_while_payment_is_not_paid(
    client, auth_headers, open_drawer, monkeypatch
):
    state = {"push": httpx.Response(503, json={"error_code": "INTERNAL_ERROR"})}
    _configure_sumup(monkeypatch, _handler(state))
    client_uuid, _body = await _initiate_failure(client, auth_headers, state)

    sale = await client.post(
        "/api/pos/transactions",
        json={
            "client_uuid": client_uuid,
            "items": [{"label": "Robe", "unit_price": "12.50", "quantity": 1}],
            "payments": [
                {"method": "card", "amount": "12.50", "checkout_id": client_uuid}
            ],
        },
        headers=auth_headers,
    )
    assert sale.status_code == 409
    assert sale.json()["code"] == "card_not_confirmed"

    async with async_session() as db:
        assert (await db.execute(select(Transaction))).scalars().all() == []
    queued = await _only_queued()
    assert queued.status == FailedPaymentStatus.pending
    assert queued.transaction_id is None


# ---------------------------------------------------------------------------
# Routes admin
# ---------------------------------------------------------------------------


async def test_admin_list_requires_auth(client):
    assert (await client.get("/api/admin/failed-payments")).status_code == 401
    assert (
        await client.post(
            f"/api/admin/failed-payments/{uuid.uuid4()}/abandon", json={"reason": "x"}
        )
    ).status_code == 401


async def test_admin_list_filters_by_status(client, auth_headers, monkeypatch):
    state = {"push": httpx.Response(503, json={"error_code": "INTERNAL_ERROR"})}
    _configure_sumup(monkeypatch, _handler(state))
    client_uuid, body = await _initiate_failure(client, auth_headers, state)

    listed = await client.get(
        "/api/admin/failed-payments?status=pending", headers=auth_headers
    )
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["total"] == 1
    row = payload["failed_payments"][0]
    assert row["id"] == body["failed_payment_id"]
    assert row["status"] == "pending"
    assert row["amount"] == 12.50
    assert row["checkout_id"] == client_uuid
    assert row["max_retries"] == 3

    empty = await client.get(
        "/api/admin/failed-payments?status=abandoned", headers=auth_headers
    )
    assert empty.json() == {"failed_payments": [], "total": 0}

    unknown = await client.get(
        "/api/admin/failed-payments?status=nimportequoi", headers=auth_headers
    )
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "invalid_status"


async def test_admin_abandon_closes_the_line_and_journals_the_reason(
    client, auth_headers, monkeypatch
):
    state = {"push": httpx.Response(503, json={"error_code": "INTERNAL_ERROR"})}
    _configure_sumup(monkeypatch, _handler(state))
    _client_uuid, body = await _initiate_failure(client, auth_headers, state)
    failed_payment_id = body["failed_payment_id"]

    resp = await client.post(
        f"/api/admin/failed-payments/{failed_payment_id}/abandon",
        json={"reason": "encaissé en espèces"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    # La ligne mise à jour est renvoyée telle quelle (pas d'enveloppe).
    assert resp.json()["status"] == "abandoned"
    assert resp.json()["id"] == failed_payment_id

    queued = await _only_queued()
    assert queued.status == FailedPaymentStatus.abandoned
    assert queued.resolved_at is not None

    events = await _events(EVENT_PAYMENT_ABANDONED)
    assert len(events) == 1
    assert events[0].payload["reason"] == "encaissé en espèces"
    assert set(events[0].payload) <= ALLOWED_JET_KEYS

    # Deux fois, non : la ligne est déjà fermée.
    again = await client.post(
        f"/api/admin/failed-payments/{failed_payment_id}/abandon",
        json={"reason": "encore"},
        headers=auth_headers,
    )
    assert again.status_code == 409
    assert again.json()["code"] == "already_abandoned"

    # Et on ne réessaie pas une ligne abandonnée.
    retry = await client.post(
        f"/api/pos/payments/cb/retry-failed/{failed_payment_id}", headers=auth_headers
    )
    assert retry.status_code == 409
    assert retry.json()["code"] == "not_retryable"


async def test_admin_abandon_requires_a_reason(client, auth_headers, monkeypatch):
    state = {"push": httpx.Response(503, json={"error_code": "INTERNAL_ERROR"})}
    _configure_sumup(monkeypatch, _handler(state))
    _client_uuid, body = await _initiate_failure(client, auth_headers, state)

    resp = await client.post(
        f"/api/admin/failed-payments/{body['failed_payment_id']}/abandon",
        json={"reason": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_admin_abandon_unknown_id_returns_404(client, auth_headers):
    resp = await client.post(
        f"/api/admin/failed-payments/{uuid.uuid4()}/abandon",
        json={"reason": "peu importe"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Classification (unitaire) — la frontière entre « rattrapable » et « refusé »
# ---------------------------------------------------------------------------


def test_classify_push_result():
    classify = failed_payment_service.classify_push_result
    assert classify({"recoverable": True, "error_type": "ConnectError"}) == (
        True,
        "transport",
    )
    assert classify({"recoverable": True, "error_type": "ReadTimeout"}) == (
        True,
        "timeout",
    )
    assert classify({"recoverable": True, "http_status": 503}) == (True, "http_5xx")
    assert classify({"recoverable": True, "http_status": 429}) == (True, "http_4xx")
    assert classify({"recoverable": False, "http_status": 400}) == (False, "declined")
