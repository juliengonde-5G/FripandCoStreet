# Nouveau service (PR4, docs/ARCHITECTURE_PR4.md §1/§3, F5) — clotures
# periodiques scellees, extrait/etendu du module equivalent de l'application
# source (`services/fiscal_closure.py`) : verrou fiscal dedie, refus si
# caisse ouverte, verification des DEUX chaines (transactions + Z) ET du JET
# avant scellement, snapshot incluant en plus (vs. l'application source)
# les mouvements de caisse, le JET de la periode, les tickets texte et les
# reglages boutique (§2/§3 du contrat) — reutilise `FiscalExportService` pour
# la partie transactions/Z/mouvements/JET. Archive gzip `mtime=0`
# reproductible (deux generations => meme sha256), manifeste HMAC chaine a
# la cloture precedente (memes conventions que `services/fiscal.py`/
# `services/jet.py` : genesis "0", `FiscalService._hmac`/`_canonical`).
from __future__ import annotations

import gzip
import hashlib
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounting import AccountingExport
from app.models.fiscal_closure import ClosureType, FiscalClosure
from app.models.jet import JournalEvent
from app.models.pos import CashDrawer, Transaction, TransactionType, ZReport
from app.models.receipt import Receipt
from app.services.fiscal import FiscalService, PosServiceError
from app.services.fiscal_export import FiscalExportService
from app.services.jet import EVENT_CLOSURE_CREATED, EVENT_CLOSURE_FAILED, JournalService
from app.services.settings_service import SettingsService
from app.version import APP_VERSION, FISCAL_VERSION_DATE

SOFTWARE_VERSION = APP_VERSION

# Verrou avisory Postgres DEDIE aux clotures periodiques — distinct de celui
# des ventes/Z (FISCAL_WRITE_LOCK_KEY=5_252_026), du JET (837_120_001) et des
# parametres boutique (837_120_002) : une cloture ne doit jamais se glisser
# en concurrence d'une autre, mais n'a pas besoin de bloquer les ventes.
CLOSURE_WRITE_LOCK_KEY = 5_252_027

VALID_CLOSURE_TYPES = {"monthly", "annual", "manual"}


class ClosureDrawerOpen(PosServiceError):
    status_code = 409
    code = "drawer_open"

    def __init__(self):
        super().__init__("Fermez la caisse avant de produire une clôture fiscale.")


class ClosureChainInvalid(PosServiceError):
    status_code = 409
    code = "chain_invalid"

    def __init__(self, detail: dict):
        super().__init__("La chaîne fiscale n'est pas valide : clôture refusée.")
        self.detail = detail


class ClosurePeriodInvalid(PosServiceError):
    status_code = 400
    code = "invalid_period"


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


class FiscalClosureService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _fail(
        self, exc: PosServiceError, *, closure_type: str, user_id: uuid.UUID | None, **extra
    ) -> None:
        """Journalise `closure.failed` puis COMMIT explicitement avant de
        lever `exc` — indispensable ici : `app.core.database.get_db` fait un
        ROLLBACK automatique sur toute exception non geree remontant jusqu'au
        routeur (meme motif que `pos/router.py::_raise_printer_unreachable`),
        donc un simple `flush()` suivi d'un `raise` perdrait cet evenement."""
        await JournalService(self.db).record(
            EVENT_CLOSURE_FAILED,
            user_id=user_id,
            payload={"closure_type": closure_type, "reason": exc.code, **extra},
        )
        await self.db.commit()
        raise exc

    async def close_period(
        self,
        *,
        closure_type: str,
        period_start: datetime,
        period_end: datetime,
        user_id: uuid.UUID | None,
    ) -> FiscalClosure:
        if closure_type not in VALID_CLOSURE_TYPES:
            await self._fail(
                ClosurePeriodInvalid(f"Type de clôture invalide : {closure_type!r}"),
                closure_type=closure_type,
                user_id=user_id,
            )
        if period_start.tzinfo is None:
            period_start = period_start.replace(tzinfo=timezone.utc)
        if period_end.tzinfo is None:
            period_end = period_end.replace(tzinfo=timezone.utc)
        if period_start >= period_end:
            await self._fail(
                ClosurePeriodInvalid("Période de clôture invalide (début ≥ fin)."),
                closure_type=closure_type,
                user_id=user_id,
            )
        if period_end > datetime.now(timezone.utc):
            await self._fail(
                ClosurePeriodInvalid("Clôture future interdite."),
                closure_type=closure_type,
                user_id=user_id,
            )

        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": CLOSURE_WRITE_LOCK_KEY},
        )

        existing = (
            await self.db.execute(
                select(FiscalClosure).where(
                    FiscalClosure.closure_type == ClosureType(closure_type),
                    FiscalClosure.period_start == period_start,
                    FiscalClosure.period_end == period_end,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        open_drawer = (
            await self.db.execute(
                select(CashDrawer.id).where(CashDrawer.is_open.is_(True)).limit(1)
            )
        ).scalar_one_or_none()
        if open_drawer is not None:
            await self._fail(ClosureDrawerOpen(), closure_type=closure_type, user_id=user_id)

        fiscal = FiscalService(self.db)
        tx_integrity = await fiscal.verify_chain_integrity()
        z_integrity = await fiscal.verify_z_chain_integrity()
        jet_integrity = await JournalService(self.db).verify_chain()
        if not tx_integrity["valid"] or not z_integrity["valid"] or not jet_integrity["valid"]:
            await self._fail(
                ClosureChainInvalid(
                    {
                        "transactions": tx_integrity,
                        "z_reports": z_integrity,
                        "jet": jet_integrity,
                    }
                ),
                closure_type=closure_type,
                user_id=user_id,
            )

        txs = (
            await self.db.execute(
                select(Transaction)
                .where(Transaction.created_at >= period_start, Transaction.created_at < period_end)
                .order_by(Transaction.transaction_number.asc())
            )
        ).scalars().all()
        z_reports = (
            await self.db.execute(
                select(ZReport)
                .where(ZReport.created_at >= period_start, ZReport.created_at < period_end)
                .order_by(ZReport.report_number.asc())
            )
        ).scalars().all()

        shop = await SettingsService(self.db).get("shop")
        snapshot = await FiscalExportService(self.db).build_snapshot(
            period_from=period_start,
            period_to=period_end,
            merchant_name=shop.get("name") or "Frip & Co Street",
            merchant_id=shop.get("siret") or "",
        )

        # Ecritures comptables de la periode (F5 : la snapshot est
        # autoportante — pas besoin de rejouer le calcul comptable hors
        # application pour retrouver la ventilation par compte).
        z_ids = [z.id for z in z_reports]
        acct_lines: list[dict] = []
        if z_ids:
            exports = (
                await self.db.execute(
                    select(AccountingExport).where(AccountingExport.z_report_id.in_(z_ids))
                )
            ).scalars().all()
            for exp in exports:
                for ln in sorted(exp.lines, key=lambda x: x.line_number):
                    acct_lines.append(
                        {
                            "piece_reference": ln.piece_reference,
                            "account_number": ln.account_number,
                            "account_label": ln.account_label,
                            "label": ln.label,
                            "debit": _money(Decimal(str(ln.debit))),
                            "credit": _money(Decimal(str(ln.credit))),
                        }
                    )
        snapshot["accounting_export_lines"] = acct_lines

        # Tickets texte (F5 : "lisible hors application" — le contenu figé
        # du ticket, sans les compteurs mutables printed_count/duplicate_count).
        tx_ids = [t.id for t in txs]
        receipts_payload: list[dict] = []
        if tx_ids:
            receipts = (
                await self.db.execute(select(Receipt).where(Receipt.transaction_id.in_(tx_ids)))
            ).scalars().all()
            receipts_payload = [
                {"transaction_id": str(r.transaction_id), "content": r.content}
                for r in receipts
            ]
        snapshot["receipts"] = receipts_payload
        snapshot["shop_settings"] = shop

        # PR8/J5 — factures et avoirs professionnels de la periode. La cle
        # `invoices` vient de `FiscalExportService.build_snapshot` (appele
        # plus haut) : une seule serialisation pour l'export a la demande et
        # pour l'archive scellee. On la reaffirme ici — plutot que de la
        # supposer — parce que le contrat exige que l'archive la porte.
        snapshot.setdefault("invoices", [])

        snapshot["software_version"] = SOFTWARE_VERSION
        snapshot["fiscal_version_date"] = FISCAL_VERSION_DATE
        snapshot["integrity"] = {
            "transactions": tx_integrity,
            "z_reports": z_integrity,
            "jet": jet_integrity,
        }
        snapshot["closure_notice_fr"] = (
            "Cette archive fiscale auto-descriptive (art. 286 I-3° bis du CGI) "
            "contient toutes les ventes, annulations, clôtures Z, mouvements "
            "de caisse, événements techniques (JET), tickets, factures et "
            "avoirs professionnels, et réglages boutique de la période. "
            "Elle se lit hors application (JSON "
            "compressé gzip) : décompresser, vérifier le SHA-256 affiché "
            "(en-tête X-Archive-SHA256), puis comparer les totaux "
            "grand_total_period aux clôtures Z correspondantes."
        )

        raw_archive = FiscalService._canonical(snapshot)
        archive = gzip.compress(raw_archive, compresslevel=9, mtime=0)
        archive_sha = hashlib.sha256(archive).hexdigest()

        period_sales = sum(
            (Decimal(str(t.total_ttc)) for t in txs if t.transaction_type == TransactionType.sale),
            Decimal("0"),
        )
        period_refunds = sum(
            (Decimal(str(t.total_ttc)) for t in txs if t.transaction_type == TransactionType.refund),
            Decimal("0"),
        )

        perpetual_rows = (
            await self.db.execute(select(Transaction).where(Transaction.created_at < period_end))
        ).scalars().all()
        perpetual_sales = sum(
            (Decimal(str(t.total_ttc)) for t in perpetual_rows if t.transaction_type == TransactionType.sale),
            Decimal("0"),
        )
        perpetual_refunds = sum(
            (Decimal(str(t.total_ttc)) for t in perpetual_rows if t.transaction_type == TransactionType.refund),
            Decimal("0"),
        )

        jet_last = (
            await self.db.execute(select(JournalEvent).order_by(JournalEvent.seq.desc()).limit(1))
        ).scalar_one_or_none()

        previous = (
            await self.db.execute(
                select(FiscalClosure).order_by(FiscalClosure.sequence_number.desc()).limit(1)
            )
        ).scalar_one_or_none()
        previous_hash = previous.hash if previous else "0"
        sequence_number = (previous.sequence_number + 1) if previous else 1

        manifest = {
            "closure_type": closure_type,
            "sequence_number": sequence_number,
            "period_start": period_start.astimezone(timezone.utc).isoformat(),
            "period_end": period_end.astimezone(timezone.utc).isoformat(),
            "software_version": SOFTWARE_VERSION,
            "fiscal_version_date": FISCAL_VERSION_DATE,
            "transaction_count": len(txs),
            "first_transaction_number": txs[0].transaction_number if txs else None,
            "last_transaction_number": txs[-1].transaction_number if txs else None,
            "last_transaction_hash": txs[-1].hash_chain if txs else None,
            "z_report_count": len(z_reports),
            "first_z_number": z_reports[0].report_number if z_reports else None,
            "last_z_number": z_reports[-1].report_number if z_reports else None,
            "last_z_hash": z_reports[-1].hash if z_reports else None,
            "jet_last_seq": jet_last.seq if jet_last else None,
            "jet_last_hash": jet_last.hash if jet_last else None,
            "grand_total_period": {
                "sales_ttc": _money(period_sales),
                "refunds_ttc": _money(period_refunds),
                "net_ttc": _money(period_sales - period_refunds),
            },
            "total_perpetual": {
                "transactions_count": len(perpetual_rows),
                "sales_ttc": _money(perpetual_sales),
                "refunds_ttc": _money(perpetual_refunds),
                "net_ttc": _money(perpetual_sales - perpetual_refunds),
            },
            "archive_sha256": archive_sha,
            "previous_hash": previous_hash,
        }

        closure = FiscalClosure(
            sequence_number=sequence_number,
            closure_type=ClosureType(closure_type),
            period_start=period_start,
            period_end=period_end,
            software_version=SOFTWARE_VERSION,
            fiscal_version_date=FISCAL_VERSION_DATE,
            transaction_count=len(txs),
            first_transaction_number=manifest["first_transaction_number"],
            last_transaction_number=manifest["last_transaction_number"],
            last_transaction_hash=manifest["last_transaction_hash"],
            first_z_number=manifest["first_z_number"],
            last_z_number=manifest["last_z_number"],
            last_z_hash=manifest["last_z_hash"],
            jet_last_seq=manifest["jet_last_seq"],
            jet_last_hash=manifest["jet_last_hash"],
            grand_total_sales=period_sales,
            grand_total_refunds=period_refunds,
            grand_total_net=period_sales - period_refunds,
            perpetual_sales=perpetual_sales,
            perpetual_refunds=perpetual_refunds,
            perpetual_net=perpetual_sales - perpetual_refunds,
            perpetual_transaction_count=len(perpetual_rows),
            archive_sha256=archive_sha,
            archive_content=archive,
            archive_size=len(archive),
            manifest=manifest,
            previous_hash=previous_hash,
            hash=FiscalService._hmac(manifest),
            signature_version=1,
            closed_by_user_id=user_id,
        )
        self.db.add(closure)
        await self.db.flush()

        await JournalService(self.db).record(
            EVENT_CLOSURE_CREATED,
            user_id=user_id,
            payload={
                "closure_type": closure_type,
                "sequence": sequence_number,
                "period_start": manifest["period_start"],
                "period_end": manifest["period_end"],
                "sha256": archive_sha,
            },
        )
        await self.db.flush()
        await self.db.refresh(closure)
        return closure

    async def verify_chain(self) -> dict:
        rows = (
            await self.db.execute(select(FiscalClosure).order_by(FiscalClosure.sequence_number.asc()))
        ).scalars().all()
        previous_hash = "0"
        for closure in rows:
            if closure.previous_hash != previous_hash:
                return {
                    "valid": False,
                    "broken_at": closure.sequence_number,
                    "reason": "previous_hash_mismatch",
                }
            if hashlib.sha256(closure.archive_content).hexdigest() != closure.archive_sha256:
                return {
                    "valid": False,
                    "broken_at": closure.sequence_number,
                    "reason": "archive_sha256_mismatch",
                }
            if FiscalService._hmac(closure.manifest) != closure.hash:
                return {
                    "valid": False,
                    "broken_at": closure.sequence_number,
                    "reason": "signature_mismatch",
                }
            previous_hash = closure.hash
        return {"valid": True, "checked": len(rows)}

    @staticmethod
    def serialize(closure: FiscalClosure) -> dict:
        return {
            "id": str(closure.id),
            "sequence_number": closure.sequence_number,
            "closure_type": closure.closure_type.value,
            "period_start": closure.period_start.isoformat(),
            "period_end": closure.period_end.isoformat(),
            "software_version": closure.software_version,
            "fiscal_version_date": closure.fiscal_version_date,
            "transaction_count": closure.transaction_count,
            "first_transaction_number": closure.first_transaction_number,
            "last_transaction_number": closure.last_transaction_number,
            "last_transaction_hash": closure.last_transaction_hash,
            "first_z_number": closure.first_z_number,
            "last_z_number": closure.last_z_number,
            "last_z_hash": closure.last_z_hash,
            "grand_total_sales": float(closure.grand_total_sales),
            "grand_total_refunds": float(closure.grand_total_refunds),
            "grand_total_net": float(closure.grand_total_net),
            "perpetual_sales": float(closure.perpetual_sales),
            "perpetual_refunds": float(closure.perpetual_refunds),
            "perpetual_net": float(closure.perpetual_net),
            "perpetual_transaction_count": closure.perpetual_transaction_count,
            "previous_hash": closure.previous_hash,
            "hash": closure.hash,
            "archive_sha256": closure.archive_sha256,
            "archive_size": closure.archive_size,
            "manifest": closure.manifest,
            "created_at": closure.created_at.isoformat() if closure.created_at else None,
        }
