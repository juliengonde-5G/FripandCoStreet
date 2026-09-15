# Nouveau test (PR3, §7 ARCHITECTURE_PR3.md) — passerelle e-mail (Brevo ->
# SMTP -> simulation, E6). Aucun appel reseau reel : `httpx.MockTransport`
# injecte dans `email_gateway._transport` (meme patron que
# `SumUpService._transport`, `tests/test_sumup_service.py`) ; `smtplib`
# monkeypatche pour le repli SMTP.
from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import settings
from app.services import email_gateway
from app.services.email_gateway import EmailMessage

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _clear_email_config(monkeypatch):
    """Repart d'une configuration e-mail vierge a chaque test."""
    monkeypatch.setattr(settings, "BREVO_API_KEY", "")
    monkeypatch.setattr(settings, "BREVO_ANONYMOUS_TRACKING", False)
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(settings, "SMTP_USER", "")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    monkeypatch.setattr(settings, "EMAIL_FROM_ADDRESS", "noreply@fripco-street.fr")
    monkeypatch.setattr(settings, "EMAIL_FROM_NAME", "Frip & Co Street")
    monkeypatch.setattr(email_gateway, "_transport", None)
    yield
    monkeypatch.setattr(email_gateway, "_transport", None)


async def test_simulate_when_nothing_configured():
    result = await email_gateway.send_email(
        EmailMessage(to="cliente@example.com", subject="Bonjour", html="<b>Salut</b>")
    )
    assert result.status == "simulated"
    assert result.provider == "simulated"


async def test_brevo_call_shape_with_anonymous_tracking_confirmed(monkeypatch):
    monkeypatch.setattr(settings, "BREVO_API_KEY", "ak-test")
    monkeypatch.setattr(settings, "BREVO_ANONYMOUS_TRACKING", True)

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"messageId": "<abc@fripco>"})

    email_gateway._transport = httpx.MockTransport(handler)

    result = await email_gateway.send_email(
        EmailMessage(
            to="cliente@example.com",
            subject="Votre ticket Frip & Co Street n° 42",
            html="<pre>TICKET</pre>",
            text="TICKET",
        )
    )

    assert result.status == "sent"
    assert result.provider == "brevo"
    assert result.message_id == "<abc@fripco>"
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.brevo.com/v3/smtp/email"
    assert seen["headers"]["api-key"] == "ak-test"
    assert seen["body"]["sender"] == {
        "email": "noreply@fripco-street.fr",
        "name": "Frip & Co Street",
    }
    assert seen["body"]["to"] == [{"email": "cliente@example.com"}]
    assert seen["body"]["subject"] == "Votre ticket Frip & Co Street n° 42"
    assert "TICKET" in seen["body"]["htmlContent"]
    assert seen["body"]["textContent"] == "TICKET"


async def test_brevo_uses_configurable_api_base(monkeypatch):
    monkeypatch.setattr(settings, "BREVO_API_KEY", "ak-test")
    monkeypatch.setattr(settings, "BREVO_ANONYMOUS_TRACKING", True)
    monkeypatch.setattr(settings, "BREVO_API_BASE", "http://fake-brevo.local")

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(201, json={"messageId": "<x@fripco>"})

    email_gateway._transport = httpx.MockTransport(handler)

    await email_gateway.send_email(
        EmailMessage(to="a@b.fr", subject="x", html="<p>x</p>")
    )
    assert seen["url"] == "http://fake-brevo.local/v3/smtp/email"


async def test_brevo_http_error_returns_failed_status(monkeypatch):
    monkeypatch.setattr(settings, "BREVO_API_KEY", "ak-test")
    monkeypatch.setattr(settings, "BREVO_ANONYMOUS_TRACKING", True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    email_gateway._transport = httpx.MockTransport(handler)

    result = await email_gateway.send_email(
        EmailMessage(to="a@b.fr", subject="x", html="<p>x</p>")
    )
    assert result.status == "failed"
    assert result.provider == "brevo"
    assert result.error is not None


async def test_missing_anonymous_tracking_falls_back_to_smtp(monkeypatch):
    """E6 — Brevo configuré mais BREVO_ANONYMOUS_TRACKING absent : repli
    SMTP (statut cohérent), sans jamais appeler Brevo."""
    monkeypatch.setattr(settings, "BREVO_API_KEY", "ak-test")
    monkeypatch.setattr(settings, "BREVO_ANONYMOUS_TRACKING", False)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_USER", "user")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "pw")

    def _fail_if_called(*a, **k):  # Brevo ne doit jamais être appelé.
        raise AssertionError("Brevo appelé malgré BREVO_ANONYMOUS_TRACKING absent")

    email_gateway._transport = httpx.MockTransport(_fail_if_called)

    calls: list = []

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append((host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def sendmail(self, sender, to, message):
            calls.append(("sent", to))

    monkeypatch.setattr(email_gateway.smtplib, "SMTP", _FakeSMTP)

    result = await email_gateway.send_email(
        EmailMessage(to="cliente@example.com", subject="x", html="<p>x</p>")
    )
    assert result.status == "sent"
    assert result.provider == "smtp"
    assert ("smtp.example.com", 587) in calls


async def test_missing_anonymous_tracking_falls_back_to_simulation_without_smtp(monkeypatch):
    monkeypatch.setattr(settings, "BREVO_API_KEY", "ak-test")
    monkeypatch.setattr(settings, "BREVO_ANONYMOUS_TRACKING", False)

    def _fail_if_called(*a, **k):
        raise AssertionError("Brevo appelé malgré BREVO_ANONYMOUS_TRACKING absent")

    email_gateway._transport = httpx.MockTransport(_fail_if_called)

    result = await email_gateway.send_email(
        EmailMessage(to="cliente@example.com", subject="x", html="<p>x</p>")
    )
    assert result.status == "simulated"
    assert result.provider == "simulated"


async def test_smtp_send_failure_returns_failed_status(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_USER", "user")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "pw")

    class _FakeSMTP:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(email_gateway.smtplib, "SMTP", _FakeSMTP)

    result = await email_gateway.send_email(
        EmailMessage(to="cliente@example.com", subject="x", html="<p>x</p>")
    )
    assert result.status == "failed"
    assert result.provider == "smtp"


def test_describe_active_provider_never_leaks_secrets(monkeypatch):
    monkeypatch.setattr(settings, "BREVO_API_KEY", "super-secret-key")
    monkeypatch.setattr(settings, "BREVO_ANONYMOUS_TRACKING", True)
    body = email_gateway.describe_active_provider()
    assert body["provider"] == "brevo"
    assert body["anonymous_tracking"] is True
    assert "super-secret-key" not in json.dumps(body)


def test_describe_active_provider_defaults_to_simulated():
    body = email_gateway.describe_active_provider()
    assert body["provider"] == "simulated"
