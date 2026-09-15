# Nouveau service (PR4, docs/ARCHITECTURE_PR4.md §1/§3, F1-F3) — extrait/adapte
# du module equivalent de l'application source (`services/accounting_service.py`) :
# `_PENNYLANE_CSV_COLUMNS`/`_csv_row`/`_csv_amount`/`_csv_date`/`_csv_field`,
# `_FEC_COLUMNS`/`_fec_row`/`_fec_date`, `_build_journal_lines`, `_generate_fec`,
# `generate_monthly_csv` sont repris **texte identique** (memes colonnes,
# meme ordre, meme format — decision Julien §1 : pas d'integration API
# Pennylane, l'import se fait par fichier). Reduit au perimetre Frip & Co
# Street : deux moyens de paiement seulement (espece/CB, `PaymentMethod`),
# pas de config Pennylane persistee (F1 — la config comptable vit dans
# `app_settings.accounting`, cf. `services/settings_service.py`), `Decimal`
# partout au lieu de `float` (montants fiscaux).
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounting import AccountingExport, AccountingExportLine
from app.models.pos import ZReport
from app.services.jet import (
    EVENT_ACCOUNTING_EXPORT_CREATED,
    EVENT_ACCOUNTING_MISMATCH,
    JournalService,
)
from app.services.settings_service import SettingsService

_log = logging.getLogger("fripco.accounting")

TWO_PLACES = Decimal("0.01")


def _d(value) -> Decimal:
    return Decimal(str(value if value is not None else 0)).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )


class AccountingMismatchError(RuntimeError):
    """Levee par `verify_export` quand l'ecriture recalculee diverge de
    l'ecriture persistee (§3 F3 : `verify_export(z)` -> `accounting.mismatch`)."""


# ---------------------------------------------------------------------------
# FEC (Format d'Echange Comptable) — 18 colonnes obligatoires DGFiP, copie
# fidele de l'application source (`_FEC_COLUMNS`/`_fec_row`/`_fec_date`).
# ---------------------------------------------------------------------------

_FEC_COLUMNS = [
    "JournalCode", "JournalLib", "EcritureNum", "EcritureDate",
    "CompteNum", "CompteLib", "CompAuxNum", "CompAuxLib",
    "PieceRef", "PieceDate", "EcritureLib",
    "Debit", "Credit", "EcritureLet", "DateLet",
    "ValidDate", "Montantdevise", "Idevise",
]


def _fec_row(**kwargs) -> str:
    return "\t".join(str(kwargs.get(col, "")) for col in _FEC_COLUMNS)


def _fec_date(d: date | datetime | None) -> str:
    if d is None:
        return ""
    if isinstance(d, datetime):
        return d.strftime("%Y%m%d")
    return d.strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# CSV mensuel Pennylane — copie fidele de l'application source
# (`_PENNYLANE_CSV_COLUMNS`/`_csv_row`/`_csv_amount`/`_csv_date`/`_csv_field`).
# Separateur « ; », decimales a la virgule, UTF-8 (BOM ajoute au service —
# la meme convention que `table_export.py`), `\r\n`, regroupement par
# « Numero de piece » (= `Z####`) — chaque Z partage le meme numero de piece
# sur toutes ses lignes -> Pennylane regroupe en une ecriture equilibree.
# ---------------------------------------------------------------------------

_PENNYLANE_CSV_COLUMNS = [
    "Date",
    "Code Journal",
    "Numéro de compte",
    "Libellé de compte",
    "Libellé de ligne",
    "Taux de TVA du compte",
    "Code pays du compte",
    "Libellé de pièce",
    "Numéro de pièce",
    "Débit et/ou Crédit",
    "Crédit",
    "Famille de catégories",
    "Catégorie",
    "Identifiant de ligne",
    "Identifiant de lettrage",
]


def _csv_amount(value) -> str:
    """Montant a 2 decimales, virgule decimale (convention francaise)."""
    return f"{_d(value):.2f}".replace(".", ",")


def _csv_date(d: date | datetime | None) -> str:
    """Date au format JJ/MM/AAAA (lisible Excel + import Pennylane)."""
    if d is None:
        return ""
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime("%d/%m/%Y")


def _csv_field(value) -> str:
    """Echappement CSV : guillemets si le champ contient « ; », guillemet ou
    saut de ligne (RFC 4180)."""
    s = "" if value is None else str(value)
    if any(c in s for c in (";", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s


def _csv_row(values) -> str:
    return ";".join(_csv_field(v) for v in values)


@dataclass(frozen=True)
class JournalLine:
    account_number: str
    account_label: str
    debit: Decimal
    credit: Decimal
    label: str


class AccountingService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── Config (F1) ──────────────────────────────────────────────────────

    async def get_config(self) -> dict:
        return await SettingsService(self.db).get("accounting")

    # ── Construction des lignes d'ecriture (F2) ─────────────────────────

    def build_journal_lines(self, z_report: ZReport, cfg: dict) -> list[JournalLine]:
        """Une ligne debit par moyen d'encaissement NET (ventes -
        remboursements, deja calcule sur le Z — `payment_totals[method].net`),
        une ligne credit ventes HT NETTE (707), une ligne credit TVA
        collectee NETTE (44571), et un ajustement d'arrondi 658/758 si
        Σdebit != Σcredit (fidele a l'application source
        `_build_journal_lines`)."""
        z_ref = f"Z{z_report.report_number:04d}"
        payment_totals = z_report.payment_totals or {}
        lines: list[JournalLine] = []

        method_map = {
            "cash": (cfg["account_cash"], cfg["label_cash"]),
            "card": (cfg["account_card"], cfg["label_card"]),
        }
        for method, (account, label) in method_map.items():
            bucket = payment_totals.get(method)
            net = _d(bucket.get("net")) if bucket else Decimal("0")
            if net == 0:
                continue
            if net > 0:
                lines.append(
                    JournalLine(
                        account_number=account,
                        account_label=label,
                        debit=net,
                        credit=Decimal("0"),
                        label=f"{label} — {z_ref}",
                    )
                )
            else:
                lines.append(
                    JournalLine(
                        account_number=account,
                        account_label=label,
                        debit=Decimal("0"),
                        credit=abs(net),
                        label=f"Remboursement {label} — {z_ref}",
                    )
                )

        net_ht = _d(z_report.total_ht)
        if net_ht != 0:
            lines.append(
                JournalLine(
                    account_number=cfg["account_sales"],
                    account_label=cfg["label_sales"],
                    debit=Decimal("0") if net_ht > 0 else abs(net_ht),
                    credit=net_ht if net_ht > 0 else Decimal("0"),
                    label=f"{cfg['label_sales']} — {z_ref}",
                )
            )

        net_tva = _d(z_report.total_tva)
        if net_tva != 0:
            lines.append(
                JournalLine(
                    account_number=cfg["account_tva"],
                    account_label=cfg["label_tva"],
                    debit=Decimal("0") if net_tva > 0 else abs(net_tva),
                    credit=net_tva if net_tva > 0 else Decimal("0"),
                    label=f"{cfg['label_tva']} — {z_ref}",
                )
            )

        total_debit = sum((ln.debit for ln in lines), Decimal("0"))
        total_credit = sum((ln.credit for ln in lines), Decimal("0"))
        diff = total_debit - total_credit
        if diff != 0:
            if abs(diff) > Decimal("1.00"):
                _log.error(
                    "Ecriture %s desequilibre anormal: debit=%s credit=%s ecart=%s",
                    z_ref, total_debit, total_credit, diff,
                )
            if diff > 0:
                lines.append(
                    JournalLine(
                        account_number=cfg["account_rounding_income"],
                        account_label="Produits divers de gestion (arrondi)",
                        debit=Decimal("0"),
                        credit=abs(diff),
                        label=f"Ajustement équilibre — {z_ref}",
                    )
                )
            else:
                lines.append(
                    JournalLine(
                        account_number=cfg["account_rounding_expense"],
                        account_label="Charges diverses de gestion (arrondi)",
                        debit=abs(diff),
                        credit=Decimal("0"),
                        label=f"Ajustement équilibre — {z_ref}",
                    )
                )

        return lines

    # ── Ecriture d'un Z (F2, idempotent) ────────────────────────────────

    async def create_export_for_z(
        self, z_report: ZReport, *, user_id=None
    ) -> tuple[AccountingExport, bool]:
        """Cree l'ecriture comptable d'un Z, dans la MEME transaction SQL que
        la cloture (appelant : `pos.py::close_drawer` / `fiscal.py::close_open_drawers`).
        Idempotent : renvoie l'existant si deja cree (pas de nouvel evenement
        JET). Retourne ``(export, created)``."""
        existing = (
            await self.db.execute(
                select(AccountingExport).where(AccountingExport.z_report_id == z_report.id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

        cfg = await self.get_config()
        lines = self.build_journal_lines(z_report, cfg)
        export_date = z_report.closed_at.date() if z_report.closed_at else datetime.now(timezone.utc).date()

        total_debit = sum((ln.debit for ln in lines), Decimal("0"))
        total_credit = sum((ln.credit for ln in lines), Decimal("0"))
        payment_totals = z_report.payment_totals or {}
        cash_net = _d((payment_totals.get("cash") or {}).get("net"))
        card_net = _d((payment_totals.get("card") or {}).get("net"))
        rounding = total_debit - total_credit

        # NB: `fec_content` DOIT etre fourni des la construction — les
        # tables PR4 sont immuables des l'INSERT (trigger migration 0005),
        # un UPDATE ulterieur (meme pour completer une colonne) serait donc
        # rejete par la base.
        export = AccountingExport(
            z_report_id=z_report.id,
            export_date=export_date,
            total_sales_ht=_d(z_report.total_ht),
            total_tva=_d(z_report.total_tva),
            total_ttc=_d(z_report.total_ht) + _d(z_report.total_tva),
            total_refunds_ttc=_d(z_report.total_refunds),
            total_cash=cash_net,
            total_card=card_net,
            total_debit=total_debit,
            total_credit=total_credit,
            rounding_adjustment=rounding,
            fec_content=self._generate_fec(lines, z_report, export_date, cfg),
        )
        self.db.add(export)
        await self.db.flush()

        z_ref = f"Z{z_report.report_number:04d}"
        for i, ln in enumerate(lines, start=1):
            self.db.add(
                AccountingExportLine(
                    export_id=export.id,
                    line_number=i,
                    account_number=ln.account_number,
                    account_label=ln.account_label,
                    label=ln.label,
                    debit=ln.debit,
                    credit=ln.credit,
                    piece_reference=z_ref,
                )
            )
        await self.db.flush()

        await JournalService(self.db).record(
            EVENT_ACCOUNTING_EXPORT_CREATED,
            user_id=user_id,
            payload={
                "z_number": z_report.report_number,
                "export_id": str(export.id),
                "total_debit": str(total_debit),
                "total_credit": str(total_credit),
                "balanced": total_debit == total_credit,
            },
        )
        await self.db.flush()
        return export, True

    # ── Verification (recalcul et comparaison — jamais de reecriture) ──

    async def verify_export(self, z_report: ZReport, *, user_id=None) -> dict:
        """Recalcule les lignes d'ecriture d'un Z et les compare a ce qui a
        ete persiste (§3 : `verify_export(z)` — F2 : jamais de reecriture,
        une divergence journalise `accounting.mismatch`)."""
        export = (
            await self.db.execute(
                select(AccountingExport).where(AccountingExport.z_report_id == z_report.id)
            )
        ).scalar_one_or_none()
        if export is None:
            return {"valid": False, "reason": "export_missing", "z_number": z_report.report_number}

        cfg = await self.get_config()
        recomputed = self.build_journal_lines(z_report, cfg)
        stored = sorted(export.lines, key=lambda ln: ln.line_number)

        mismatch = len(recomputed) != len(stored)
        if not mismatch:
            for a, b in zip(recomputed, stored):
                if (
                    a.account_number != b.account_number
                    or a.debit != _d(b.debit)
                    or a.credit != _d(b.credit)
                ):
                    mismatch = True
                    break

        if mismatch:
            await JournalService(self.db).record(
                EVENT_ACCOUNTING_MISMATCH,
                user_id=user_id,
                payload={
                    "z_number": z_report.report_number,
                    "export_id": str(export.id),
                    "recomputed_lines": len(recomputed),
                    "stored_lines": len(stored),
                },
            )
            await self.db.flush()
            return {"valid": False, "reason": "lines_mismatch", "z_number": z_report.report_number}

        return {"valid": True, "z_number": z_report.report_number}

    # ── FEC (Format d'Echange Comptable) ────────────────────────────────

    def _generate_fec(
        self, lines: list[JournalLine], z_report: ZReport, export_date: date, cfg: dict
    ) -> str:
        rows = ["\t".join(_FEC_COLUMNS)]
        ecriture_date = _fec_date(export_date)
        piece_ref = f"Z{z_report.report_number:04d}"
        valid_date = _fec_date(datetime.now(timezone.utc))

        for i, ln in enumerate(lines, start=1):
            rows.append(
                _fec_row(
                    JournalCode=cfg["journal_code"],
                    JournalLib="Ventes",
                    EcritureNum=f"{z_report.report_number:04d}-{i:03d}",
                    EcritureDate=ecriture_date,
                    CompteNum=ln.account_number,
                    CompteLib=ln.account_label,
                    CompAuxNum="",
                    CompAuxLib="",
                    PieceRef=piece_ref,
                    PieceDate=ecriture_date,
                    EcritureLib=ln.label,
                    Debit=f"{ln.debit:.2f}".replace(".", ",") if ln.debit else "0,00",
                    Credit=f"{ln.credit:.2f}".replace(".", ",") if ln.credit else "0,00",
                    EcritureLet="",
                    DateLet="",
                    ValidDate=valid_date,
                    Montantdevise="",
                    Idevise="EUR",
                )
            )
        return "\n".join(rows)

    async def generate_daily_fec(self, target_date: date) -> str:
        """FEC d'une journee = agregat des FEC des ecritures de la date."""
        stmt = (
            select(AccountingExport)
            .where(AccountingExport.export_date == target_date)
            .order_by(AccountingExport.created_at.asc())
        )
        exports = list((await self.db.execute(stmt)).scalars().all())
        rows = ["\t".join(_FEC_COLUMNS)]
        for exp in exports:
            if not exp.fec_content:
                continue
            for line in exp.fec_content.splitlines()[1:]:  # skip header
                rows.append(line)
        return "\n".join(rows)

    async def generate_monthly_fec(self, year: int, month: int) -> str:
        """FEC mensuel = agregat des FEC de toutes les ecritures du mois."""
        from calendar import monthrange

        last_day = monthrange(year, month)[1]
        date_from = date(year, month, 1)
        date_to = date(year, month, last_day)
        stmt = (
            select(AccountingExport)
            .where(AccountingExport.export_date >= date_from)
            .where(AccountingExport.export_date <= date_to)
            .order_by(AccountingExport.export_date.asc(), AccountingExport.created_at.asc())
        )
        exports = list((await self.db.execute(stmt)).scalars().all())
        rows = ["\t".join(_FEC_COLUMNS)]
        for exp in exports:
            if not exp.fec_content:
                continue
            for line in exp.fec_content.splitlines()[1:]:
                rows.append(line)
        return "\n".join(rows)

    # ── CSV mensuel Pennylane (F3) ───────────────────────────────────────

    async def generate_monthly_csv(self, year: int, month: int) -> str:
        """CSV mensuel des ecritures comptables — texte identique a
        l'application source (memes colonnes, virgule decimale, `\\r\\n`,
        regroupement par numero de piece Z####)."""
        from calendar import monthrange

        cfg = await self.get_config()
        last_day = monthrange(year, month)[1]
        date_from = date(year, month, 1)
        date_to = date(year, month, last_day)

        stmt = (
            select(AccountingExport)
            .where(AccountingExport.export_date >= date_from)
            .where(AccountingExport.export_date <= date_to)
            .order_by(AccountingExport.export_date.asc(), AccountingExport.created_at.asc())
        )
        exports = list((await self.db.execute(stmt)).scalars().all())

        rows = [_csv_row(_PENNYLANE_CSV_COLUMNS)]
        for exp in exports:
            lines = sorted(exp.lines or [], key=lambda ln: ln.line_number)
            if not lines:
                continue
            ecr_date = _csv_date(exp.export_date)
            piece_num = lines[0].piece_reference
            piece_label = f"Clôture caisse {piece_num} du {ecr_date}"
            for ln in lines:
                rows.append(
                    _csv_row(
                        [
                            ecr_date,  # Date
                            cfg["journal_code"],  # Code Journal
                            ln.account_number,  # Numéro de compte
                            ln.account_label,  # Libellé de compte
                            ln.label,  # Libellé de ligne
                            "",  # Taux de TVA du compte
                            "",  # Code pays du compte
                            piece_label,  # Libellé de pièce
                            piece_num,  # Numéro de pièce (regroupement)
                            _csv_amount(ln.debit),  # Débit et/ou Crédit
                            _csv_amount(ln.credit),  # Crédit
                            "",  # Famille de catégories
                            "",  # Catégorie
                            "",  # Identifiant de ligne
                            "",  # Identifiant de lettrage
                        ]
                    )
                )
        return "\r\n".join(rows) + "\r\n"
