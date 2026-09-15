# Nouveau test (PR5, docs/ARCHITECTURE_PR5.md §3) — sauvegarde applicative
# de la base : dump reel via `pg_dump` (disponible en CI, postgresql-client),
# gzip valide + empreinte sha256, ligne `failed` quand `pg_dump` est
# introuvable, purge par retention, fichier disparu -> `missing` au listing,
# routes admin (401 sans JWT, cycle run/list/download/delete, 409 concurrent,
# 422 bornes de retention).
from __future__ import annotations

import gzip
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

import app.services.database_backup as backup_svc
from app.core.database import async_session
from app.models.database_backup import BackupStatus, BackupTrigger, DatabaseBackup
from app.models.jet import JournalEvent

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _backup_dir(tmp_path, monkeypatch):
    """Toutes les sauvegardes de ce module ecrivent dans un `tmp_path` isole
    — jamais dans `data/backups` du depot."""
    monkeypatch.setattr(backup_svc.settings, "BACKUP_DIR", str(tmp_path))
    yield tmp_path


@pytest.fixture(autouse=True)
def _reset_lock():
    """Le verrou de processus est un singleton de module — s'assure qu'un
    test qui simule un « busy » ne pollue pas les suivants."""
    yield
    if backup_svc._run_lock.locked():
        backup_svc._run_lock.release()


# ---------------------------------------------------------------------------
# Service — dump reel
# ---------------------------------------------------------------------------


async def test_run_backup_creates_valid_gzip_with_sha256(_backup_dir):
    async with async_session() as db:
        backup = await backup_svc.run_backup(db, trigger="manual")

    assert backup.status == BackupStatus.success
    assert backup.trigger == BackupTrigger.manual
    assert backup.filename.startswith("fripco_") and backup.filename.endswith(".sql.gz")
    assert backup.size_bytes and backup.size_bytes > 0
    assert backup.sha256 and len(backup.sha256) == 64
    assert backup.error is None
    assert backup.duration_ms is not None and backup.duration_ms >= 0

    path = _backup_dir / backup.filename
    assert path.exists()
    assert backup_svc._sha256_of(path) == backup.sha256

    with gzip.open(path, "rb") as gz:
        content = gz.read()
    assert b"CREATE TABLE public.journal_events" in content


async def test_run_backup_pg_dump_missing_records_failed_row_and_raises(monkeypatch):
    monkeypatch.setattr(backup_svc, "_PG_DUMP_BIN", "pg_dump_does_not_exist_at_all")

    async with async_session() as db:
        with pytest.raises(backup_svc.BackupError):
            await backup_svc.run_backup(db, trigger="manual")

    async with async_session() as db:
        rows = (await db.execute(select(DatabaseBackup))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == BackupStatus.failed
    assert rows[0].error
    assert "introuvable" in rows[0].error


async def test_run_backup_rejects_dump_missing_journal_events(monkeypatch, _backup_dir):
    """Un dump gzip valide mais sans la table `journal_events` (base
    incorrecte ou dump tronque) est rejeté — jamais un dump partiel offert
    au téléchargement."""

    async def _fake_pg_dump(dest_tmp: Path) -> None:
        with gzip.open(dest_tmp, "wb") as gz:
            gz.write(b"-- dump sans le schema attendu\n")

    monkeypatch.setattr(backup_svc, "_run_pg_dump", _fake_pg_dump)

    async with async_session() as db:
        with pytest.raises(backup_svc.BackupError):
            await backup_svc.run_backup(db, trigger="manual")

    async with async_session() as db:
        rows = (await db.execute(select(DatabaseBackup))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == BackupStatus.failed
    assert "journal_events" in rows[0].error
    # Le fichier temporaire ne doit pas rester sur le disque après l'échec.
    assert list(_backup_dir.iterdir()) == []


async def test_run_backup_busy_when_lock_held(_backup_dir):
    await backup_svc._run_lock.acquire()
    try:
        async with async_session() as db:
            with pytest.raises(backup_svc.BackupBusyError):
                await backup_svc.run_backup(db, trigger="manual")
    finally:
        backup_svc._run_lock.release()


# ---------------------------------------------------------------------------
# Retention / etat missing
# ---------------------------------------------------------------------------


async def test_apply_retention_deletes_old_file_and_row(_backup_dir):
    async with async_session() as db:
        backup = await backup_svc.run_backup(db, trigger="manual")
        # Fait vieillir artificiellement la ligne (au-dela de la retention).
        row = (
            await db.execute(select(DatabaseBackup).where(DatabaseBackup.id == backup.id))
        ).scalar_one()
        row.created_at = datetime.now(timezone.utc) - timedelta(days=100)
        await db.commit()

        purged = await backup_svc.apply_retention(db, retention_days=60)

    assert purged == 1
    assert not (_backup_dir / backup.filename).exists()
    async with async_session() as db:
        rows = (await db.execute(select(DatabaseBackup))).scalars().all()
    assert rows == []


async def test_apply_retention_keeps_recent_backups(_backup_dir):
    async with async_session() as db:
        backup = await backup_svc.run_backup(db, trigger="manual")
        purged = await backup_svc.apply_retention(db, retention_days=60)
    assert purged == 0
    assert (_backup_dir / backup.filename).exists()


async def test_deleted_file_marked_missing_on_listing(_backup_dir):
    async with async_session() as db:
        backup = await backup_svc.run_backup(db, trigger="manual")

    (_backup_dir / backup.filename).unlink()  # suppression manuelle

    async with async_session() as db:
        rows = await backup_svc.list_backups(db)
    assert len(rows) == 1
    assert rows[0].status == BackupStatus.missing

    # Jamais recree automatiquement : re-lister ne fait pas reapparaitre le fichier.
    async with async_session() as db:
        rows2 = await backup_svc.list_backups(db)
    assert rows2[0].status == BackupStatus.missing
    assert not (_backup_dir / backup.filename).exists()


# ---------------------------------------------------------------------------
# Routes admin — /api/admin/database/*
# ---------------------------------------------------------------------------


async def test_database_routes_require_auth(client):
    backup_id = str(uuid.uuid4())
    endpoints = [
        ("GET", "/api/admin/database/state"),
        ("GET", "/api/admin/database/config"),
        ("PUT", "/api/admin/database/config"),
        ("GET", "/api/admin/database/backups"),
        ("POST", "/api/admin/database/backups/run"),
        ("GET", f"/api/admin/database/backups/{backup_id}/download"),
        ("DELETE", f"/api/admin/database/backups/{backup_id}"),
    ]
    for method, url in endpoints:
        resp = await client.request(method, url, json={} if method == "PUT" else None)
        assert resp.status_code == 401, f"{method} {url} -> {resp.status_code}"


async def test_get_backup_config_defaults(client, auth_headers):
    r = await client.get("/api/admin/database/config", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["retention_days"] == 60
    assert body["nightly_enabled"] is True
    assert body["alert_email"] == ""


async def test_put_backup_config_rejects_retention_below_minimum(client, auth_headers):
    r = await client.put(
        "/api/admin/database/config",
        json={"retention_days": 3, "nightly_enabled": True, "alert_email": ""},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_setting"


async def test_put_backup_config_persists_and_journals(client, auth_headers):
    r = await client.put(
        "/api/admin/database/config",
        json={"retention_days": 90, "nightly_enabled": False, "alert_email": "alerte@fripco-street.fr"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["retention_days"] == 90
    assert r.json()["nightly_enabled"] is False

    r2 = await client.get("/api/admin/database/config", headers=auth_headers)
    assert r2.json()["retention_days"] == 90

    async with async_session() as db:
        events = (
            await db.execute(select(JournalEvent).where(JournalEvent.event_type == "config.changed"))
        ).scalars().all()
    assert any(e.payload.get("key") == "backup" for e in events)


async def test_run_list_download_delete_cycle(client, auth_headers, _backup_dir):
    run_resp = await client.post("/api/admin/database/backups/run", headers=auth_headers)
    assert run_resp.status_code == 201, run_resp.text
    body = run_resp.json()
    assert body["status"] == "success"
    backup_id = body["id"]

    list_resp = await client.get("/api/admin/database/backups", headers=auth_headers)
    assert list_resp.status_code == 200
    assert any(b["id"] == backup_id for b in list_resp.json()["backups"])

    state_resp = await client.get("/api/admin/database/state", headers=auth_headers)
    assert state_resp.status_code == 200
    state = state_resp.json()
    assert state["last_backup"]["id"] == backup_id
    assert isinstance(state["tables"], list) and len(state["tables"]) > 0
    assert any(t["name"] == "journal_events" for t in state["tables"])

    download_resp = await client.get(
        f"/api/admin/database/backups/{backup_id}/download", headers=auth_headers
    )
    assert download_resp.status_code == 200
    assert download_resp.headers["content-type"].startswith("application/gzip")
    assert download_resp.headers["x-backup-sha256"] == body["sha256"]
    on_disk = (_backup_dir / body["filename"]).read_bytes()
    # Le fichier est encore present au moment du telechargement (verifie
    # avant la suppression ci-dessous) — octets exacts.
    assert download_resp.content == on_disk

    async with async_session() as db:
        events = (
            await db.execute(
                select(JournalEvent).where(JournalEvent.event_type == "export.downloaded")
            )
        ).scalars().all()
    assert any(e.payload.get("kind") == "database_backup" for e in events)

    delete_resp = await client.delete(
        f"/api/admin/database/backups/{backup_id}", headers=auth_headers
    )
    assert delete_resp.status_code == 200
    assert delete_resp.json()["deleted"] is True
    assert not (_backup_dir / body["filename"]).exists()

    async with async_session() as db:
        deleted_events = (
            await db.execute(select(JournalEvent).where(JournalEvent.event_type == "backup.deleted"))
        ).scalars().all()
    assert any(e.payload.get("backup_id") == backup_id for e in deleted_events)

    list_resp2 = await client.get("/api/admin/database/backups", headers=auth_headers)
    assert all(b["id"] != backup_id for b in list_resp2.json()["backups"])


async def test_run_backup_returns_409_when_already_running(client, auth_headers, _backup_dir):
    await backup_svc._run_lock.acquire()
    try:
        resp = await client.post("/api/admin/database/backups/run", headers=auth_headers)
        assert resp.status_code == 409
        assert resp.json()["code"] == "backup_running"
    finally:
        backup_svc._run_lock.release()


async def test_download_unknown_backup_404(client, auth_headers):
    resp = await client.get(
        f"/api/admin/database/backups/{uuid.uuid4()}/download", headers=auth_headers
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


async def test_delete_unknown_backup_404(client, auth_headers):
    resp = await client.delete(f"/api/admin/database/backups/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404
