# Nouveau test (PR12, docs/ARCHITECTURE_PR12.md §3, N2) — supervision
# technique : forme complete de la reponse, taches planifiees, cache des
# integrites, statut global, files d'attente, tampon des erreurs 500 et
# absence totale de secret dans la reponse.
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core import middleware as middleware_mod
from app.core.config import settings
from app.core.database import async_session
from app.jobs import SCHEDULED_JOBS, JOB_NIGHTLY_DATABASE_BACKUP, list_registered_jobs
from app.models.database_backup import BackupStatus, BackupTrigger, DatabaseBackup
from app.models.failed_payment import FailedPayment, FailedPaymentStatus
from app.models.payment_attempt import PaymentAttempt, PaymentAttemptStatus
from app.services import monitoring
from app.services import sumup_service as sumup_mod
from app.services.jet import EVENT_SYSTEM_JOB_FAILED, JournalService

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _reset_process_state():
    """Le cache d'integrite, le tampon d'erreurs et le dernier ping TPE vivent
    en memoire de PROCESSUS : sans remise a zero, un test heriterait de ce
    qu'un autre a laisse."""
    monitoring.reset_integrity_cache()
    middleware_mod.reset_recent_errors()
    sumup_mod.reset_last_reader_ping()
    yield
    monitoring.reset_integrity_cache()
    middleware_mod.reset_recent_errors()
    sumup_mod.reset_last_reader_ping()


async def _fresh_backup() -> None:
    """Une sauvegarde reussie de la nuit — sans elle, `backups.stale` est vrai
    (une installation sans sauvegarde EST une anomalie a signaler)."""
    async with async_session() as db:
        db.add(
            DatabaseBackup(
                finished_at=datetime.now(timezone.utc),
                trigger=BackupTrigger.nightly,
                status=BackupStatus.success,
                filename=f"fripco_{uuid.uuid4().hex}.sql.gz",
                size_bytes=1024,
                sha256="a" * 64,
                duration_ms=1200,
            )
        )
        await db.commit()


async def _pending_failed_payment() -> None:
    async with async_session() as db:
        attempt = PaymentAttempt(
            client_uuid=uuid.uuid4(),
            amount=Decimal("42.00"),
            status=PaymentAttemptStatus.failed,
            checkout_id=f"chk_{uuid.uuid4().hex[:12]}",
        )
        db.add(attempt)
        await db.flush()
        db.add(
            FailedPayment(
                attempt_id=attempt.id,
                client_uuid=attempt.client_uuid,
                amount=Decimal("42.00"),
                status=FailedPaymentStatus.pending,
                error_type="transport",
            )
        )
        await db.commit()


async def _get(client, auth_headers, **params) -> dict:
    r = await client.get("/api/admin/monitoring", params=params, headers=auth_headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Forme de la reponse
# ---------------------------------------------------------------------------


async def test_monitoring_full_shape(client, auth_headers):
    body = await _get(client, auth_headers)

    assert set(body) == {
        "generated_at",
        "app",
        "database",
        "backups",
        "jobs",
        "integrity",
        "external",
        "printer",
        "queues",
        "recent_errors",
        "status",
    }
    assert set(body["app"]) == {
        "version",
        "build_sha",
        "build_date",
        "environment",
        "expected_db_revision",
        "current_db_revision",
        "db_revision_ok",
        "uptime_seconds",
    }
    assert body["app"]["version"] == "0.13.0"
    assert body["app"]["environment"] == "test"
    assert body["app"]["db_revision_ok"] is True
    assert body["app"]["current_db_revision"] == body["app"]["expected_db_revision"]
    assert body["app"]["uptime_seconds"] >= 0

    assert set(body["database"]) == {"ok", "latency_ms", "size_bytes", "tables_count"}
    assert body["database"]["ok"] is True
    assert body["database"]["tables_count"] > 0

    assert set(body["backups"]) == {"last", "nightly_enabled", "dir_free_bytes", "stale"}
    assert set(body["external"]) == {"sumup", "brevo", "openweather"}
    assert set(body["external"]["sumup"]) == {"configured", "reader_configured", "last_ping"}
    assert set(body["external"]["openweather"]) == {"configured", "cache_age_seconds"}
    assert set(body["printer"]) == {"mode", "online", "latency_ms"}
    assert set(body["queues"]) == {
        "failed_payments_pending",
        "sumup_exchange_errors_24h",
        "clients_deletion_due",
    }
    assert body["queues"] == {
        "failed_payments_pending": 0,
        "sumup_exchange_errors_24h": 0,
        "clients_deletion_due": 0,
    }
    assert body["recent_errors"] == []


async def test_monitoring_requires_authentication(client):
    r = await client.get("/api/admin/monitoring")
    assert r.status_code == 401
    r = await client.post("/api/admin/monitoring/check")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Taches planifiees
# ---------------------------------------------------------------------------


async def test_monitoring_lists_scheduled_jobs(client, auth_headers):
    body = await _get(client, auth_headers)
    names = [job["name"] for job in body["jobs"]]
    assert names == [job.name for job in SCHEDULED_JOBS]
    for job in body["jobs"]:
        assert set(job) == {"name", "cron", "next_run_at", "last_run"}
        assert job["cron"].count(" ") == 4
        # Aucun planificateur ne tourne sous les tests : la liste doit
        # neanmoins etre complete (echeance inconnue plutot qu'ecran vide).
        assert job["next_run_at"] is None
        assert job["last_run"] is None


def test_list_registered_jobs_matches_declaration():
    assert [job["name"] for job in list_registered_jobs()] == [
        job.name for job in SCHEDULED_JOBS
    ]


async def test_monitoring_reports_last_job_failure_from_jet(client, auth_headers):
    async with async_session() as db:
        await JournalService(db).record(
            EVENT_SYSTEM_JOB_FAILED,
            payload={"job": JOB_NIGHTLY_DATABASE_BACKUP, "error": "pg_dump introuvable"},
        )
        await db.commit()

    body = await _get(client, auth_headers)
    job = next(j for j in body["jobs"] if j["name"] == JOB_NIGHTLY_DATABASE_BACKUP)
    assert job["last_run"]["status"] == "failed"
    assert job["last_run"]["detail"] == "pg_dump introuvable"
    assert job["last_run"]["at"]


# ---------------------------------------------------------------------------
# Integrite : null, calcul a la demande, cache
# ---------------------------------------------------------------------------


async def test_integrity_is_null_then_computed_then_cached(client, auth_headers):
    body = await _get(client, auth_headers)
    assert body["integrity"] is None

    checked = await _get(client, auth_headers, check=1)
    assert set(checked["integrity"]) == {"jet", "fiscal", "closures"}
    assert set(checked["integrity"]["jet"]) == {"valid", "count", "checked_at"}
    assert checked["integrity"]["jet"]["valid"] is True
    assert checked["integrity"]["fiscal"]["valid"] is True
    assert checked["integrity"]["closures"]["valid"] is True

    # Lecture suivante SANS `check` : le dernier resultat est resservi depuis
    # le cache memoire (meme instant de verification).
    cached = await _get(client, auth_headers)
    assert cached["integrity"]["jet"]["checked_at"] == checked["integrity"]["jet"]["checked_at"]

    # ... et il expire au-dela de 10 minutes.
    monitoring._integrity_cache = (
        monitoring._integrity_cache[0] - monitoring.INTEGRITY_CACHE_TTL_SECONDS - 1,
        monitoring._integrity_cache[1],
    )
    assert (await _get(client, auth_headers))["integrity"] is None


async def test_check_endpoint_records_jet_event(client, auth_headers):
    from sqlalchemy import select

    from app.models.jet import JournalEvent

    r = await client.post("/api/admin/monitoring/check", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["integrity"]["jet"]["valid"] is True

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(
                    JournalEvent.event_type == "fiscal.integrity_checked"
                )
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["source"] == "monitoring"
    # La chaine reste valide apres cette ecriture (c'est la seule du module).
    async with async_session() as db:
        assert (await JournalService(db).verify_chain())["valid"] is True


# ---------------------------------------------------------------------------
# Statut global
# ---------------------------------------------------------------------------


async def test_status_ok_with_fresh_backup(client, auth_headers):
    await _fresh_backup()
    body = await _get(client, auth_headers)
    assert body["backups"]["stale"] is False
    assert body["backups"]["last"]["status"] == "success"
    assert body["status"] == "ok"


async def test_status_warning_when_backup_is_stale(client, auth_headers):
    body = await _get(client, auth_headers)
    assert body["backups"]["stale"] is True
    assert body["status"] == "warning"


async def test_status_warning_when_payment_queue_not_empty(client, auth_headers):
    await _fresh_backup()
    await _pending_failed_payment()
    body = await _get(client, auth_headers)
    assert body["queues"]["failed_payments_pending"] == 1
    assert body["status"] == "warning"


async def test_status_critical_when_db_revision_differs(
    client, auth_headers, monkeypatch
):
    await _fresh_backup()
    monkeypatch.setattr(monitoring, "EXPECTED_DB_REVISION", "9999")
    body = await _get(client, auth_headers)
    assert body["app"]["db_revision_ok"] is False
    assert body["status"] == "critical"


async def test_status_critical_when_integrity_invalid(
    client, auth_headers, monkeypatch
):
    await _fresh_backup()

    async def _broken_chain(self):
        return {"valid": False, "count": 3, "first_invalid_seq": 2, "errors": ["x"]}

    monkeypatch.setattr(JournalService, "verify_chain", _broken_chain)
    body = await _get(client, auth_headers, check=1)
    assert body["integrity"]["jet"]["valid"] is False
    assert body["status"] == "critical"


async def test_status_warning_when_sumup_reader_not_ready(
    client, auth_headers, monkeypatch
):
    await _fresh_backup()
    monkeypatch.setattr(settings, "SUMUP_API_KEY", "sup_sk_secret_value")
    monkeypatch.setattr(settings, "SUMUP_MERCHANT_CODE", "MCODE123")
    monkeypatch.setattr(settings, "SUMUP_READER_ID", "rdr_abcdef123456")
    sumup_mod._remember_reader_ping(False)

    body = await _get(client, auth_headers)
    assert body["external"]["sumup"]["configured"] is True
    assert body["external"]["sumup"]["last_ping"]["ready"] is False
    assert body["status"] == "warning"


async def test_unconfigured_sumup_is_not_a_warning(client, auth_headers, monkeypatch):
    """Sans TPE, la boutique encaisse en especes (D7) : ce n'est pas une
    anomalie a signaler, seulement une configuration."""
    await _fresh_backup()
    for name in ("SUMUP_API_KEY", "SUMUP_MERCHANT_CODE", "SUMUP_READER_ID"):
        monkeypatch.setattr(settings, name, "")
    body = await _get(client, auth_headers)
    assert body["external"]["sumup"]["configured"] is False
    assert body["external"]["sumup"]["reader_configured"] is False
    assert body["external"]["sumup"]["last_ping"] is None
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# Tampon des erreurs 500 (N5 -> N2)
# ---------------------------------------------------------------------------


async def test_recent_errors_filled_by_a_real_500(client, auth_headers, monkeypatch):
    original = monitoring.build_snapshot

    async def _boom(db, **kwargs):
        raise RuntimeError("panne simulee")

    monkeypatch.setattr(monitoring, "build_snapshot", _boom)
    failed = await client.get("/api/admin/monitoring", headers=auth_headers)
    assert failed.status_code == 500
    request_id = failed.headers["x-request-id"]
    monkeypatch.setattr(monitoring, "build_snapshot", original)

    body = await _get(client, auth_headers)
    assert len(body["recent_errors"]) == 1
    entry = body["recent_errors"][0]
    assert set(entry) == {"at", "request_id", "method", "path", "status", "error_type"}
    assert entry["request_id"] == request_id
    assert entry["method"] == "GET"
    assert entry["path"] == "/api/admin/monitoring"
    assert entry["status"] == 500
    assert entry["error_type"] == "RuntimeError"
    # Une erreur dans l'heure fait passer l'etat global en « warning ».
    assert body["status"] == "warning"


def test_recent_errors_buffer_is_capped_at_fifty():
    middleware_mod.reset_recent_errors()
    for i in range(55):
        middleware_mod.record_recent_error(
            request_id=f"id{i:04d}",
            method="GET",
            path="/api/pos/transactions",
            status=500,
            error_type="RuntimeError",
        )
    buffered = middleware_mod.recent_errors()
    assert len(buffered) == middleware_mod.RECENT_ERRORS_MAX == 50
    # La plus recente d'abord, les cinq premieres oubliees.
    assert buffered[0]["request_id"] == "id0054"
    assert buffered[-1]["request_id"] == "id0005"


def test_recent_errors_never_carry_a_body():
    """Le tampon est relu par une route d'administration : il ne doit porter
    que des metadonnees techniques, jamais le contenu d'une requete."""
    middleware_mod.reset_recent_errors()
    middleware_mod.record_recent_error(
        request_id="abc", method="POST", path="/api/pos/transactions",
        status=500, error_type="ValueError",
    )
    assert set(middleware_mod.recent_errors()[0]) == {
        "at", "request_id", "method", "path", "status", "error_type",
    }


# ---------------------------------------------------------------------------
# Aucun secret dans la reponse
# ---------------------------------------------------------------------------


async def test_monitoring_leaks_no_secret(client, auth_headers, monkeypatch):
    secrets = {
        "SUMUP_API_KEY": "sup_sk_tres_secret_0123456789",
        "SUMUP_MERCHANT_CODE": "MSECRETCODE",
        "SUMUP_READER_ID": "rdr_secret_reader_id",
        "BREVO_API_KEY": "xkeysib-secret-brevo-key",
        "OPENWEATHER_API_KEY": "openweather-secret-key",
    }
    for name, value in secrets.items():
        monkeypatch.setattr(settings, name, value)

    body = await _get(client, auth_headers, check=1)
    dumped = json.dumps(body, ensure_ascii=False)
    for value in secrets.values():
        assert value not in dumped
    # Les deux cles de signature ne sont pas remplacees (les remplacer
    # invaliderait le jeton et la chaine du JET pendant le test) : on
    # verifie qu'elles ne fuient pas telles qu'elles sont configurees.
    assert settings.SECRET_KEY not in dumped
    assert settings.FISCAL_SIGNING_KEY not in dumped
    # Meme pas un fragment : une valeur masquee reste une valeur.
    assert "sup_sk" not in dumped
    assert "xkeysib" not in dumped
    # Seuls des booleens de configuration sont exposes.
    assert body["external"]["brevo"] == {"configured": True}
    assert body["external"]["sumup"]["configured"] is True


# ---------------------------------------------------------------------------
# Lecture seule
# ---------------------------------------------------------------------------


async def test_monitoring_get_writes_nothing(client, auth_headers):
    from sqlalchemy import func, select

    from app.models.jet import JournalEvent

    async def _count() -> int:
        async with async_session() as db:
            return (await db.execute(select(func.count(JournalEvent.id)))).scalar_one()

    before = await _count()
    await _get(client, auth_headers)
    await _get(client, auth_headers)
    assert await _count() == before


async def test_openweather_cache_age_is_null_without_call(client, auth_headers):
    body = await _get(client, auth_headers)
    assert body["external"]["openweather"]["cache_age_seconds"] is None


async def test_backups_block_reports_disabled_nightly(client, auth_headers):
    r = await client.put(
        "/api/admin/database/config",
        json={"nightly_enabled": False},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = await _get(client, auth_headers)
    assert body["backups"]["nightly_enabled"] is False


async def test_clients_deletion_due_is_counted(client, auth_headers):
    from app.models.client import Client

    async with async_session() as db:
        db.add(
            Client(
                email=f"{uuid.uuid4().hex}@exemple.test",
                deletion_scheduled_for=datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
        await db.commit()

    body = await _get(client, auth_headers)
    assert body["queues"]["clients_deletion_due"] == 1
