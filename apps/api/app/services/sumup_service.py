# Extrait de Vintiz (apps/api/app/services/sumup_service.py, 1484 lignes) —
# réduit au strict périmètre PR2 (§4.5 ARCHITECTURE_PR2.md) :
#
# Conservé : is_configured, describe, ping_reader (pré-vol), _push_to_reader,
# get_checkout_status + _reader_checkout_status, cancel_checkout,
# terminate_reader_checkout, refund_transaction, get_transaction,
# _request_with_retry/_send (tenacity), redact_sumup_error, _friendly_error.
#
# Retiré : _create_link_checkout et tout fallback_mode/prefer_link (D7 — sans
# TPE, espèces uniquement, pas de mode « lien de paiement ») ;
# resolve_reader_from_registry (pas de registre de TPE : SUMUP_READER_ID en
# env, D12) ; is_sandbox/environment en tant que concept « double
# environnement » (remplacé par le seul contrôle nécessaire : refuser une clé
# de test en production, cf. ``is_test_api_key``) ; drain_exchanges et le
# journal SumUpExchange (aucune table ``sumup_exchanges`` en PR2 — les erreurs
# sont journalisées via ``logger`` + ``redact_sumup_error``, jamais persistées) ;
# get_official_receipt ; lecture de ``data/app_config.json`` (configuration
# lue uniquement depuis ``settings``, D12) ; clés affiliate.
#
# Conséquence du push-to-reader devenu l'UNIQUE mode CB (plus de checkout
# « lien ») : un ``checkout_id`` est toujours un ``client_transaction_id``
# SumUp (pas de préfixe ``reader:`` — inutile sans second espace d'ids à
# désambiguïser).
from __future__ import annotations

import json as _json
import logging
import os
import re
import time
from decimal import ROUND_HALF_UP, Decimal

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings

_log = logging.getLogger("fripco")

# ---------------------------------------------------------------------------
# Network timeouts & retry policy — identiques à Vintiz.
# ---------------------------------------------------------------------------
CHECKOUT_TIMEOUT = float(os.getenv("SUMUP_CHECKOUT_TIMEOUT", "30"))  # push / refund
STATUS_TIMEOUT = float(os.getenv("SUMUP_STATUS_TIMEOUT", "20"))      # poll status / lookups
PING_TIMEOUT = float(os.getenv("SUMUP_PING_TIMEOUT", "5"))           # pré-vol TPE

RETRY_MAX_ATTEMPTS = int(os.getenv("SUMUP_RETRY_ATTEMPTS", "3"))
RETRY_WAIT_MULTIPLIER = float(os.getenv("SUMUP_RETRY_WAIT_MULTIPLIER", "1"))
RETRY_WAIT_MIN = float(os.getenv("SUMUP_RETRY_WAIT_MIN", "1"))
RETRY_WAIT_MAX = float(os.getenv("SUMUP_RETRY_WAIT_MAX", "8"))

# Erreurs réseau en phase de connexion : la requête n'a jamais atteint
# SumUp — rejouable sans risque (idempotent ou non).
_RETRY_CONNECT_EXC: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)
# Erreurs réseau en phase de lecture : la requête a peut-être atteint SumUp,
# la réponse s'est perdue. Rejouable UNIQUEMENT pour les GET (idempotents) —
# rejouer un POST pourrait faire sonner deux fois le TPE.
_RETRY_READ_EXC: tuple[type[Exception], ...] = (
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)

# Préfixe des clés API SumUp de test — jamais autorisé en production.
_TEST_KEY_PREFIX = "sup_sk_test_"


def is_test_api_key(api_key: str) -> bool:
    """True si ``api_key`` est une clé SumUp de test (préfixe ``sup_sk_test_``).

    Utilisé par ``cb_router`` pour refuser une clé de test en production —
    comme dans Vintiz (``is_sandbox`` y était une propriété d'instance ;
    ici c'est une fonction pure, le concept de « double environnement » a
    été retiré du service lui-même, D12).
    """
    return bool(api_key) and api_key.startswith(_TEST_KEY_PREFIX)


# ---------------------------------------------------------------------------
# PII redaction pour les payloads d'erreur SumUp — copié tel quel de Vintiz
# (conforme PCI-DSS req. 3 + minimisation RGPD).
# ---------------------------------------------------------------------------

_PAN_RUN_RE = re.compile(r"(?<!\d)\d{13,}(?!\d)")
_PAN_FMT_RE = re.compile(r"(?<!\d)\d{4}(?:[ \-]\d{4}){2,4}(?!\d)")
_CVV_RE = re.compile(
    r"(?i)\b(cvv2?|cvc2?|csc|card_security_code|security_code)\b\s*[:=]?\s*\d{3,}"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}")
_AUTH_HEADER_RE = re.compile(
    r"(?i)\bauthorization\b\s*[:=]\s*[A-Za-z0-9._\-+/= \t]{8,}"
)
_API_KEY_RE = re.compile(
    r"\b(sup_sk_|sk_live_|pk_live_|sk_test_|pk_test_|sk-ant-)[A-Za-z0-9_\-]{20,}"
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
)


def redact_sumup_error(text: str | None, max_len: int = 300) -> str:
    """Nettoie un payload d'erreur SumUp avant journalisation.

    Remplace PAN partiel, CVV, Bearer token ou clé API par un marqueur
    ``<…_REDACTED>`` avant de tronquer à ``max_len`` caractères.
    """
    if not text:
        return ""
    safe = str(text)
    safe = _PAN_FMT_RE.sub("<PAN_REDACTED>", safe)
    safe = _PAN_RUN_RE.sub("<PAN_REDACTED>", safe)
    safe = _CVV_RE.sub(lambda m: f"{m.group(1)} <CVV_REDACTED>", safe)
    safe = _BEARER_RE.sub("Bearer <TOKEN_REDACTED>", safe)
    safe = _AUTH_HEADER_RE.sub("Authorization: <REDACTED>", safe)
    safe = _API_KEY_RE.sub(lambda m: f"{m.group(1)}<API_KEY_REDACTED>", safe)
    safe = _JWT_RE.sub("<JWT_REDACTED>", safe)
    if len(safe) > max_len:
        safe = safe[:max_len].rstrip() + "…"
    return safe


def _extract_sumup_error_code(body_text: str | None) -> str | None:
    """Extrait le ``error_code`` machine d'un corps d'erreur JSON SumUp."""
    if not body_text:
        return None
    try:
        data = _json.loads(body_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("error_code", "code", "type"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().upper()
    return None


# ---------------------------------------------------------------------------
# SumUp service — production uniquement, push reader seulement (D5/D7)
# ---------------------------------------------------------------------------


class SumUpService:
    """Client fin de l'API SumUp Checkout/Readers (production uniquement).

    Configuration lue UNIQUEMENT depuis ``settings`` (variables
    d'environnement) — aucun secret dans ``app_settings`` ni fichier de
    config persisté (D12). ``is_configured`` exige la clé API **et** le
    merchant code **et** un reader — sans TPE, pas de paiement CB possible
    (D7 : sans TPE, espèces uniquement).
    """

    def __init__(self) -> None:
        self.api_key = (settings.SUMUP_API_KEY or "").strip()
        self.merchant_code = (settings.SUMUP_MERCHANT_CODE or "").strip()
        self.reader_id = (settings.SUMUP_READER_ID or "").strip()
        self._api_base = (settings.SUMUP_API_BASE or "").strip().rstrip("/")
        # Override de transport httpx — ``None`` = réseau réel. Les tests
        # injectent un ``httpx.MockTransport`` ici (comme Vintiz).
        self._transport = None

    @property
    def is_configured(self) -> bool:
        """True quand clé API + merchant code + reader sont tous les trois posés."""
        return bool(self.api_key and self.merchant_code and self.reader_id)

    def describe(self) -> dict:
        """Snapshot de config pour l'admin — jamais de secret en clair."""
        merchant_masked = ""
        if self.merchant_code:
            merchant_masked = (
                self.merchant_code[:2] + "***" + self.merchant_code[-2:]
                if len(self.merchant_code) > 4
                else "***"
            )
        reader_masked = ""
        if self.reader_id:
            reader_masked = (
                self.reader_id[:4] + "***" + self.reader_id[-2:]
                if len(self.reader_id) > 6
                else "***"
            )
        return {
            "configured": self.is_configured,
            "api_key_set": bool(self.api_key),
            "merchant_code_set": bool(self.merchant_code),
            "merchant_code_masked": merchant_masked,
            "reader_id_set": bool(self.reader_id),
            "reader_id_masked": reader_masked,
            "api_base": self._api_base,
        }

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _client(self, timeout: float) -> httpx.AsyncClient:
        """Client httpx honorant l'éventuel transport de test."""
        kwargs: dict = {"timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        counter: dict | None = None,
    ) -> httpx.Response:
        """Émet la requête HTTP en rejouant les erreurs transport *transitoires*.

        Seules les erreurs de transport sont rejouées (jamais une réponse
        4xx/5xx, jamais un POST sur les erreurs de lecture — il pourrait
        avoir déjà atteint le TPE).
        """
        if counter is None:
            counter = {"n": 0}
        idempotent = method.upper() in ("GET", "HEAD", "OPTIONS")
        retry_exc = _RETRY_CONNECT_EXC + (_RETRY_READ_EXC if idempotent else ())
        resp: httpx.Response | None = None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max(RETRY_MAX_ATTEMPTS, 1)),
            wait=wait_exponential(
                multiplier=RETRY_WAIT_MULTIPLIER,
                min=RETRY_WAIT_MIN,
                max=RETRY_WAIT_MAX,
            ),
            retry=retry_if_exception_type(retry_exc),
            reraise=True,
        ):
            with attempt:
                counter["n"] += 1
                resp = await client.request(
                    method, url, json=json, params=params, headers=self._headers
                )
        return resp  # type: ignore[return-value]

    async def _send(
        self,
        client: httpx.AsyncClient,
        operation: str,
        method: str,
        url: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        """Émet une requête authentifiée et journalise les échecs (redigés).

        Ne persiste jamais rien en base (pas de table ``sumup_exchanges`` en
        PR2) — uniquement des logs via ``logger``, jamais la clé API.
        """
        started = time.perf_counter()
        counter = {"n": 0}
        try:
            resp = await self._request_with_retry(
                client, method, url, json=json, params=params, counter=counter
            )
        except Exception as exc:  # noqa: BLE001 — log puis re-lève
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            _log.error(
                "SumUp %s %s a échoué après %d tentative(s) en %d ms : %s",
                operation,
                method.upper(),
                counter["n"],
                elapsed_ms,
                redact_sumup_error(f"{type(exc).__name__}: {exc}"),
            )
            raise
        if resp.status_code >= 400:
            body = resp.text if resp.content else ""
            _log.warning(
                "SumUp %s %s -> HTTP %d : %s",
                operation,
                method.upper(),
                resp.status_code,
                redact_sumup_error(body, max_len=300),
            )
        return resp

    # ------------------------------------------------------------------
    # Pré-vol TPE
    # ------------------------------------------------------------------
    async def ping_reader(self) -> dict:
        """Sonde le reader configuré et retourne un statut structuré.

        Distingue l'état d'appairage (``GET /readers/{id}``) de l'état live
        Wi-Fi/4G (``GET /readers/{id}/status``), comme Vintiz — permet au
        bandeau caisse de distinguer « jamais appairé » de « appairé mais
        Wi-Fi coupé ».
        """
        if not self.is_configured:
            missing = [
                name
                for name, val in (
                    ("SUMUP_API_KEY", self.api_key),
                    ("SUMUP_MERCHANT_CODE", self.merchant_code),
                    ("SUMUP_READER_ID", self.reader_id),
                )
                if not val
            ]
            return {
                "configured": False,
                "paired": False,
                "online": False,
                "ready": False,
                "status": "unconfigured",
                "message": f"Non configuré ({', '.join(missing)} manquant(s))",
            }

        reader_url = f"{self._api_base}/merchants/{self.merchant_code}/readers/{self.reader_id}"
        status_url = f"{reader_url}/status"

        async with self._client(PING_TIMEOUT) as client:
            try:
                resp = await self._send(client, "ping", "GET", reader_url)
            except httpx.HTTPError as exc:
                return {
                    "configured": True, "paired": False, "online": False, "ready": False,
                    "status": "network_error",
                    "message": f"SumUp Cloud injoignable : {redact_sumup_error(str(exc))}",
                }
            if resp.status_code == 404:
                return {
                    "configured": True, "paired": False, "online": False, "ready": False,
                    "status": "reader_not_found",
                    "message": "Le TPE configuré (SUMUP_READER_ID) n'existe plus côté SumUp — rappairez-le",
                }
            if resp.status_code != 200:
                return {
                    "configured": True, "paired": False, "online": False, "ready": False,
                    "status": f"http_{resp.status_code}",
                    "message": f"SumUp Cloud {resp.status_code} : {redact_sumup_error(resp.text)[:120]}",
                }
            reader = resp.json()
            pairing = (reader.get("status") or "").lower()
            if pairing != "paired":
                return {
                    "configured": True, "paired": False, "online": False, "ready": False,
                    "status": f"pairing_{pairing or 'unknown'}",
                    "message": {
                        "processing": "TPE en cours d'appairage avec SumUp — patientez quelques minutes",
                        "expired": "Appairage SumUp expiré — supprimez ce TPE et recréez-le",
                        "unknown": "Statut d'appairage SumUp inconnu — réessayez plus tard",
                    }.get(pairing, f"TPE non appairé ({pairing})"),
                    "name": reader.get("name"),
                }

            try:
                live_resp = await self._send(client, "ping_status", "GET", status_url)
            except httpx.HTTPError:
                return {
                    "configured": True, "paired": True, "online": False, "ready": True,
                    "status": "live_unreachable",
                    "message": "TPE appairé mais l'état live n'est pas joignable — tentez quand même la vente",
                    "name": reader.get("name"),
                }
            if live_resp.status_code == 404:
                return {
                    "configured": True, "paired": True, "online": True, "ready": True,
                    "status": "paired",
                    "message": "TPE appairé (état live indisponible — firmware Solo trop ancien ?)",
                    "name": reader.get("name"),
                }
            if live_resp.status_code != 200:
                return {
                    "configured": True, "paired": True, "online": False, "ready": True,
                    "status": f"live_http_{live_resp.status_code}",
                    "message": "État live SumUp indisponible — TPE appairé, tentez quand même",
                    "name": reader.get("name"),
                }
            live = (live_resp.json() or {}).get("data", {}) or {}
            device_status = (live.get("status") or "").upper()
            online = device_status == "ONLINE"
            return {
                "configured": True, "paired": True,
                "online": online, "ready": online,
                "status": device_status.lower() or "unknown",
                "state": live.get("state"),
                "battery_level": live.get("battery_level"),
                "connection_type": live.get("connection_type"),
                "name": reader.get("name"),
                "message": (
                    f"TPE en ligne ({live.get('state', 'IDLE')})"
                    if online
                    else "TPE hors ligne — vérifiez le Wi-Fi/4G du Solo"
                ),
            }

    # ------------------------------------------------------------------
    # Push paiement sur le TPE
    # ------------------------------------------------------------------
    async def _push_to_reader(
        self,
        *,
        amount: Decimal,
        client_transaction_id: str,
        description: str = "Vente Frip & Co Street",
    ) -> dict:
        """Pousse un paiement sur le SumUp Solo via la Readers API.

        Spec : ``POST /v0.1/merchants/{m}/readers/{r}/checkout``. Le montant
        est envoyé en unités mineures (centimes) entières, comme l'exige
        SumUp. ``client_transaction_id`` est notre propre identifiant
        (``client_uuid`` de la vente, ou une variante suffixée en cas de
        réessai) — devient le ``checkout_id`` retourné, unique par
        ``PaymentAttempt``.
        """
        url = f"{self._api_base}/merchants/{self.merchant_code}/readers/{self.reader_id}/checkout"
        minor_units = int(
            (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        payload: dict = {
            "total_amount": {
                "value": minor_units,
                "currency": "EUR",
                "minor_unit": 2,
            },
            "description": description,
            "client_transaction_id": client_transaction_id,
        }
        try:
            async with self._client(CHECKOUT_TIMEOUT) as client:
                resp = await self._send(client, "reader_push", "POST", url, json=payload)
        except httpx.HTTPError as exc:
            return {
                "checkout_id": client_transaction_id,
                "client_transaction_id": client_transaction_id,
                "status": "FAILED",
                "error_type": type(exc).__name__,
                "error_detail": redact_sumup_error(f"network error: {exc}"),
                "error_friendly": "TPE injoignable — vérifiez le Wi-Fi/4G du Solo et réessayez",
                "recoverable": True,
            }
        if resp.status_code in (200, 201, 202):
            return {
                "checkout_id": client_transaction_id,
                "client_transaction_id": client_transaction_id,
                "status": "PENDING",
                "reader_id": self.reader_id,
            }
        body_text = resp.text if resp.content else ""
        error_code = _extract_sumup_error_code(body_text)
        recoverable, retry_after = _reader_error_recoverability(resp.status_code, error_code)
        result = {
            "checkout_id": client_transaction_id,
            "client_transaction_id": client_transaction_id,
            "status": "FAILED",
            "http_status": resp.status_code,
            "error_code": error_code,
            "error_detail": redact_sumup_error(body_text),
            "error_friendly": _friendly_error(resp.status_code, body_text),
            "recoverable": recoverable,
        }
        if retry_after:
            result["retry_after"] = retry_after
        return result

    # ------------------------------------------------------------------
    # Poll statut
    # ------------------------------------------------------------------
    async def get_checkout_status(self, checkout_id: str) -> dict:
        if not self.is_configured:
            return {
                "checkout_id": checkout_id,
                "status": "FAILED",
                "error": "SumUp non configuré",
            }
        return await self._reader_checkout_status(checkout_id)

    async def _reader_checkout_status(self, client_transaction_id: str) -> dict:
        """Résout un push reader via la Transactions API.

        Tant que le client n'a pas tapé sa carte, la transaction n'existe pas
        encore (404/vide) → ``PENDING``. Les erreurs transitoires SumUp sont
        aussi mappées ``PENDING`` (le front-end borne l'attente à 90 s côté
        UI) plutôt que d'afficher un faux « Carte refusée ».
        """
        base = {"checkout_id": client_transaction_id}
        if not self.merchant_code:
            return {**base, "status": "FAILED", "error": "SUMUP_MERCHANT_CODE manquant"}
        url = f"https://api.sumup.com/v2.1/merchants/{self.merchant_code}/transactions"
        try:
            async with self._client(STATUS_TIMEOUT) as client:
                resp = await self._send(
                    client,
                    "reader_status",
                    "GET",
                    url,
                    params={"client_transaction_id": client_transaction_id},
                )
        except httpx.HTTPError:
            return {**base, "status": "PENDING"}

        if resp.status_code == 404:
            return {**base, "status": "PENDING"}
        if resp.status_code in (401, 403):
            return {
                **base, "status": "FAILED", "http_status": resp.status_code,
                "error_friendly": _friendly_error(resp.status_code, resp.text),
            }
        if resp.status_code != 200:
            return {**base, "status": "PENDING"}

        data = resp.json() or {}
        txn = data
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            txn = (data["items"] or [{}])[0] or {}
        raw_status = (txn.get("status") or "").upper()
        norm = _normalize_txn_status(raw_status)
        amount = txn.get("amount")
        if amount is None and isinstance(txn.get("total_amount"), dict):
            total_amount = txn["total_amount"]
            value = total_amount.get("value")
            minor_unit = int(total_amount.get("minor_unit", 2) or 2)
            if value is not None:
                amount = float(value) / (10 ** minor_unit)
        result = {
            **base,
            "status": norm,
            "sumup_status": raw_status,
            "amount": amount,
            "currency": txn.get("currency") or (txn.get("total_amount") or {}).get("currency"),
        }
        if norm == "PAID":
            card = txn.get("card") or {}
            result.update({
                "sumup_transaction_id": txn.get("id"),
                "sumup_transaction_code": txn.get("transaction_code"),
                "sumup_auth_code": txn.get("auth_code"),
                "sumup_card_brand": card.get("type") or card.get("scheme"),
                "sumup_card_last4": card.get("last_4_digits"),
            })
        return result

    # ------------------------------------------------------------------
    # Annulation
    # ------------------------------------------------------------------
    async def cancel_checkout(self, checkout_id: str) -> bool:
        """Annule un paiement en attente.

        En mode push-reader (le seul mode PR2, D7), il n'existe pas de
        ressource Checkout à supprimer — l'annulation passe systématiquement
        par :meth:`terminate_reader_checkout` (no-op documenté si le client a
        déjà tapé sa carte).
        """
        if not self.is_configured:
            return False
        outcome = await self.terminate_reader_checkout()
        return bool(outcome.get("ok"))

    async def terminate_reader_checkout(self) -> dict:
        """Annule le paiement en cours affiché sur le Solo.

        Spec : ``POST /v0.1/merchants/{m}/readers/{r}/terminate``. Sans effet
        documenté si le client a déjà tapé sa carte (la transaction se
        termine normalement).
        """
        if not self.is_configured:
            return {"ok": False, "status": "unconfigured", "message": "SumUp non configuré"}

        url = f"{self._api_base}/merchants/{self.merchant_code}/readers/{self.reader_id}/terminate"
        try:
            async with self._client(STATUS_TIMEOUT) as client:
                resp = await self._send(client, "terminate", "POST", url)
        except httpx.HTTPError as exc:
            return {
                "ok": False, "status": "network_error",
                "message": f"SumUp injoignable : {redact_sumup_error(str(exc))}",
            }
        if resp.status_code == 202:
            return {"ok": True, "status": "terminated", "message": "Annulation envoyée au TPE"}
        if resp.status_code == 422:
            return {
                "ok": False, "status": "not_waiting",
                "message": "Le TPE n'attend pas le client (déjà payé ou inactif) — annulation refusée",
            }
        if resp.status_code == 404:
            return {"ok": False, "status": "not_found", "message": "TPE introuvable côté SumUp"}
        return {
            "ok": False, "status": f"http_{resp.status_code}",
            "message": _friendly_error(resp.status_code, resp.text),
        }

    # ------------------------------------------------------------------
    # Remboursement
    # ------------------------------------------------------------------
    async def refund_transaction(
        self,
        transaction_id: str,
        *,
        amount: Decimal | None = None,
    ) -> dict:
        """Rembourse une transaction SumUp en tout ou partie.

        Spec : ``POST /v1.0/merchants/{m}/payments/{id}/refunds``. Appelé
        par le flux d'annulation (D6) AVANT toute écriture locale — un refus
        SumUp doit bloquer la vente inverse plutôt que créer un écart de
        caisse.
        """
        if not self.is_configured:
            return {"ok": False, "status": "unconfigured", "message": "SumUp non configuré"}
        if not transaction_id:
            return {"ok": False, "status": "invalid_id", "message": "ID transaction SumUp invalide"}

        url = f"https://api.sumup.com/v1.0/merchants/{self.merchant_code}/payments/{transaction_id}/refunds"
        body: dict = {}
        if amount is not None:
            body["amount"] = round(float(amount), 2)
        try:
            async with self._client(CHECKOUT_TIMEOUT) as client:
                resp = await self._send(client, "refund", "POST", url, json=body)
        except httpx.HTTPError as exc:
            return {
                "ok": False, "status": "network_error",
                "message": f"SumUp injoignable : {redact_sumup_error(str(exc))}",
            }
        if resp.status_code == 204:
            return {
                "ok": True, "status": "refunded", "http_status": 204,
                "message": f"Remboursement SumUp effectué ({amount if amount is not None else 'total'} EUR)",
            }
        body_text = resp.text
        try:
            data = resp.json()
            error_code = data.get("error_code")
            error_msg = data.get("message")
        except Exception:  # noqa: BLE001
            error_code, error_msg = None, body_text[:200]
        return {
            "ok": False,
            "status": f"http_{resp.status_code}",
            "http_status": resp.status_code,
            "error_code": error_code,
            "message": (
                _friendly_refund_error(error_code, error_msg)
                or _friendly_error(resp.status_code, body_text)
            ),
        }

    # ------------------------------------------------------------------
    # Lookup transaction (réconciliation, utilisé par sumup_verify)
    # ------------------------------------------------------------------
    async def get_transaction(
        self,
        *,
        transaction_id: str | None = None,
        transaction_code: str | None = None,
        foreign_transaction_id: str | None = None,
    ) -> dict | None:
        """Recherche le détail complet d'une transaction SumUp.

        Spec : ``GET /v2.1/merchants/{m}/transactions``. Au moins un des
        trois identifiants doit être fourni. Retourne ``None`` sur erreur ou
        si rien n'est trouvé — jamais d'exception (appelant tolérant).
        """
        if not self.is_configured:
            return None
        params: dict = {}
        if transaction_id:
            params["id"] = transaction_id
        elif transaction_code:
            params["transaction_code"] = transaction_code
        elif foreign_transaction_id:
            params["foreign_transaction_id"] = foreign_transaction_id
        else:
            return None
        url = f"https://api.sumup.com/v2.1/merchants/{self.merchant_code}/transactions"
        try:
            async with self._client(STATUS_TIMEOUT) as client:
                resp = await self._send(client, "get_transaction", "GET", url, params=params)
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError as exc:
            _log.warning("get_transaction network error: %s", redact_sumup_error(str(exc)))
        return None


# ---------------------------------------------------------------------------
# Mapping d'erreurs — messages français pour la caissière
# ---------------------------------------------------------------------------

_ERROR_CODES_FR: dict[str, str] = {
    "READER_BUSY": "Le terminal traite déjà un paiement. Patientez ~10 secondes puis réessayez, ou redémarrez le TPE.",
    "READER_OFFLINE": "Le terminal est hors ligne. Vérifiez le Wi-Fi/4G du SumUp Solo et que l'écran d'accueil est affiché.",
    "READER_NOT_FOUND": "Terminal inconnu. Vérifiez SUMUP_READER_ID.",
    "CONFLICT": "Cette transaction n'est pas dans un état remboursable (déjà remboursée, expirée, ou non finalisée).",
    "NOT_ENOUGH_BALANCE": "Solde SumUp insuffisant pour ce remboursement. Voir l'app SumUp.",
    "NOT_FOUND": "Transaction introuvable côté SumUp. Vérifiez l'identifiant ou patientez quelques minutes.",
    "MISSING_FIELD": "Données manquantes envoyées à SumUp — contactez le support.",
    "NETWORK_ERROR": "Problème de connexion à SumUp. Vérifiez la connexion Internet de la caisse, puis réessayez.",
    "TIMEOUT": "Le terminal met trop de temps à répondre. Réessayez, ou encaissez en espèces.",
}

_RECOVERABLE_READER_CODES = {"READER_BUSY", "READER_OFFLINE"}


def _reader_error_recoverability(
    http_status: int, error_code: str | None
) -> tuple[bool, int | None]:
    """Classe un échec de push reader en (récupérable, délai de reprise)."""
    code = (error_code or "").upper()
    if code == "READER_BUSY":
        return True, 5
    if code in _RECOVERABLE_READER_CODES:
        return True, None
    if http_status == 429 or 500 <= http_status < 600:
        return True, None
    return False, None


def _normalize_txn_status(raw: str | None) -> str:
    """Normalise un statut SumUp vers PAID/FAILED/CANCELLED/PENDING.

    La Transactions API (push reader) utilise SUCCESSFUL/FAILED/CANCELLED/
    PENDING — on ramène ``SUCCESSFUL`` à ``PAID`` pour un vocabulaire unique
    côté caisse.
    """
    s = (raw or "").upper()
    if s in ("PAID", "SUCCESSFUL"):
        return "PAID"
    if s in ("FAILED", "REFUSED", "DECLINED", "EXPIRED"):
        return "FAILED"
    if s in ("CANCELLED", "CANCELED"):
        return "CANCELLED"
    return "PENDING"


def _friendly_error(http_status: int, body_text: str) -> str:
    """Mappe (statut HTTP, corps d'erreur) → message français."""
    code = _extract_sumup_error_code(body_text)
    if code and code in _ERROR_CODES_FR:
        return _ERROR_CODES_FR[code]
    if http_status == 401:
        return "Clé API SumUp invalide ou expirée."
    if http_status == 403:
        return "Permissions insuffisantes sur la clé SumUp (scopes payments, readers.read, readers.write)."
    if http_status == 404:
        return "Ressource SumUp introuvable."
    if http_status in (408, 504):
        return "Le terminal met trop de temps à répondre — vérifiez le Wi-Fi/4G du Solo puis réessayez."
    if http_status == 409:
        return "Conflit côté SumUp (paiement déjà traité ou en cours)."
    if http_status == 422:
        return "Données refusées par SumUp."
    if http_status == 429:
        return "Trop d'appels SumUp — patientez quelques secondes."
    if 500 <= http_status < 600:
        return "Erreur serveur SumUp — réessayer plus tard."
    return f"SumUp HTTP {http_status} — {redact_sumup_error(body_text)[:200]}"


def _friendly_refund_error(error_code: str | None, message: str | None) -> str | None:
    """Mappe un ``error_code`` de remboursement → message français ; None si inconnu."""
    if not error_code:
        return None
    return _ERROR_CODES_FR.get(error_code)
