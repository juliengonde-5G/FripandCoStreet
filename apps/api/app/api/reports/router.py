# Nouveau routeur (PR6, H3 de docs/ARCHITECTURE_PR6.md) — tableau de bord
# d'accueil. Lecture seule : aucune ecriture, aucune signature fiscale,
# aucun evenement JET (comme la lecture du JET elle-meme, §4.7 PR2).
#
# Etendu en PR11 (M1/M3, docs/ARCHITECTURE_PR11.md) aux trois rapports
# (jour / semaine / mois) et a la meteo. Meme regle : on ne touche a rien.
# La SEULE ecriture de ce routeur est l'evenement JET `export.downloaded`
# pose au telechargement d'un CSV — un export de donnees se trace, c'est
# justement ce que le JET est la pour dire.
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.fiscal import PosServiceError
from app.services.jet import EVENT_EXPORT_DOWNLOADED, JournalService
from app.services.pos import _PARIS
from app.services.reporting import dashboard, serialize_dashboard
from app.services.reports import (
    DAILY,
    MONTHLY,
    WEEKLY,
    build_report,
    parse_day,
    parse_month,
    report_csv_filename,
    report_to_csv,
    serialize_report,
)
from app.services.weather import get_current as weather_current

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/dashboard")
async def get_dashboard(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date: Annotated[str | None, Query(description="Jour de reference, YYYY-MM-DD")] = None,
):
    """Chiffres d'accueil : jour, mois, sept derniers jours, vs objectifs.

    `?date=YYYY-MM-DD` deplace le jour de reference (consultation d'un jour
    passe) : il est converti en instant de reference (midi, heure de Paris —
    n'importe quelle heure de la journee civile donne le meme resultat) et
    passe tel quel au service, qui en derive « aujourd'hui », le mois et la
    serie 7 jours.
    """
    now: datetime | None = None
    if date is not None:
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise PosServiceError(
                f"Date invalide ({date!r}) : format YYYY-MM-DD attendu.",
                code="invalid_date",
                status_code=422,
            )
        now = datetime(day.year, day.month, day.day, 12, 0, tzinfo=_PARIS)

    return serialize_dashboard(await dashboard(db, now=now))


@router.get("/weather")
async def get_weather(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Meteo courante de la boutique (PR11, M3).

    Toujours 200 : une meteo indisponible est une reponse valide
    (`{"unavailable": true, "reason": "…"}`), pas une erreur — l'accueil
    affiche alors une ligne discrete au lieu de casser.
    """
    return await weather_current(db)


async def _report_response(
    db: AsyncSession,
    user: User,
    kind: str,
    *,
    day,
    csv_format: str | None,
) -> Any:
    """Corps commun aux trois routes : JSON, ou CSV telecharge et trace.

    Le JET ne recoit que les BORNES de l'export (`kind`, `period_kind`,
    `from`, `to`) — jamais son contenu : le journal dit qu'une extraction a
    eu lieu et laquelle, il n'est pas une copie des donnees extraites.
    """
    report = await build_report(db, kind, day=day)
    if csv_format != "csv":
        return serialize_report(report)

    period = report["period"]
    # UTF-8 avec BOM (`utf-8-sig`) : sans lui, un tableur ouvre « Espèces »
    # en « EspÃ¨ces » — c'est le premier reproche fait a tout export CSV.
    body = report_to_csv(report).encode("utf-8-sig")
    await JournalService(db).record(
        EVENT_EXPORT_DOWNLOADED,
        user_id=user.id,
        payload={
            "kind": "report_csv",
            "period_kind": period["kind"],
            "from": period["from"].isoformat(),
            "to": period["to"].isoformat(),
        },
    )
    await db.commit()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{report_csv_filename(report)}"'
        },
    )


@router.get("/daily")
async def get_daily_report(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date: Annotated[str, Query(description="Jour du rapport, AAAA-MM-JJ")],
    format: Annotated[str | None, Query(description="`csv` pour telecharger")] = None,
):
    """Rapport d'une journee civile Europe/Paris (M1)."""
    return await _report_response(db, user, DAILY, day=parse_day(date), csv_format=format)


@router.get("/weekly")
async def get_weekly_report(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date: Annotated[str, Query(description="Un jour de la semaine voulue, AAAA-MM-JJ")],
    format: Annotated[str | None, Query(description="`csv` pour telecharger")] = None,
):
    """Rapport de la semaine ISO (lundi -> dimanche) contenant `date` (M1)."""
    return await _report_response(db, user, WEEKLY, day=parse_day(date), csv_format=format)


@router.get("/monthly")
async def get_monthly_report(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    month: Annotated[str, Query(description="Mois du rapport, AAAA-MM")],
    format: Annotated[str | None, Query(description="`csv` pour telecharger")] = None,
):
    """Rapport d'un mois civil (M1)."""
    return await _report_response(db, user, MONTHLY, day=parse_month(month), csv_format=format)
