# Extrait de Vintiz (apps/api/app/jobs.py:648-652 — daily_fiscal_close_guard
# uniquement, §4.3 du contrat PR2). Aucun autre cron Vintiz (clotures
# mensuelle/annuelle, exports) n'est repris — hors perimetre PR2.
from __future__ import annotations

import logging

from app.core.database import async_session
from app.services.fiscal import FiscalService
from app.services.jet import EVENT_SYSTEM_JOB_FAILED, JournalService

logger = logging.getLogger("fripco")

JOB_DAILY_FISCAL_CLOSE_GUARD = "daily_fiscal_close_guard"


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


def register_all_jobs(scheduler) -> None:
    """Enregistre les jobs planifies (fuseau Europe/Paris, cf. `app/main.py`)."""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_daily_fiscal_close_guard,
        CronTrigger(hour=23, minute=59),
        id=JOB_DAILY_FISCAL_CLOSE_GUARD,
        replace_existing=True,
    )
