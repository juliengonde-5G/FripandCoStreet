# Extrait de l'application source (apps/api/app/api/admin/router.py) — perimetre reduit a
# la lecture du JET (compte unique : tout utilisateur authentifie a acces
# admin, pas de RoleChecker). PR2 (docs/ARCHITECTURE_PR2.md §4.7) ajoute les
# parametres boutique (`app_settings`) et le controle d'integrite fiscal.
# PR3 (docs/ARCHITECTURE_PR3.md §4) ajoute la fiche client/RGPD, l'etat de
# la messagerie et `dpo_email` sur `shop`.
# PR3b (impression tickets, décision Julien) ajoute la clé `hardware`
# (imprimante MUNBYN 047P + tiroir Safescan SD-4141) — jamais de secret,
# uniquement de la config réseau/USB (voir app/services/escpos_service.py).
import ipaddress
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.accounting import AccountingExport
from app.models.client import Client, ConsentPurpose, ConsentSource
from app.models.fiscal_closure import FiscalClosure
from app.models.jet import JournalEvent
from app.models.pos import ZReport
from app.models.user import User
from app.services import brevo_contacts, email_gateway
from app.services.accounting_service import AccountingService
from app.services.client_service import ClientService
from app.services.fiscal import FiscalService, PosServiceError
from app.services.fiscal_closure import FiscalClosureService
from app.services.fiscal_export import FiscalExportService
from app.services.jet import (
    EVENT_EXPORT_DOWNLOADED,
    EVENT_FISCAL_INTEGRITY_CHECKED,
    JournalService,
)
from app.services.settings_service import SettingsService
from app.services.table_export import EXPORTABLE_TABLES, UnknownTableError, export_table_csv
from app.services.tva_service import SUPPORTED_TVA_RATES

router = APIRouter(prefix="/admin", tags=["admin"])


def _serialize(event: JournalEvent) -> dict:
    return {
        "id": str(event.id),
        "seq": event.seq,
        "event_type": event.event_type,
        "user_id": str(event.user_id) if event.user_id else None,
        "username": event.username,
        "ip": event.ip,
        "request_id": event.request_id,
        "payload": event.payload,
        "previous_hash": event.previous_hash,
        "hash": event.hash,
        "signature_version": event.signature_version,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


@router.get("/jet")
async def list_journal_events(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=100, ge=1, le=500),
    before_seq: int | None = Query(default=None),
):
    """Journal des evenements techniques, du plus recent au plus ancien.

    La lecture du JET n'est volontairement pas elle-meme journalisee (seul
    l'export futur le sera) — pas d'ajout de complexite non demandee ici.

    `next_before_seq` porte le curseur de pagination du front : le `seq` du
    dernier evenement de cette page (le plus ancien, l'ordre etant
    decroissant), a repasser en `before_seq` pour la page suivante — ou
    `null` quand cette page n'est pas pleine (`limit`), preuve qu'il n'y a
    plus rien au-dela.
    """
    query = select(JournalEvent).order_by(JournalEvent.seq.desc())
    if before_seq is not None:
        query = query.where(JournalEvent.seq < before_seq)
    events = (await db.execute(query.limit(limit))).scalars().all()
    next_before_seq = events[-1].seq if len(events) == limit else None
    return {
        "events": [_serialize(e) for e in events],
        "count": len(events),
        "next_before_seq": next_before_seq,
    }


@router.get("/jet/integrity")
async def journal_integrity(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Verifie l'integrite complete de la chaine JET (recalcul des hash)."""
    return await JournalService(db).verify_chain()


# ---------------------------------------------------------------------------
# Parametrage boutique (PR2, D13, §4.7) — remplace `data/app_config.json` de
# l'application source : stocke en base, toute ecriture journalisee au JET.
# ---------------------------------------------------------------------------

_SIRET_RE = re.compile(r"^\d{14}$")


class ShopSettingsIn(BaseModel):
    name: str = "Frip & Co Street"
    address_line1: str = ""
    address_line2: str = ""
    postal_code: str = ""
    city: str = ""
    siret: str = ""
    vat_number: str = ""
    phone: str = ""
    email: str = ""
    # PR3 (E8) — contact DPO affiché sur l'écran de saisie client et dans le
    # paragraphe RGPD de l'e-mail du ticket (`services/receipt_email.py`).
    dpo_email: str = ""

    @field_validator("siret")
    @classmethod
    def _validate_siret(cls, value: str) -> str:
        if value and not _SIRET_RE.match(value):
            raise ValueError("SIRET invalide : 14 chiffres attendus")
        return value


class FiscalSettingsIn(BaseModel):
    tva_rate: Decimal

    @field_validator("tva_rate")
    @classmethod
    def _validate_rate(cls, value: Decimal) -> Decimal:
        normalized = value.quantize(Decimal("0.01"))
        if normalized not in SUPPORTED_TVA_RATES:
            allowed = ", ".join(f"{r:.2f}" for r in SUPPORTED_TVA_RATES)
            raise ValueError(f"Taux de TVA non supporté ({value}) — valeurs autorisées : {allowed}")
        return normalized


class ReceiptSettingsIn(BaseModel):
    header_note: str = ""
    footer_note: str = ""
    return_policy: str = ""


class HardwareSettingsIn(BaseModel):
    """Réglages matériel (PR3b, décision Julien) — imprimante ticket MUNBYN
    047P (réseau TCP 9100 ou WebUSB depuis la tablette) et tiroir-caisse
    Safescan SD-4141 branché dessus. Aucun secret : uniquement de la
    config réseau/USB, comme `shop`/`fiscal`/`receipt`."""

    printer_mode: Literal["network", "webusb", "none"] = "none"
    printer_host: str = ""
    printer_port: int = Field(default=9100, ge=1, le=65535)
    drawer_enabled: bool = False
    drawer_pin: Literal[0, 1] = 0
    auto_print_on_sale: bool = False
    auto_kick_on_cash: bool = False

    @field_validator("printer_host")
    @classmethod
    def _validate_printer_host(cls, value: str) -> str:
        value = (value or "").strip()
        if value:
            try:
                ipaddress.IPv4Address(value)
            except ValueError:
                raise ValueError(
                    "Adresse IP de l'imprimante invalide (format IPv4 attendu, ex. 192.168.1.50)"
                )
        return value

    @model_validator(mode="after")
    def _validate_network_requires_host(self) -> "HardwareSettingsIn":
        if self.printer_mode == "network" and not self.printer_host:
            raise ValueError(
                "Adresse IP requise en mode réseau (printer_mode=network)"
            )
        return self


# PR4 (F1, docs/ARCHITECTURE_PR4.md §1/§4) — plan de comptes comptable.
# Validation : compte numerique, journal 1 a 5 caracteres. Ecart assume par
# rapport a la lettre du contrat (« comptes numeriques 6-8 chiffres ») : les
# DEFAUTS F1 eux-memes (identiques a l'application source) incluent
# `account_tva="44571"` (5 chiffres) — une regle stricte 6-8 rejetterait donc
# la valeur par defaut si un manager la re-soumettait telle quelle depuis
# l'ecran de reglages. On valide 3 a 8 chiffres (couvre tous les defauts F1 :
# 44571 comme 707100/512000/531000/658000/758000) plutot que de bloquer un
# cas d'usage legitime — voir le rapport de livraison.
_ACCOUNT_NUMBER_RE = re.compile(r"^\d{3,8}$")
_JOURNAL_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,5}$")


class AccountingSettingsIn(BaseModel):
    journal_code: str = "VTE"
    account_sales: str = "707100"
    label_sales: str = "Ventes marchandises"
    account_tva: str = "44571"
    label_tva: str = "TVA collectée 20%"
    account_cash: str = "531000"
    label_cash: str = "Caisse"
    account_card: str = "512000"
    label_card: str = "CB SumUp"
    account_rounding_expense: str = "658000"
    account_rounding_income: str = "758000"

    @field_validator("journal_code")
    @classmethod
    def _validate_journal(cls, value: str) -> str:
        if not _JOURNAL_CODE_RE.match(value or ""):
            raise ValueError("Code journal invalide (1 à 5 caractères alphanumériques)")
        return value.upper()

    @field_validator(
        "account_sales",
        "account_tva",
        "account_cash",
        "account_card",
        "account_rounding_expense",
        "account_rounding_income",
    )
    @classmethod
    def _validate_account(cls, value: str) -> str:
        if not _ACCOUNT_NUMBER_RE.match(value or ""):
            raise ValueError(f"Numéro de compte invalide ({value!r}) : 3 à 8 chiffres attendus")
        return value


_SETTINGS_SCHEMAS: dict[str, type[BaseModel]] = {
    "shop": ShopSettingsIn,
    "fiscal": FiscalSettingsIn,
    "receipt": ReceiptSettingsIn,
    "hardware": HardwareSettingsIn,
    "accounting": AccountingSettingsIn,
}


def _settings_to_json(value: BaseModel) -> dict:
    """Serialise un schema de settings en JSON-compatible (Decimal -> str)."""
    return {
        k: (str(v) if isinstance(v, Decimal) else v) for k, v in value.model_dump().items()
    }


@router.get("/settings/{key}")
async def get_settings(
    key: str,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if key not in _SETTINGS_SCHEMAS:
        raise PosServiceError(f"Paramètre inconnu : {key}", code="unknown_setting", status_code=404)
    return await SettingsService(db).get(key)


@router.put("/settings/{key}")
async def put_settings(
    key: str,
    body: dict,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    schema = _SETTINGS_SCHEMAS.get(key)
    if schema is None:
        raise PosServiceError(f"Paramètre inconnu : {key}", code="unknown_setting", status_code=404)
    try:
        validated = schema(**body)
    except Exception as exc:  # noqa: BLE001 — erreurs Pydantic -> 422 lisible
        raise PosServiceError(
            f"Paramètres invalides : {exc}", code="invalid_setting", status_code=422
        )
    row = await SettingsService(db).set(key, _settings_to_json(validated), user_id=user.id)
    await db.commit()
    return row.value


# ---------------------------------------------------------------------------
# Controle d'integrite fiscal (§4.7, §3)
# ---------------------------------------------------------------------------


@router.get("/fiscal/integrity")
async def fiscal_integrity(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Verification d'integrite globale — etendue (PR4, §4) aux clotures
    fiscales periodiques et aux ecritures comptables (recalcul + comparaison,
    jamais de reecriture — voir `AccountingService.verify_export`)."""
    fiscal = FiscalService(db)
    transactions_result = await fiscal.verify_chain_integrity()
    z_reports_result = await fiscal.verify_z_chain_integrity()
    jet_result = await JournalService(db).verify_chain()
    closures_result = await FiscalClosureService(db).verify_chain()

    accounting_service = AccountingService(db)
    z_rows = (await db.execute(select(ZReport).order_by(ZReport.report_number.asc()))).scalars().all()
    accounting_mismatches: list[int] = []
    for z in z_rows:
        result = await accounting_service.verify_export(z, user_id=user.id)
        if not result["valid"]:
            accounting_mismatches.append(z.report_number)
    accounting_result = {
        "valid": not accounting_mismatches,
        "checked": len(z_rows),
        "mismatched_z_numbers": accounting_mismatches,
    }

    await JournalService(db).record(
        EVENT_FISCAL_INTEGRITY_CHECKED,
        user_id=user.id,
        payload={
            "transactions_valid": transactions_result["valid"],
            "z_reports_valid": z_reports_result["valid"],
            "jet_valid": jet_result["valid"],
            "closures_valid": closures_result["valid"],
            "accounting_valid": accounting_result["valid"],
        },
    )
    await db.commit()
    return {
        "transactions": transactions_result,
        "z_reports": z_reports_result,
        "jet": jet_result,
        "closures": closures_result,
        "accounting": accounting_result,
    }


# ---------------------------------------------------------------------------
# Comptabilite — ecritures par Z, CSV mensuel Pennylane, FEC (PR4, §3/§4 F1-F3)
# ---------------------------------------------------------------------------


async def _record_export_downloaded(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    kind: str,
    sha256: str,
    rows: int,
    period_start: str | None = None,
    period_end: str | None = None,
) -> None:
    await JournalService(db).record(
        EVENT_EXPORT_DOWNLOADED,
        user_id=user_id,
        payload={
            "kind": kind,
            "period_start": period_start,
            "period_end": period_end,
            "sha256": sha256,
            "rows": rows,
        },
    )


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _siren(shop: dict) -> str:
    siret = (shop.get("siret") or "").strip()
    return siret[:9] if len(siret) >= 9 else "000000000"


def _serialize_accounting_export(export: AccountingExport, z: ZReport | None = None) -> dict:
    total_debit = sum((float(ln.debit) for ln in export.lines), 0.0)
    total_credit = sum((float(ln.credit) for ln in export.lines), 0.0)
    return {
        "id": str(export.id),
        "z_report_id": str(export.z_report_id),
        "z_number": z.report_number if z else None,
        "export_date": export.export_date.isoformat(),
        "total_sales_ht": float(export.total_sales_ht),
        "total_tva": float(export.total_tva),
        "total_ttc": float(export.total_ttc),
        "total_refunds_ttc": float(export.total_refunds_ttc),
        "total_cash": float(export.total_cash),
        "total_card": float(export.total_card),
        "total_debit": total_debit,
        "total_credit": total_credit,
        "balanced": abs(total_debit - total_credit) < 0.005,
        "rounding_adjustment": float(export.rounding_adjustment),
        "lines": [
            {
                "line_number": ln.line_number,
                "account_number": ln.account_number,
                "account_label": ln.account_label,
                "label": ln.label,
                "debit": float(ln.debit),
                "credit": float(ln.credit),
                "piece_reference": ln.piece_reference,
            }
            for ln in sorted(export.lines, key=lambda x: x.line_number)
        ],
    }


@router.get("/accounting/exports")
async def list_accounting_exports(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
):
    from calendar import monthrange

    last_day = monthrange(year, month)[1]
    date_from = date(year, month, 1)
    date_to = date(year, month, last_day)
    exports = (
        await db.execute(
            select(AccountingExport)
            .where(AccountingExport.export_date >= date_from, AccountingExport.export_date <= date_to)
            .order_by(AccountingExport.export_date.asc(), AccountingExport.created_at.asc())
        )
    ).scalars().all()
    z_by_id = {
        z.id: z
        for z in (
            await db.execute(
                select(ZReport).where(ZReport.id.in_([e.z_report_id for e in exports]))
            )
        ).scalars().all()
    } if exports else {}
    return {
        "year": year,
        "month": month,
        "exports": [_serialize_accounting_export(e, z_by_id.get(e.z_report_id)) for e in exports],
    }


@router.get("/accounting/exports/{z_id}")
async def get_accounting_export(
    z_id: uuid.UUID,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    z = (await db.execute(select(ZReport).where(ZReport.id == z_id))).scalar_one_or_none()
    if z is None:
        raise PosServiceError("Z introuvable.", code="not_found", status_code=404)
    export = (
        await db.execute(select(AccountingExport).where(AccountingExport.z_report_id == z_id))
    ).scalar_one_or_none()
    if export is None:
        raise PosServiceError(
            "Aucune écriture comptable pour ce Z.", code="export_not_found", status_code=404
        )
    return _serialize_accounting_export(export, z)


@router.get("/accounting/monthly-csv/{year}/{month}")
async def download_monthly_csv(
    year: int,
    month: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not (1 <= month <= 12):
        raise PosServiceError("Mois invalide.", code="invalid_period", status_code=422)
    csv_text = await AccountingService(db).generate_monthly_csv(year, month)
    # UTF-8 BOM (utf-8-sig) pour qu'Excel ouvre correctement les accents.
    body = csv_text.encode("utf-8-sig")
    filename = f"ecritures_{year}-{month:02d}.csv"
    rows = max(csv_text.count("\r\n") - 1, 0)
    await _record_export_downloaded(
        db, user.id, kind="accounting_monthly_csv", sha256=_sha256_hex(body), rows=rows,
        period_start=date(year, month, 1).isoformat(),
    )
    await db.commit()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/accounting/fec/day/{target_date}")
async def download_daily_fec(
    target_date: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        d = date.fromisoformat(target_date)
    except ValueError:
        raise PosServiceError("Date invalide (AAAA-MM-JJ).", code="invalid_date", status_code=422)
    fec = await AccountingService(db).generate_daily_fec(d)
    shop = await SettingsService(db).get("shop")
    body = fec.encode("utf-8")
    filename = f"FEC_{_siren(shop)}_{d.strftime('%Y%m%d')}.txt"
    rows = max(fec.count("\n") - 1, 0) if fec else 0
    await _record_export_downloaded(
        db, user.id, kind="fec_day", sha256=_sha256_hex(body), rows=rows,
        period_start=d.isoformat(), period_end=d.isoformat(),
    )
    await db.commit()
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/accounting/fec/month/{year}/{month}")
async def download_monthly_fec(
    year: int,
    month: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not (1 <= month <= 12):
        raise PosServiceError("Mois invalide.", code="invalid_period", status_code=422)
    fec = await AccountingService(db).generate_monthly_fec(year, month)
    shop = await SettingsService(db).get("shop")
    body = fec.encode("utf-8")
    filename = f"FEC_{_siren(shop)}_{year}{month:02d}.txt"
    rows = max(fec.count("\n") - 1, 0) if fec else 0
    await _record_export_downloaded(
        db, user.id, kind="fec_month", sha256=_sha256_hex(body), rows=rows,
        period_start=date(year, month, 1).isoformat(),
    )
    await db.commit()
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Exports bruts CSV — journal des ventes / journal de caisse (PR4, §3/§4 F4)
# ---------------------------------------------------------------------------


@router.get("/exports/table/{table}")
async def download_table_export(
    table: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
):
    def _parse(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise PosServiceError(
                f"Date invalide : {value!r}", code="invalid_date", status_code=422
            )
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

    try:
        filename, csv_text = await export_table_csv(
            db, table, date_from=_parse(date_from), date_to=_parse(date_to)
        )
    except UnknownTableError:
        raise PosServiceError(
            f"Table non exportable : {table!r} (autorisées : {', '.join(EXPORTABLE_TABLES)})",
            code="unknown_table",
            status_code=404,
        )
    body = csv_text.encode("utf-8")
    rows = max(csv_text.count("\r\n") - 1, 0)
    await _record_export_downloaded(
        db, user.id, kind=f"table_{table}", sha256=_sha256_hex(body), rows=rows,
        period_start=date_from, period_end=date_to,
    )
    await db.commit()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Clotures fiscales periodiques (PR4, §3/§4 F5)
# ---------------------------------------------------------------------------


class FiscalClosureRequest(BaseModel):
    closure_type: Literal["monthly", "annual", "manual"]
    period_start: datetime
    period_end: datetime


@router.post("/fiscal-closures", status_code=201)
async def create_fiscal_closure(
    body: FiscalClosureRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    closure = await FiscalClosureService(db).close_period(
        closure_type=body.closure_type,
        period_start=body.period_start,
        period_end=body.period_end,
        user_id=user.id,
    )
    await db.commit()
    return FiscalClosureService.serialize(closure)


@router.get("/fiscal-closures")
async def list_fiscal_closures(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (
        await db.execute(select(FiscalClosure).order_by(FiscalClosure.sequence_number.desc()))
    ).scalars().all()
    return {"closures": [FiscalClosureService.serialize(c) for c in rows]}


@router.get("/fiscal-closures/integrity")
async def fiscal_closures_integrity(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await FiscalClosureService(db).verify_chain()


@router.get("/fiscal-closures/{closure_id}")
async def get_fiscal_closure(
    closure_id: uuid.UUID,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    closure = (
        await db.execute(select(FiscalClosure).where(FiscalClosure.id == closure_id))
    ).scalar_one_or_none()
    if closure is None:
        raise PosServiceError("Clôture fiscale introuvable.", code="not_found", status_code=404)
    return FiscalClosureService.serialize(closure)


@router.get("/fiscal-closures/{closure_id}/archive")
async def download_fiscal_closure_archive(
    closure_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    closure = (
        await db.execute(select(FiscalClosure).where(FiscalClosure.id == closure_id))
    ).scalar_one_or_none()
    if closure is None:
        raise PosServiceError("Clôture fiscale introuvable.", code="not_found", status_code=404)
    filename = f"fripco-nf525-{closure.sequence_number:06d}-{closure.closure_type.value}.json.gz"
    await _record_export_downloaded(
        db, user.id, kind="fiscal_closure_archive", sha256=closure.archive_sha256,
        rows=closure.transaction_count,
        period_start=closure.period_start.isoformat(), period_end=closure.period_end.isoformat(),
    )
    await db.commit()
    return Response(
        content=closure.archive_content,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Archive-SHA256": closure.archive_sha256,
            "X-Closure-Hash": closure.hash,
        },
    )


# ---------------------------------------------------------------------------
# Export fiscal a la demande (PR4, §3/§4 F6)
# ---------------------------------------------------------------------------


@router.get("/fiscal-export")
async def fiscal_export(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    format: Literal["json", "xml"] = Query(default="json"),
):
    def _parse(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise PosServiceError(
                f"Date invalide : {value!r}", code="invalid_date", status_code=422
            )
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

    period_from = _parse(date_from)
    period_to = _parse(date_to)

    fiscal = FiscalService(db)
    tx_integrity = await fiscal.verify_chain_integrity()
    z_integrity = await fiscal.verify_z_chain_integrity()
    if not tx_integrity["valid"] or not z_integrity["valid"]:
        raise PosServiceError(
            "La chaîne fiscale n'est pas valide : export refusé.",
            code="chain_invalid",
            status_code=409,
        )

    shop = await SettingsService(db).get("shop")
    svc = FiscalExportService(db)
    snapshot = await svc.build_snapshot(
        period_from=period_from,
        period_to=period_to,
        merchant_name=shop.get("name") or "Frip & Co Street",
        merchant_id=shop.get("siret") or "",
    )

    if format == "xml":
        body_text = svc.to_xml(snapshot)
        media_type = "application/xml; charset=utf-8"
    else:
        body_text = svc.to_json(snapshot)
        media_type = "application/json; charset=utf-8"
    body = body_text.encode("utf-8")
    sha = _sha256_hex(body)

    await _record_export_downloaded(
        db, user.id, kind=f"fiscal_export_{format}", sha256=sha,
        rows=len(snapshot["transactions"]),
        period_start=date_from, period_end=date_to,
    )
    await db.commit()
    return Response(content=body, media_type=media_type, headers={"X-Export-SHA256": sha})


# ---------------------------------------------------------------------------
# Débogage TPE (§4.7) — journal des tentatives de paiement CB.
# ---------------------------------------------------------------------------


@router.get("/payments/cb/attempts")
async def list_payment_attempts(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    from app.models.payment_attempt import PaymentAttempt, PaymentAttemptStatus

    query = select(PaymentAttempt).order_by(PaymentAttempt.created_at.desc())
    if status:
        try:
            query = query.where(PaymentAttempt.status == PaymentAttemptStatus(status))
        except ValueError:
            raise PosServiceError(f"Statut inconnu : {status}", code="invalid_status", status_code=422)
    rows = (await db.execute(query.limit(limit))).scalars().all()
    return {
        "attempts": [
            {
                "id": str(a.id),
                "client_uuid": str(a.client_uuid),
                "amount": float(a.amount),
                "status": a.status.value,
                "checkout_id": a.checkout_id,
                "reader_id": a.reader_id,
                "error_message": a.error_message,
                "transaction_id": str(a.transaction_id) if a.transaction_id else None,
                "attempt_count": a.attempt_count,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in rows
        ]
    }


# ---------------------------------------------------------------------------
# Clients — fiche, consentements, RGPD (PR3, §4 ARCHITECTURE_PR3.md)
# ---------------------------------------------------------------------------


class AdminConsentIn(BaseModel):
    purpose: Literal["newsletter"] = "newsletter"
    granted: bool
    note: str | None = None


class AdminAnonymizeIn(BaseModel):
    reason: str = Field(min_length=3)


def _serialize_client_summary(client: Client) -> dict:
    return {
        "id": str(client.id),
        "email": client.email,
        "first_name": client.first_name,
        "last_name": client.last_name,
        "newsletter_optin": client.newsletter_optin,
        "created_at": client.created_at.isoformat() if client.created_at else None,
        "anonymized_at": client.anonymized_at.isoformat() if client.anonymized_at else None,
    }


@router.get("/clients")
async def list_clients(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    clients = await ClientService(db).search(q, limit=limit)
    return {"clients": [_serialize_client_summary(c) for c in clients]}


@router.get("/clients/{client_id}")
async def get_client(
    client_id: uuid.UUID,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await ClientService(db).get_by_id(client_id)
    if client is None:
        raise PosServiceError("Client introuvable.", code="not_found", status_code=404)
    return await ClientService(db).get_full(client)


@router.post("/clients/{client_id}/consents")
async def add_client_consent(
    client_id: uuid.UUID,
    body: AdminConsentIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await ClientService(db).get_by_id(client_id)
    if client is None:
        raise PosServiceError("Client introuvable.", code="not_found", status_code=404)
    clients = ClientService(db)
    purpose = ConsentPurpose(body.purpose)
    await clients.record_consent(
        client=client,
        purpose=purpose,
        granted=body.granted,
        source=ConsentSource.admin,
        user_id=user.id,
        note=body.note,
    )
    if purpose == ConsentPurpose.newsletter:
        # Revue RGPD : « Retirer de la newsletter »/« Inscrire (demande
        # orale) » doit refléter l'état sur la liste Brevo dédiée (push si
        # opt-in, retrait sinon) — best-effort, jamais bloquant.
        await clients.sync_brevo(client, user_id=user.id)
    await db.commit()
    return await clients.get_full(client)


@router.post("/clients/{client_id}/anonymize")
async def anonymize_client(
    client_id: uuid.UUID,
    body: AdminAnonymizeIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await ClientService(db).get_by_id(client_id)
    if client is None:
        raise PosServiceError("Client introuvable.", code="not_found", status_code=404)
    # L'e-mail original doit être capturé AVANT l'anonymisation (E4) :
    # `anonymize()` le remplace par une adresse `@anonyme.invalid`, ce
    # n'est donc plus l'adresse à retirer côté Brevo.
    original_email = client.email
    await ClientService(db).anonymize(client=client, user_id=user.id, reason=body.reason)
    if original_email:
        # Best-effort — jamais `DELETE /v3/contacts` ni `emailBlacklisted`
        # (E1) : seule l'appartenance à la liste Fripco dédiée change.
        await brevo_contacts.remove_from_list(original_email)
    await db.commit()
    return await ClientService(db).get_full(client)


@router.get("/clients/{client_id}/export")
async def export_client(
    client_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await ClientService(db).get_by_id(client_id)
    if client is None:
        raise PosServiceError("Client introuvable.", code="not_found", status_code=404)
    data = await ClientService(db).export(client, user_id=user.id)
    await db.commit()
    return data


# ---------------------------------------------------------------------------
# Messagerie — état des fournisseurs, aucun secret (PR3, §4)
# ---------------------------------------------------------------------------


@router.get("/messaging/status")
async def messaging_status(
    _user: Annotated[User, Depends(get_current_user)],
    _db: Annotated[AsyncSession, Depends(get_db)],
):
    return {
        "email": email_gateway.describe_active_provider(),
        "brevo_contacts": brevo_contacts.describe(),
    }
