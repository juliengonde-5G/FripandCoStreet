# Nouveau service (PR5, docs/ARCHITECTURE_PR5.md §1, G1) — sauvegarde
# applicative planifiee de la base : dump `pg_dump | gzip` en sous-processus
# asynchrone (jamais `shell=True`, mot de passe uniquement dans
# l'environnement du sous-processus via `PGPASSWORD`), ecriture en fichier
# temporaire puis `rename` atomique, verification gzip complete + presence de
# `CREATE TABLE public.journal_events`, empreinte sha256, ligne
# `DatabaseBackup` ecrite dans TOUS les cas (succes/echec), purge par
# retention. Table d'exploitation (pas de trigger d'immuabilite, migration
# 0006) : contrairement au JET (`app/services/jet.py`), une exception ici
# n'a pas a etre "jamais avalee" au sens fiscal — mais le contrat impose
# neanmoins qu'aucun echec ne disparaisse silencieusement (une ligne `failed`
# est toujours ecrite, et `BackupError` est toujours remontee a l'appelant).
from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import os
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.database_backup import BackupStatus, BackupTrigger, DatabaseBackup

logger = logging.getLogger("fripco")

_CHUNK = 64 * 1024
# Chaine attestant qu'un dump couvre bien le schema fiscal (§G1) — le JET
# est la table dont l'absence trahirait le plus surement un dump partiel/
# corrompu (pg_dump interrompu, mauvaise base ciblee...).
_SCHEMA_MARKER = b"CREATE TABLE public.journal_events"

# Nom du binaire — variable de module (plutot que la chaine litterale en
# dur) pour que les tests puissent simuler « pg_dump introuvable » sans
# toucher au PATH du process (voir tests/test_database_backup.py).
_PG_DUMP_BIN = "pg_dump"

# Verrou de PROCESSUS (mono-instance d'API, comme les autres verrous "simples"
# du contrat, cf. G5) — empeche deux sauvegardes de tourner en meme temps
# (cron nocturne + declenchement manuel, ou double-clic).
_run_lock = asyncio.Lock()


class BackupError(Exception):
    """`pg_dump` a echoue ou le fichier produit est invalide (gzip/schema)."""


class BackupBusyError(Exception):
    """Une sauvegarde est deja en cours (verrou de processus, G5 409)."""


def backup_dir() -> Path:
    """Dossier des dumps (`BACKUP_DIR`, defaut `data/backups`) — cree si absent."""
    d = Path(settings.BACKUP_DIR).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dump_source_url() -> str:
    """URL de connexion pour `pg_dump` — proprietaire si disponible (G1),
    sinon le role applicatif (`SELECT` sur toutes les tables/sequences,
    suffisant pour un dump)."""
    return (settings.MIGRATION_DATABASE_URL or settings.DATABASE_URL or "").strip()


async def _run_pg_dump(dest_tmp: Path) -> None:
    """Lance `pg_dump` en sous-processus asynchrone, ecrit sa sortie
    gzippee en flux vers `dest_tmp`. Leve `BackupError` si le binaire est
    absent ou si le processus se termine en erreur — jamais `shell=True`,
    le mot de passe ne passe que par `PGPASSWORD` dans l'environnement du
    sous-processus (jamais en argument de ligne de commande, jamais loggue)."""
    parsed = urlparse(_dump_source_url())
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    cmd = [
        _PG_DUMP_BIN,
        "-h", parsed.hostname or "localhost",
        "-p", str(parsed.port or 5432),
        "-U", unquote(parsed.username or "postgres"),
        "-d", (parsed.path or "/postgres").lstrip("/"),
        "--no-owner",
        "--no-privileges",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise BackupError(
            "pg_dump introuvable — installez postgresql-client (voir docker/Dockerfile.api)."
        ) from exc

    assert proc.stdout is not None
    with gzip.open(dest_tmp, "wb") as gz:
        while True:
            chunk = await proc.stdout.read(_CHUNK)
            if not chunk:
                break
            gz.write(chunk)
    stderr = await proc.stderr.read() if proc.stderr else b""
    returncode = await proc.wait()
    if returncode != 0:
        # Jamais l'URL de connexion (mot de passe) dans le message — seul le
        # flux stderr de pg_dump est repris, tronque.
        detail = stderr.decode("utf-8", "replace").strip()[:500]
        raise BackupError(f"pg_dump a échoué (code {returncode}) : {detail}")


def _verify_gzip_and_schema(path: Path) -> None:
    """Relit l'integralite du fichier gzip (equivalent `gunzip -t`) et
    verifie la presence de `_SCHEMA_MARKER` dans le contenu decompresse —
    detecte a la fois un fichier corrompu et un dump partiel/tronque."""
    found = False
    overlap = b""
    try:
        with gzip.open(path, "rb") as gz:
            while True:
                chunk = gz.read(_CHUNK)
                if not chunk:
                    break
                if not found and _SCHEMA_MARKER in (overlap + chunk):
                    found = True
                # Conserve un petit chevauchement pour ne pas rater le
                # marqueur s'il est coupe entre deux blocs de lecture.
                overlap = chunk[-(len(_SCHEMA_MARKER) - 1):]
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise BackupError(f"Fichier de sauvegarde gzip invalide : {exc}") from exc
    if not found:
        raise BackupError(
            "Le dump ne contient pas la table journal_events — sauvegarde rejetée "
            "(dump partiel ou base incorrecte)."
        )


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Execution d'une sauvegarde
# ---------------------------------------------------------------------------


async def run_backup(
    db: AsyncSession,
    *,
    trigger: str,
    user_id: uuid.UUID | None = None,
) -> DatabaseBackup:
    """Execute une sauvegarde complete et retourne la ligne `DatabaseBackup`.

    Une ligne est ecrite dans TOUS les cas (succes ou echec, G1). En cas
    d'echec (pg_dump absent, code de retour non nul, fichier gzip/schema
    invalide), la ligne `failed` est tout de meme committee puis
    `BackupError` est levee vers l'appelant — a lui de decider (route HTTP,
    cron) comment reagir.
    """
    if trigger not in ("nightly", "manual"):
        raise ValueError(f"trigger invalide : {trigger!r}")
    if _run_lock.locked():
        raise BackupBusyError("Une sauvegarde est déjà en cours.")
    async with _run_lock:
        return await _do_run_backup(db, trigger=trigger, user_id=user_id)


async def _do_run_backup(
    db: AsyncSession, *, trigger: str, user_id: uuid.UUID | None
) -> DatabaseBackup:
    ts = datetime.now(timezone.utc)
    directory = backup_dir()
    filename = f"fripco_{ts.strftime('%Y%m%d_%H%M%S')}.sql.gz"
    dest = directory / filename
    # Fichier temporaire sur le MEME dossier que la destination : le
    # `rename` final doit rester atomique (meme systeme de fichiers, G1).
    tmp = directory / f".{filename}.{uuid.uuid4().hex[:8]}.tmp"

    backup = DatabaseBackup(
        trigger=BackupTrigger(trigger),
        status=BackupStatus.failed,
        filename=filename,
        triggered_by_user_id=user_id,
    )

    started = time.monotonic()
    try:
        await _run_pg_dump(tmp)
        _verify_gzip_and_schema(tmp)
        sha256 = _sha256_of(tmp)
        size_bytes = tmp.stat().st_size
        os.replace(tmp, dest)  # rename atomique
        backup.status = BackupStatus.success
        backup.size_bytes = size_bytes
        backup.sha256 = sha256
        logger.info("Sauvegarde base de données OK : %s (%d octets, %s)", filename, size_bytes, trigger)
    except Exception as exc:  # noqa: BLE001 — toute erreur -> ligne failed, jamais avalee
        logger.exception("Sauvegarde base de données échouée (%s) : %s", trigger, exc)
        backup.status = BackupStatus.failed
        backup.error = str(exc)[:500]
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    finally:
        backup.finished_at = datetime.now(timezone.utc)
        backup.duration_ms = int((time.monotonic() - started) * 1000)

    db.add(backup)
    await db.flush()
    await db.commit()

    # Purge best-effort : ne doit jamais masquer le resultat de CETTE
    # sauvegarde (deja committee ci-dessus).
    try:
        await apply_retention(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Purge des sauvegardes en échec : %s", exc)

    if backup.status == BackupStatus.failed:
        raise BackupError(backup.error or "Sauvegarde échouée.")
    return backup


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def apply_retention(db: AsyncSession, retention_days: int | None = None) -> int:
    """Supprime fichiers + lignes plus vieux que `retention_days`. Retourne
    le nombre de sauvegardes purgees."""
    if retention_days is None:
        from app.services.settings_service import SettingsService

        backup_settings = await SettingsService(db).get("backup")
        retention_days = int(backup_settings.get("retention_days", 60))
    if retention_days < 1:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    rows = (
        await db.execute(select(DatabaseBackup).where(DatabaseBackup.created_at < cutoff))
    ).scalars().all()
    if not rows:
        return 0

    directory = backup_dir()
    n = 0
    for row in rows:
        try:
            p = directory / row.filename
            if p.exists():
                p.unlink()
        except OSError as exc:
            logger.warning("Suppression du fichier de sauvegarde %s impossible : %s", row.filename, exc)
        await db.delete(row)
        n += 1
    await db.flush()
    await db.commit()
    logger.info("Rétention sauvegardes : %d purgée(s) (> %d j)", n, retention_days)
    return n


# ---------------------------------------------------------------------------
# Lecture / etat
# ---------------------------------------------------------------------------


def serialize_backup(b: DatabaseBackup) -> dict:
    return {
        "id": str(b.id),
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "finished_at": b.finished_at.isoformat() if b.finished_at else None,
        "trigger": b.trigger.value,
        "status": b.status.value,
        "filename": b.filename,
        "size_bytes": b.size_bytes,
        "sha256": b.sha256,
        "duration_ms": b.duration_ms,
        "error": b.error,
        "triggered_by_user_id": str(b.triggered_by_user_id) if b.triggered_by_user_id else None,
    }


async def _reconcile_missing(db: AsyncSession, rows: list[DatabaseBackup]) -> None:
    """Marque `missing` toute ligne `success` dont le fichier a disparu du
    disque (purge manuelle, support externe deconnecte...) — jamais
    recree automatiquement (G1)."""
    directory = backup_dir()
    changed = False
    for row in rows:
        if row.status == BackupStatus.success and not (directory / row.filename).exists():
            row.status = BackupStatus.missing
            changed = True
    if changed:
        await db.flush()
        await db.commit()


async def list_backups(db: AsyncSession, *, limit: int = 50) -> list[DatabaseBackup]:
    rows = (
        await db.execute(
            select(DatabaseBackup).order_by(DatabaseBackup.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    await _reconcile_missing(db, list(rows))
    return list(rows)


async def get_backup(db: AsyncSession, backup_id: uuid.UUID) -> DatabaseBackup | None:
    row = (
        await db.execute(select(DatabaseBackup).where(DatabaseBackup.id == backup_id))
    ).scalar_one_or_none()
    if row is not None:
        await _reconcile_missing(db, [row])
    return row


async def database_state(db: AsyncSession) -> dict:
    """G5 : `GET /admin/database/state` — moteur, taille, volumes par table
    (via `pg_stat_user_tables.n_live_tup`, jamais de `COUNT(*)` sur toutes
    les tables), derniere sauvegarde, espace disque libre dans `BACKUP_DIR`."""
    engine_version = (await db.execute(text("SHOW server_version"))).scalar_one_or_none()
    database_size = (
        await db.execute(text("SELECT pg_database_size(current_database())"))
    ).scalar_one_or_none()
    table_rows = (
        await db.execute(
            text(
                "SELECT relname, n_live_tup FROM pg_stat_user_tables "
                "ORDER BY relname"
            )
        )
    ).all()
    tables = [{"name": r.relname, "rows_estimate": int(r.n_live_tup)} for r in table_rows]

    last = (
        await db.execute(select(DatabaseBackup).order_by(DatabaseBackup.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    if last is not None:
        await _reconcile_missing(db, [last])

    try:
        backup_dir_free_bytes = shutil.disk_usage(backup_dir()).free
    except OSError:
        backup_dir_free_bytes = None

    return {
        "engine_version": engine_version,
        "database_size_bytes": int(database_size) if database_size is not None else None,
        "tables": tables,
        "last_backup": serialize_backup(last) if last else None,
        "backup_dir_free_bytes": backup_dir_free_bytes,
    }
