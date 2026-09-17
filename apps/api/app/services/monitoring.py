# Nouveau service (PR12, docs/ARCHITECTURE_PR12.md §1, N2) — photographie
# technique de l'installation pour l'ecran de supervision.
#
# Trois principes :
#
# 1. **Lecture seule.** Une page de diagnostic ne doit jamais modifier ce
#    qu'elle observe : aucune ecriture en base ici, a la seule exception de
#    l'evenement JET `fiscal.integrity_checked` deja existant, ecrit
#    uniquement quand une verification d'integrite est EXPLICITEMENT demandee
#    (`?check=1` ou `POST /monitoring/check`).
# 2. **Jamais de secret.** On n'expose que des booleens « configure ou non »
#    (cle SumUp, cle Brevo, cle meteo) — jamais une valeur, meme masquee.
# 3. **Jamais bloquant.** Chaque bloc est protege : une base injoignable, un
#    dossier de sauvegarde absent ou une imprimante debranchee donnent un
#    champ `null` ou `ok: false`, jamais une 500. C'est precisement quand
#    tout va mal que cette page doit repondre.
#
# Le calcul d'integrite (recalcul complet des chaines HMAC) est cher : il
# n'est donc jamais fait au fil du rafraichissement automatique (60 s), mais
# a la demande, et son resultat est garde 10 minutes en memoire de processus.
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.middleware import recent_errors
from app.jobs import list_registered_jobs
from app.models.client import Client
from app.models.database_backup import BackupStatus, DatabaseBackup
from app.models.failed_payment import FailedPayment, FailedPaymentStatus
from app.models.jet import JournalEvent
from app.models.sumup_exchange import SumUpExchange
from app.services import database_backup as database_backup_service
from app.services import weather
from app.services.fiscal import FiscalService
from app.services.fiscal_closure import FiscalClosureService
from app.services.jet import EVENT_FISCAL_INTEGRITY_CHECKED, JournalService
from app.services.settings_service import SettingsService
from app.services.sumup_service import SumUpService, last_reader_ping
from app.version import APP_VERSION, EXPECTED_DB_REVISION

logger = logging.getLogger("fripco.monitoring")

# Duree de vie du cache d'integrite (§1 N2 : « dernier resultat en cache
# memoire <= 10 min »).
INTEGRITY_CACHE_TTL_SECONDS = 10 * 60

# Une sauvegarde est « perimee » passe 36 h : la sauvegarde est nocturne
# (03:00), 36 h laisse donc passer une nuit ratee ponctuelle sans crier au
# loup, mais pas deux.
BACKUP_STALE_AFTER_SECONDS = 36 * 3600

# Fenetre des erreurs 500 qui font passer l'etat global en « warning ».
RECENT_ERROR_WINDOW_SECONDS = 3600

# Instant de demarrage du processus : `time.monotonic` pour la duree (jamais
# faussee par un reglage d'horloge), pas `time.time`.
_STARTED_MONOTONIC = time.monotonic()

# Cache memoire du dernier calcul d'integrite : `(instant_monotone, charge)`.
_integrity_cache: tuple[float, dict] | None = None


def reset_integrity_cache() -> None:
    """Oublie le dernier calcul d'integrite — tests, ou apres une operation
    qui invaliderait la photo (remise a zero)."""
    global _integrity_cache
    _integrity_cache = None


def cached_integrity() -> dict | None:
    """Dernier resultat d'integrite s'il a moins de 10 minutes, sinon `None`
    (le front affiche alors « jamais verifie »)."""
    if _integrity_cache is None:
        return None
    computed_at, payload = _integrity_cache
    if time.monotonic() - computed_at > INTEGRITY_CACHE_TTL_SECONDS:
        return None
    return payload


async def compute_integrity(db: AsyncSession, *, user_id=None) -> dict:
    """Recalcule les trois chaines (JET, fiscale, clotures) et met en cache.

    Ecrit l'evenement JET `fiscal.integrity_checked` deja utilise par
    `GET /admin/fiscal/integrity` : une verification demandee est un acte
    d'exploitation, elle laisse une trace. C'est la SEULE ecriture de tout
    ce module.
    """
    global _integrity_cache

    jet_result = await JournalService(db).verify_chain()
    transactions_result = await FiscalService(db).verify_chain_integrity()
    z_result = await FiscalService(db).verify_z_chain_integrity()
    closures_result = await FiscalClosureService(db).verify_chain()

    checked_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "jet": {
            "valid": bool(jet_result.get("valid")),
            "count": int(jet_result.get("count") or 0),
            "checked_at": checked_at,
        },
        # « fiscal » = la chaine des ventes ET celle des Z : pour l'exploitant
        # c'est un seul et meme sujet (« mes ventes sont-elles intactes ? »).
        "fiscal": {
            "valid": bool(transactions_result.get("valid")) and bool(z_result.get("valid")),
            "checked": int(transactions_result.get("checked") or 0)
            + int(z_result.get("checked") or 0),
        },
        "closures": {
            "valid": bool(closures_result.get("valid")),
            "checked": int(closures_result.get("checked") or 0),
        },
    }

    await JournalService(db).record(
        EVENT_FISCAL_INTEGRITY_CHECKED,
        user_id=user_id,
        payload={
            "source": "monitoring",
            "jet_valid": payload["jet"]["valid"],
            "transactions_valid": bool(transactions_result.get("valid")),
            "z_reports_valid": bool(z_result.get("valid")),
            "closures_valid": payload["closures"]["valid"],
        },
    )
    await db.commit()

    _integrity_cache = (time.monotonic(), payload)
    return payload


# ---------------------------------------------------------------------------
# Blocs de la photographie — chacun tolere sa propre panne.
# ---------------------------------------------------------------------------


async def _current_db_revision(db: AsyncSession) -> str | None:
    try:
        return (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — cf. en-tete du module
        logger.exception("Lecture de la revision Alembic impossible")
        return None


async def _database_block(db: AsyncSession) -> dict:
    started = time.monotonic()
    try:
        await db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        logger.exception("Base injoignable depuis la supervision")
        return {"ok": False, "latency_ms": None, "size_bytes": None, "tables_count": None}
    latency_ms = round((time.monotonic() - started) * 1000, 1)

    size_bytes: int | None = None
    tables_count: int | None = None
    try:
        size_bytes = (
            await db.execute(text("SELECT pg_database_size(current_database())"))
        ).scalar_one_or_none()
        tables_count = (
            await db.execute(text("SELECT count(*) FROM pg_stat_user_tables"))
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        logger.exception("Volumetrie de la base illisible")

    return {
        "ok": True,
        "latency_ms": latency_ms,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
        "tables_count": int(tables_count) if tables_count is not None else None,
    }


async def _backups_block(db: AsyncSession) -> dict:
    last_payload: dict | None = None
    stale = True
    try:
        last = (
            await db.execute(
                select(DatabaseBackup).order_by(DatabaseBackup.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if last is not None:
            last_payload = {
                "created_at": last.created_at.isoformat() if last.created_at else None,
                "status": last.status.value,
                "size_bytes": last.size_bytes,
            }
        last_success = (
            await db.execute(
                select(DatabaseBackup)
                .where(DatabaseBackup.status == BackupStatus.success)
                .order_by(DatabaseBackup.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        # Aucune sauvegarde reussie du tout = perime : une installation sans
        # sauvegarde est exactement la situation que cet indicateur existe
        # pour signaler.
        if last_success is not None and last_success.created_at is not None:
            age = (datetime.now(timezone.utc) - last_success.created_at).total_seconds()
            stale = age > BACKUP_STALE_AFTER_SECONDS
    except Exception:  # noqa: BLE001
        logger.exception("Lecture des sauvegardes impossible")

    nightly_enabled = True
    try:
        backup_settings = await SettingsService(db).get("backup")
        nightly_enabled = bool(backup_settings.get("nightly_enabled", True))
    except Exception:  # noqa: BLE001
        logger.exception("Lecture du reglage de sauvegarde impossible")

    try:
        dir_free_bytes: int | None = shutil.disk_usage(
            database_backup_service.backup_dir()
        ).free
    except OSError:
        dir_free_bytes = None

    return {
        "last": last_payload,
        "nightly_enabled": nightly_enabled,
        "dir_free_bytes": dir_free_bytes,
        "stale": stale,
    }


async def _jobs_block(db: AsyncSession) -> list[dict]:
    """Taches planifiees + derniere trace JET `system.job_*` de chacune."""
    jobs = list_registered_jobs()

    last_runs: dict[str, dict] = {}
    try:
        rows = (
            await db.execute(
                select(JournalEvent)
                .where(JournalEvent.event_type.like("system.job%"))
                .order_by(JournalEvent.seq.desc())
                .limit(200)
            )
        ).scalars().all()
        for event in rows:
            payload = event.payload or {}
            name = payload.get("job")
            if not isinstance(name, str) or name in last_runs:
                continue
            failed = event.event_type.endswith("_failed")
            detail = payload.get("error") if failed else payload.get("detail")
            last_runs[name] = {
                "at": event.created_at.isoformat() if event.created_at else None,
                "status": "failed" if failed else "ok",
                # Tronque : un message d'erreur SQL peut faire plusieurs
                # kilo-octets, l'ecran n'en affiche qu'un resume.
                "detail": str(detail)[:300] if detail else None,
            }
    except Exception:  # noqa: BLE001
        logger.exception("Lecture des traces de jobs impossible")

    for job in jobs:
        job["last_run"] = last_runs.get(job["name"])
    return jobs


def _external_block() -> dict:
    """Etat des services tiers — booleens de configuration uniquement.

    Aucun appel sortant : la supervision ne doit ni ralentir ni marteler
    SumUp, Brevo ou OpenWeather. Le TPE est rapporte via le dernier ping
    memorise par la caisse (`sumup_service.last_reader_ping`).
    """
    service = SumUpService()
    described = service.describe()
    return {
        "sumup": {
            "configured": bool(described.get("configured")),
            "reader_configured": bool(described.get("reader_id_set")),
            "last_ping": last_reader_ping(),
        },
        "brevo": {"configured": bool((settings.BREVO_API_KEY or "").strip())},
        "openweather": {
            "configured": weather.api_key_configured(),
            "cache_age_seconds": weather.cache_age_seconds(),
        },
    }


async def _printer_block(db: AsyncSession) -> dict:
    """Etat de l'imprimante ticket — meme sonde que `/hardware/printer/status`.

    Hors mode reseau (USB-OTG sur la tablette, ou aucune imprimante), le
    serveur n'a aucun moyen de savoir : `online` reste `null` plutot que
    `false`, qui laisserait croire a une panne.
    """
    try:
        from app.services import escpos_service

        hardware = await SettingsService(db).get("hardware")
        mode = hardware.get("printer_mode", "none")
        if mode != "network":
            return {"mode": mode, "online": None, "latency_ms": None}
        status = await escpos_service.ping_printer(
            hardware.get("printer_host") or "",
            int(hardware.get("printer_port") or escpos_service.DEFAULT_PORT),
        )
        return {"mode": mode, "online": status.online, "latency_ms": status.latency_ms}
    except Exception:  # noqa: BLE001
        logger.exception("Sonde imprimante impossible depuis la supervision")
        return {"mode": "unknown", "online": None, "latency_ms": None}


async def _queues_block(db: AsyncSession) -> dict:
    """Files d'attente d'exploitation — ce qui reste a traiter a la main."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    now = datetime.now(timezone.utc)
    try:
        failed_payments_pending = (
            await db.execute(
                select(func.count(FailedPayment.id)).where(
                    FailedPayment.status == FailedPaymentStatus.pending
                )
            )
        ).scalar_one()
        sumup_exchange_errors_24h = (
            await db.execute(
                select(func.count(SumUpExchange.id)).where(
                    SumUpExchange.is_error.is_(True),
                    SumUpExchange.created_at >= since,
                )
            )
        ).scalar_one()
        clients_deletion_due = (
            await db.execute(
                select(func.count(Client.id)).where(
                    Client.deletion_scheduled_for.is_not(None),
                    Client.deletion_scheduled_for <= now,
                    Client.anonymized_at.is_(None),
                )
            )
        ).scalar_one()
    except Exception:  # noqa: BLE001
        logger.exception("Lecture des files d'attente impossible")
        return {
            "failed_payments_pending": None,
            "sumup_exchange_errors_24h": None,
            "clients_deletion_due": None,
        }
    return {
        "failed_payments_pending": int(failed_payments_pending),
        "sumup_exchange_errors_24h": int(sumup_exchange_errors_24h),
        "clients_deletion_due": int(clients_deletion_due),
    }


def _recent_errors_last_hour(errors: list[dict]) -> int:
    limit = datetime.now(timezone.utc) - timedelta(seconds=RECENT_ERROR_WINDOW_SECONDS)
    count = 0
    for item in errors:
        try:
            at = datetime.fromisoformat(item["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if at >= limit:
            count += 1
    return count


def _overall_status(snapshot: dict[str, Any]) -> str:
    """`critical` > `warning` > `ok` (§1 N2).

    `critical` = la caisse ne peut pas etre tenue pour fiable (base KO,
    schema qui ne correspond pas au code, chaine d'integrite cassee).
    `warning` = elle fonctionne, mais quelque chose demande une action du
    manager dans la journee.
    """
    database = snapshot["database"]
    app_block = snapshot["app"]
    integrity = snapshot["integrity"]

    if not database["ok"] or not app_block["db_revision_ok"]:
        return "critical"
    if integrity is not None:
        for part in ("jet", "fiscal", "closures"):
            if not integrity[part]["valid"]:
                return "critical"

    if snapshot["backups"]["stale"]:
        return "warning"

    printer = snapshot["printer"]
    if printer["online"] is False:
        return "warning"

    sumup = snapshot["external"]["sumup"]
    # Un TPE non configure n'est pas une anomalie (D7 : sans TPE, especes
    # uniquement) — seul un TPE configure mais pas pret merite l'alerte.
    if sumup["configured"] and sumup["last_ping"] and not sumup["last_ping"]["ready"]:
        return "warning"

    queues = snapshot["queues"]
    if queues["failed_payments_pending"]:
        return "warning"

    if _recent_errors_last_hour(snapshot["recent_errors"]):
        return "warning"

    return "ok"


async def build_snapshot(
    db: AsyncSession, *, check: bool = False, user_id=None
) -> dict:
    """Photographie complete de l'installation (§1 N2).

    `check=True` recalcule les integrites (et laisse la trace JET) ; sinon on
    se contente du dernier resultat en cache, ou `null`.
    """
    current_revision = await _current_db_revision(db)
    database = await _database_block(db)

    if check:
        integrity = await compute_integrity(db, user_id=user_id)
    else:
        integrity = cached_integrity()

    errors = recent_errors()

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app": {
            "version": APP_VERSION,
            "build_sha": settings.build_sha,
            "build_date": settings.build_date,
            "environment": settings.ENVIRONMENT,
            "expected_db_revision": EXPECTED_DB_REVISION,
            "current_db_revision": current_revision,
            "db_revision_ok": current_revision == EXPECTED_DB_REVISION,
            "uptime_seconds": int(time.monotonic() - _STARTED_MONOTONIC),
        },
        "database": database,
        "backups": await _backups_block(db),
        "jobs": await _jobs_block(db),
        "integrity": integrity,
        "external": _external_block(),
        "printer": await _printer_block(db),
        "queues": await _queues_block(db),
        "recent_errors": errors,
    }
    snapshot["status"] = _overall_status(snapshot)
    return snapshot
