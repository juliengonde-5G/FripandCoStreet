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


async def _alert_job_failure(job_name: str, exc: Exception) -> None:
    """Journalise l'echec au JET (jamais avale, S-5) puis tente un e-mail
    d'alerte best-effort a `shop.email` (un echec d'envoi ne remonte pas :
    la trace JET est deja ecrite, c'est elle la garantie)."""
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

        async with async_session() as db:
            shop = await SettingsService(db).get("shop")
        shop_email = (shop.get("email") or "").strip() if isinstance(shop, dict) else ""
        if not shop_email:
            return
        await send_email(
            EmailMessage(
                to=shop_email,
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
