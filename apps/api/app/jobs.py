# Extrait de l'application source (apps/api/app/jobs.py:648-652 — daily_fiscal_close_guard
# uniquement, §4.3 du contrat PR2). PR4 (docs/ARCHITECTURE_PR4.md §1/§3, F5)
# ajoute les clotures periodiques automatiques : mensuelle (1er du mois
# 00:15) et annuelle (1er janvier 00:30), Europe/Paris — meme regle
# "echec jamais avale en silence" (S-5) : JET `system.job_failed` **et**
# e-mail d'alerte best-effort a `shop.email` (jamais bloquant : un echec
# d'envoi de l'alerte ne doit pas empecher la journalisation JET, deja
# effectuee au moment ou l'e-mail est tente).
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from app.core.database import async_session
from app.services.fiscal import FiscalService
from app.services.jet import EVENT_SYSTEM_JOB_FAILED, JournalService

logger = logging.getLogger("fripco")

JOB_DAILY_FISCAL_CLOSE_GUARD = "daily_fiscal_close_guard"
JOB_MONTHLY_FISCAL_CLOSURE = "monthly_fiscal_closure"
JOB_ANNUAL_FISCAL_CLOSURE = "annual_fiscal_closure"


async def run_daily_fiscal_close_guard() -> None:
    """Ferme toute caisse oubliee a 23:59 Europe/Paris (§4.3).

    Un echec de ce job ne doit JAMAIS etre avale en silence (S-5, CLAUDE.md
    "Sécurité") : il est journalise au JET (`system.job_failed`) — un job
    fiscal qui echoue sans laisser de trace serait pire qu'un job qui
    echoue bruyamment.
    """
    try:
        async with async_session() as db:
            reports = await FiscalService(db).close_open_drawers(user_id=None)
            await db.commit()
            if reports:
                logger.warning(
                    "Garde fiscale 23:59 : %d caisse(s) fermée(s) automatiquement (Z=%s)",
                    len(reports),
                    [r.report_number for r in reports],
                )
    except Exception as exc:  # noqa: BLE001 — cf. docstring
        logger.exception("Garde fiscale 23:59 échouée : %s", exc)
        try:
            async with async_session() as db:
                await JournalService(db).record(
                    EVENT_SYSTEM_JOB_FAILED,
                    payload={"job": JOB_DAILY_FISCAL_CLOSE_GUARD, "error": str(exc)},
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Impossible de journaliser au JET l'échec de %s", JOB_DAILY_FISCAL_CLOSE_GUARD
            )


async def _alert_job_failure(
    job_name: str, exc: Exception, *, email_override: str | None = None
) -> None:
    """Journalise l'echec au JET (jamais avale, S-5) puis tente un e-mail
    d'alerte best-effort (un echec d'envoi ne remonte pas : la trace JET est
    deja ecrite, c'est elle la garantie).

    `email_override` (PR5, G4, docs/ARCHITECTURE_PR5.md §1) permet a un
    appelant de cibler un destinataire specifique (ex. `backup.alert_email`)
    avant le repli par defaut sur `shop.email` — fonction reutilisee telle
    quelle par ailleurs (garde fiscale 23:59, clotures mensuelle/annuelle)."""
    logger.exception("%s echoue : %s", job_name, exc)
    try:
        async with async_session() as db:
            await JournalService(db).record(
                EVENT_SYSTEM_JOB_FAILED,
                payload={"job": job_name, "error": str(exc)},
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Impossible de journaliser au JET l'échec de %s", job_name)
        return

    try:
        from app.services.email_gateway import EmailMessage, send_email
        from app.services.settings_service import SettingsService

        recipient = (email_override or "").strip()
        if not recipient:
            async with async_session() as db:
                shop = await SettingsService(db).get("shop")
            recipient = (shop.get("email") or "").strip() if isinstance(shop, dict) else ""
        if not recipient:
            return
        await send_email(
            EmailMessage(
                to=recipient,
                subject=f"⚠ Frip & Co Street — échec du job « {job_name} »",
                html=(
                    f"<p>Le job planifié <b>{job_name}</b> a échoué.</p>"
                    f"<p>Erreur : {str(exc)[:500]}</p>"
                    "<p>L'échec est journalisé au JET (system.job_failed). "
                    "Vérifier la clôture correspondante depuis /admin.</p>"
                ),
            )
        )
    except Exception:  # noqa: BLE001 — alerte best-effort, jamais bloquante
        logger.exception("Alerte e-mail non envoyée pour l'échec de %s", job_name)


def _previous_month_period(today: date) -> tuple[datetime, datetime]:
    """[1er du mois precedent 00:00, 1er du mois courant 00:00), UTC naif —
    la garde fiscale 23:59 ferme deja toute caisse oubliee avant ce cron."""
    first_of_this_month = today.replace(day=1)
    last_day_prev_month = first_of_this_month - timedelta(days=1)
    first_of_prev_month = last_day_prev_month.replace(day=1)
    start = datetime(
        first_of_prev_month.year, first_of_prev_month.month, 1, tzinfo=timezone.utc
    )
    end = datetime(first_of_this_month.year, first_of_this_month.month, 1, tzinfo=timezone.utc)
    return start, end


def _previous_year_period(today: date) -> tuple[datetime, datetime]:
    """[1er janvier annee precedente 00:00, 1er janvier annee courante 00:00)."""
    start = datetime(today.year - 1, 1, 1, tzinfo=timezone.utc)
    end = datetime(today.year, 1, 1, tzinfo=timezone.utc)
    return start, end


async def run_monthly_fiscal_closure() -> None:
    """Cloture mensuelle automatique — 1er du mois 00:15 Europe/Paris (§4.3
    F5) : scelle le mois precedent. Idempotent (`close_period` renvoie la
    cloture existante pour une periode deja close)."""
    try:
        from app.services.fiscal_closure import FiscalClosureService

        async with async_session() as db:
            period_start, period_end = _previous_month_period(datetime.now(timezone.utc).date())
            closure = await FiscalClosureService(db).close_period(
                closure_type="monthly",
                period_start=period_start,
                period_end=period_end,
                user_id=None,
            )
            await db.commit()
            logger.info(
                "Clôture mensuelle automatique : séquence %d (%s → %s)",
                closure.sequence_number, period_start.date(), period_end.date(),
            )
    except Exception as exc:  # noqa: BLE001
        await _alert_job_failure(JOB_MONTHLY_FISCAL_CLOSURE, exc)


async def run_annual_fiscal_closure() -> None:
    """Cloture annuelle automatique — 1er janvier 00:30 Europe/Paris (§4.3
    F5) : scelle l'annee civile precedente."""
    try:
        from app.services.fiscal_closure import FiscalClosureService

        async with async_session() as db:
            period_start, period_end = _previous_year_period(datetime.now(timezone.utc).date())
            closure = await FiscalClosureService(db).close_period(
                closure_type="annual",
                period_start=period_start,
                period_end=period_end,
                user_id=None,
            )
            await db.commit()
            logger.info(
                "Clôture annuelle automatique : séquence %d (%s → %s)",
                closure.sequence_number, period_start.date(), period_end.date(),
            )
    except Exception as exc:  # noqa: BLE001
        await _alert_job_failure(JOB_ANNUAL_FISCAL_CLOSURE, exc)


JOB_NIGHTLY_DATABASE_BACKUP = "nightly_database_backup"


async def run_nightly_database_backup() -> None:
    """Sauvegarde applicative nocturne de la base — 03:00 Europe/Paris (PR5,
    G4, docs/ARCHITECTURE_PR5.md §1). Aucun chevauchement avec les clotures
    fiscales (00:15/00:30/23:59). N'ecrit rien si `backup.nightly_enabled`
    est faux (simple log — pas de JET dedie, ce n'est pas un echec).

    En cas d'echec, `database_backup.run_backup` a deja committe une ligne
    `failed` avant de lever `BackupError` : ce wrapper journalise en plus
    au JET (`system.job_failed`) et alerte `backup.alert_email` (repli
    `shop.email` via `_alert_job_failure`) — jamais avale en silence (S-5).
    """
    try:
        from app.services import database_backup
        from app.services.settings_service import SettingsService

        async with async_session() as db:
            backup_settings = await SettingsService(db).get("backup")
            if not backup_settings.get("nightly_enabled", True):
                logger.info("Sauvegarde nocturne désactivée (backup.nightly_enabled=false)")
                return
            backup = await database_backup.run_backup(db, trigger="nightly", user_id=None)
            logger.info(
                "Sauvegarde nocturne : statut=%s fichier=%s", backup.status.value, backup.filename
            )
        # PR9/K1 — purge du journal des echanges SumUp, APRES la sauvegarde :
        # ainsi le dump de la nuit contient encore les echanges qu'on va
        # supprimer, et une purge trop agressive reste rattrapable. Session
        # separee et echec avale : ce menage ne doit jamais faire echouer la
        # sauvegarde, qui est la seule chose critique de ce job.
        await _purge_sumup_exchanges()
    except Exception as exc:  # noqa: BLE001
        alert_email = ""
        try:
            from app.services.settings_service import SettingsService

            async with async_session() as db:
                backup_settings = await SettingsService(db).get("backup")
            alert_email = (backup_settings.get("alert_email") or "").strip()
        except Exception:  # noqa: BLE001 — lecture des reglages best-effort
            logger.exception("Lecture des réglages de sauvegarde impossible avant alerte")
        await _alert_job_failure(
            JOB_NIGHTLY_DATABASE_BACKUP, exc, email_override=alert_email or None
        )


async def _purge_sumup_exchanges() -> None:
    """Applique la retention `payments.exchange_retention_days` (PR9/K1).

    Best-effort : `sumup_exchanges` est une table d'exploitation (aucune
    vente n'y nait), une purge ratee se rattrapera la nuit suivante ou
    depuis l'ecran admin.
    """
    try:
        from app.services.settings_service import SettingsService
        from app.services.sumup_exchange_log import purge

        async with async_session() as db:
            retention_days = await SettingsService(db).get_exchange_retention_days()
            deleted = await purge(db, retention_days)
            await db.commit()
        if deleted:
            logger.info(
                "Journal des échanges SumUp : %d ligne(s) purgée(s) (rétention %d j)",
                deleted,
                retention_days,
            )
    except Exception:  # noqa: BLE001 — cf. docstring
        logger.exception("Purge du journal des échanges SumUp échouée")


def register_all_jobs(scheduler) -> None:
    """Enregistre les jobs planifies (fuseau Europe/Paris, cf. `app/main.py`)."""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_daily_fiscal_close_guard,
        CronTrigger(hour=23, minute=59),
        id=JOB_DAILY_FISCAL_CLOSE_GUARD,
        replace_existing=True,
    )
    scheduler.add_job(
        run_monthly_fiscal_closure,
        CronTrigger(day=1, hour=0, minute=15),
        id=JOB_MONTHLY_FISCAL_CLOSURE,
        replace_existing=True,
    )
    scheduler.add_job(
        run_annual_fiscal_closure,
        CronTrigger(month=1, day=1, hour=0, minute=30),
        id=JOB_ANNUAL_FISCAL_CLOSURE,
        replace_existing=True,
    )
    scheduler.add_job(
        run_nightly_database_backup,
        CronTrigger(hour=3, minute=0),
        id=JOB_NIGHTLY_DATABASE_BACKUP,
        replace_existing=True,
    )
