# Extrait de l'application source (apps/api/app/services/fiscal.py) — signature v3 (D3) :
# aucune branche legacy v1/v2, nouvelle installation. Verrou de caisse
# partage entre vente et cloture (D10) : une seule cle avisory Postgres pour
# les deux, contrairement a l'application source qui en utilisait deux distinctes.
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.cash_movement import CashMovement, CashMovementDirection
from app.models.pos import (
    CashDrawer,
    Payment,
    PaymentMethod,
    Transaction,
    TransactionItem,
    TransactionType,
    ZReport,
)
from app.services.jet import EVENT_DRAWER_AUTO_CLOSED, JournalService
from app.version import FISCAL_SIGNATURE_VERSION

GENESIS_HASH = "0"

# Verrou avisory Postgres serialisant TOUTE ecriture fiscale — ventes ET
# clotures de caisse (D10) : aucune vente ne peut se glisser entre le calcul
# d'un Z et sa signature. `PosService.create_transaction` et
# `PosService.close_drawer` acquierent cette meme cle avant de lire/ecrire.
FISCAL_WRITE_LOCK_KEY = 5_252_026


# ---------------------------------------------------------------------------
# Erreurs metier partagees (pos.py / refund.py / api/pos/router.py) — chaque
# sous-classe porte le code HTTP et le `code` machine attendus par le
# contrat (§5 : erreurs `{"detail": "...", "code": "snake_case"}`).
# ---------------------------------------------------------------------------


class PosServiceError(Exception):
    status_code = 422
    code = "pos_error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class DrawerClosed(PosServiceError):
    status_code = 409
    code = "drawer_closed"

    def __init__(self):
        super().__init__("Caisse fermée : ouvrez la caisse avant d'encaisser.")


class DrawerAlreadyOpen(PosServiceError):
    status_code = 409
    code = "drawer_already_open"

    def __init__(self):
        super().__init__("Une caisse est déjà ouverte.")


class NoOpenDrawer(PosServiceError):
    status_code = 409
    code = "no_open_drawer"

    def __init__(self):
        super().__init__("Aucune caisse ouverte.")


async def acquire_fiscal_write_lock(db: AsyncSession) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": FISCAL_WRITE_LOCK_KEY})


class FiscalService:
    """Chaine de preuve NF525 (auto-attestation) : signature v3 HMAC-SHA256
    des ventes/annulations et des rapports Z, verification d'integrite."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Canonicalisation / HMAC — identique au JET (meme convention de projet)
    # ------------------------------------------------------------------

    @staticmethod
    def _money(value) -> str:
        return f"{Decimal(str(value or 0)).quantize(Decimal('0.01')):.2f}"

    @staticmethod
    def _rate(value) -> str:
        return f"{Decimal(str(value or 0)).quantize(Decimal('0.01')):.2f}"

    @staticmethod
    def _iso(value: datetime | None) -> str:
        if value is None:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _canonical(payload: dict) -> bytes:
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @classmethod
    def _hmac(cls, payload: dict) -> str:
        return hmac.new(
            settings.FISCAL_SIGNING_KEY.encode("utf-8"), cls._canonical(payload), hashlib.sha256
        ).hexdigest()

    # ------------------------------------------------------------------
    # Chaine des transactions (ventes + annulations)
    # ------------------------------------------------------------------

    async def sign_transaction(self, transaction: Transaction) -> None:
        """Scelle une transaction (vente ou annulation) — payload v3.

        Appelee APRES que les lignes/paiements ont ete ajoutes a la session
        et flushes (leurs `id`/`created_at` sont donc definitifs) mais AVANT
        le commit : `transaction.hash_chain` passe de `""` a sa valeur
        definitive en une seule UPDATE, ce que le trigger d'immuabilite
        autorise explicitement (transition depuis hash_chain='').
        """
        await self.db.flush()
        previous_hash = await self._get_previous_transaction_hash(
            before_number=transaction.transaction_number
        )
        transaction.previous_hash = previous_hash
        transaction.fiscal_signature_version = FISCAL_SIGNATURE_VERSION
        payload = await self._transaction_payload(transaction, previous_hash)
        transaction.hash_chain = self._hmac(payload)
        await self.db.flush()

    async def _get_previous_transaction_hash(self, before_number: int | None = None) -> str:
        query = select(Transaction.hash_chain).where(Transaction.hash_chain != "")
        if before_number is not None:
            query = query.where(Transaction.transaction_number < before_number)
        row = (
            await self.db.execute(query.order_by(Transaction.transaction_number.desc()).limit(1))
        ).scalar_one_or_none()
        return row if row else GENESIS_HASH

    async def _transaction_payload(self, transaction: Transaction, previous_hash: str) -> dict:
        items = (
            await self.db.execute(
                select(TransactionItem)
                .where(TransactionItem.transaction_id == transaction.id)
                .order_by(TransactionItem.position.asc())
            )
        ).scalars().all()
        payments = (
            await self.db.execute(
                select(Payment)
                .where(Payment.transaction_id == transaction.id)
                .order_by(Payment.created_at.asc(), Payment.id.asc())
            )
        ).scalars().all()
        return {
            "signature_version": FISCAL_SIGNATURE_VERSION,
            "previous_hash": previous_hash,
            "transaction": {
                "id": str(transaction.id),
                "number": transaction.transaction_number,
                "type": transaction.transaction_type.value,
                "created_at": self._iso(transaction.created_at),
                "user_id": str(transaction.user_id),
                "client_uuid": str(transaction.client_uuid) if transaction.client_uuid else None,
                "original_transaction_id": (
                    str(transaction.original_transaction_id)
                    if transaction.original_transaction_id
                    else None
                ),
                "refund_reason": transaction.refund_reason,
                "discount_type": (
                    transaction.discount_type.value if transaction.discount_type else None
                ),
                "discount_value": (
                    self._money(transaction.discount_value)
                    if transaction.discount_value is not None
                    else None
                ),
                "discount_amount": self._money(transaction.discount_amount),
                "tva_rate": self._rate(transaction.tva_rate),
                "total_ht": self._money(transaction.total_ht),
                "total_tva": self._money(transaction.total_tva),
                "total_ttc": self._money(transaction.total_ttc),
            },
            "items": [
                {
                    "position": item.position,
                    "label": item.label,
                    "quantity": item.quantity,
                    "unit_price": self._money(item.unit_price),
                    "discount_amount": self._money(item.discount_amount),
                    "line_total": self._money(item.line_total),
                    "tva_rate": self._rate(item.tva_rate),
                    "line_ht": self._money(item.line_ht),
                    "line_tva": self._money(item.line_tva),
                    "original_item_id": (
                        str(item.original_transaction_item_id)
                        if item.original_transaction_item_id
                        else None
                    ),
                }
                for item in items
            ],
            "payments": [
                {
                    "method": payment.method.value,
                    "amount": self._money(payment.amount),
                    "tendered_amount": (
                        self._money(payment.tendered_amount)
                        if payment.tendered_amount is not None
                        else None
                    ),
                    "change_amount": (
                        self._money(payment.change_amount)
                        if payment.change_amount is not None
                        else None
                    ),
                    "sumup_checkout_id": payment.sumup_checkout_id,
                    "sumup_transaction_id": payment.sumup_transaction_id,
                    "sumup_transaction_code": payment.sumup_transaction_code,
                    "sumup_auth_code": payment.sumup_auth_code,
                    "sumup_card_brand": payment.sumup_card_brand,
                    "sumup_card_last4": payment.sumup_card_last4,
                }
                for payment in payments
            ],
        }

    async def verify_chain_integrity(self) -> dict:
        """Recalcule chaque hash de vente/annulation et verifie le maillage."""
        transactions = (
            await self.db.execute(select(Transaction).order_by(Transaction.transaction_number.asc()))
        ).scalars().all()

        previous_hash = GENESIS_HASH
        for t in transactions:
            if not t.hash_chain:
                return {
                    "valid": False,
                    "checked": t.transaction_number,
                    "broken_at": t.transaction_number,
                    "reason": "unsigned_transaction",
                    "message": f"transaction non signée n° {t.transaction_number}",
                }
            if t.previous_hash != previous_hash:
                return {
                    "valid": False,
                    "checked": t.transaction_number,
                    "broken_at": t.transaction_number,
                    "reason": "previous_hash_mismatch",
                }
            expected = self._hmac(await self._transaction_payload(t, previous_hash))
            if not hmac.compare_digest(t.hash_chain or "", expected):
                return {
                    "valid": False,
                    "checked": t.transaction_number,
                    "broken_at": t.transaction_number,
                    "reason": "signature_mismatch",
                }
            previous_hash = t.hash_chain
        return {"valid": True, "checked": len(transactions)}

    # ------------------------------------------------------------------
    # Rapports Z
    # ------------------------------------------------------------------

    async def generate_z_report(
        self,
        drawer: CashDrawer,
        user_id: uuid.UUID,
        *,
        counted: bool = True,
        is_regularization: bool = False,
        regularization_reason: str | None = None,
    ) -> ZReport:
        """Genere et scelle le Z d'un tiroir clos (D9) — sous le verrou de
        caisse partage avec les ventes (D10)."""
        await acquire_fiscal_write_lock(self.db)

        closed_at = drawer.closed_at or datetime.now(timezone.utc)

        tx_rows = (
            await self.db.execute(
                select(Transaction)
                .where(
                    Transaction.created_at >= drawer.opened_at,
                    Transaction.created_at <= closed_at,
                )
                .order_by(Transaction.transaction_number.asc())
            )
        ).scalars().all()

        total_sales = Decimal("0")
        total_refunds = Decimal("0")
        total_ht = Decimal("0")
        total_tva = Decimal("0")
        for t in tx_rows:
            if t.transaction_type == TransactionType.sale:
                total_sales += Decimal(str(t.total_ttc))
                total_ht += Decimal(str(t.total_ht))
                total_tva += Decimal(str(t.total_tva))
            else:
                total_refunds += Decimal(str(t.total_ttc))
                total_ht -= Decimal(str(t.total_ht))
                total_tva -= Decimal(str(t.total_tva))
        total_net = total_sales - total_refunds
        transaction_count = len(tx_rows)

        payment_totals: dict[str, dict[str, str]] = {}
        if tx_rows:
            tx_ids = [t.id for t in tx_rows]
            payment_rows = (
                await self.db.execute(
                    select(Payment.method, Transaction.transaction_type, Payment.amount)
                    .join(Transaction, Payment.transaction_id == Transaction.id)
                    .where(Transaction.id.in_(tx_ids))
                )
            ).all()
            accum: dict[str, dict[str, Decimal]] = {}
            for method, tx_type, amount in payment_rows:
                key = method.value if isinstance(method, PaymentMethod) else str(method)
                bucket = accum.setdefault(key, {"sales": Decimal("0"), "refunds": Decimal("0")})
                field = "refunds" if tx_type == TransactionType.refund else "sales"
                bucket[field] += Decimal(str(amount))
            for key, bucket in accum.items():
                payment_totals[key] = {
                    "sales": self._money(bucket["sales"]),
                    "refunds": self._money(bucket["refunds"]),
                    "net": self._money(bucket["sales"] - bucket["refunds"]),
                }

        first_number = tx_rows[0].transaction_number if tx_rows else None
        last_number = tx_rows[-1].transaction_number if tx_rows else None
        last_hash = tx_rows[-1].hash_chain if tx_rows else GENESIS_HASH

        # Especes attendues (D11) : ouverture + ventes especes - remboursements
        # especes + entrees - sorties.
        cash_sales = Decimal(
            str(
                (
                    await self.db.execute(
                        select(func.coalesce(func.sum(Payment.amount), 0))
                        .join(Transaction, Payment.transaction_id == Transaction.id)
                        .where(
                            Payment.method == PaymentMethod.cash,
                            Transaction.id.in_([t.id for t in tx_rows]) if tx_rows else False,
                            Transaction.transaction_type == TransactionType.sale,
                        )
                    )
                ).scalar_one()
            )
        )
        cash_refunds = Decimal(
            str(
                (
                    await self.db.execute(
                        select(func.coalesce(func.sum(Payment.amount), 0))
                        .join(Transaction, Payment.transaction_id == Transaction.id)
                        .where(
                            Payment.method == PaymentMethod.cash,
                            Transaction.id.in_([t.id for t in tx_rows]) if tx_rows else False,
                            Transaction.transaction_type == TransactionType.refund,
                        )
                    )
                ).scalar_one()
            )
        )
        movement_rows = (
            await self.db.execute(
                select(CashMovement)
                .where(CashMovement.drawer_id == drawer.id)
                .order_by(CashMovement.created_at.asc())
            )
        ).scalars().all()
        cash_in = sum(
            (Decimal(str(m.amount)) for m in movement_rows if m.direction == CashMovementDirection.inflow),
            Decimal("0"),
        )
        cash_out = sum(
            (Decimal(str(m.amount)) for m in movement_rows if m.direction == CashMovementDirection.outflow),
            Decimal("0"),
        )
        opening_amount = Decimal(str(drawer.opening_amount))
        expected_amount = opening_amount + cash_sales - cash_refunds + cash_in - cash_out
        closing_amount = (
            Decimal(str(drawer.closing_amount))
            if drawer.closing_amount is not None
            else expected_amount
        )
        discrepancy = closing_amount - expected_amount

        next_number = (
            await self.db.execute(select(func.coalesce(func.max(ZReport.report_number), 0)))
        ).scalar_one() + 1
        previous_hash = (
            await self.db.execute(
                select(ZReport.hash).order_by(ZReport.report_number.desc()).limit(1)
            )
        ).scalar_one_or_none() or GENESIS_HASH

        prev_cumulative = (
            await self.db.execute(
                select(
                    ZReport.cumulative_sales,
                    ZReport.cumulative_refunds,
                    ZReport.cumulative_net,
                    ZReport.cumulative_transaction_count,
                )
                .order_by(ZReport.report_number.desc())
                .limit(1)
            )
        ).one_or_none()
        if prev_cumulative is None:
            cum_sales = Decimal("0")
            cum_refunds = Decimal("0")
            cum_net = Decimal("0")
            cum_count = 0
        else:
            cum_sales = Decimal(str(prev_cumulative[0]))
            cum_refunds = Decimal(str(prev_cumulative[1]))
            cum_net = Decimal(str(prev_cumulative[2]))
            cum_count = int(prev_cumulative[3])
        cum_sales += total_sales
        cum_refunds += total_refunds
        cum_net += total_net
        cum_count += transaction_count

        cash_movements_payload = [
            {
                "id": str(m.id),
                "direction": m.direction.value,
                "amount": self._money(m.amount),
                "reason": m.reason.value,
                "created_at": self._iso(m.created_at),
            }
            for m in movement_rows
        ]

        z_payload = {
            "signature_version": FISCAL_SIGNATURE_VERSION,
            "previous_hash": previous_hash,
            "report_number": next_number,
            "drawer_id": str(drawer.id),
            "opened_at": self._iso(drawer.opened_at),
            "closed_at": self._iso(closed_at),
            "total_sales": self._money(total_sales),
            "total_refunds": self._money(total_refunds),
            "total_net": self._money(total_net),
            "total_ht": self._money(total_ht),
            "total_tva": self._money(total_tva),
            "transaction_count": transaction_count,
            "first_transaction_number": first_number,
            "last_transaction_number": last_number,
            "last_transaction_hash": last_hash,
            "payment_totals": payment_totals,
            "opening_amount": self._money(opening_amount),
            "closing_amount": self._money(closing_amount),
            "expected_amount": self._money(expected_amount),
            "discrepancy": self._money(discrepancy),
            "cash_in_total": self._money(cash_in),
            "cash_out_total": self._money(cash_out),
            "cash_movement_count": len(movement_rows),
            "cash_movements": cash_movements_payload,
            "counted": bool(counted),
            "cumulative_sales": self._money(cum_sales),
            "cumulative_refunds": self._money(cum_refunds),
            "cumulative_net": self._money(cum_net),
            "cumulative_transaction_count": cum_count,
            "is_regularization": is_regularization,
            "regularization_reason": regularization_reason,
        }
        report_hash = self._hmac(z_payload)

        z_report = ZReport(
            report_number=next_number,
            user_id=user_id,
            cash_drawer_id=drawer.id,
            opened_at=drawer.opened_at,
            closed_at=closed_at,
            total_sales=float(total_sales),
            total_refunds=float(total_refunds),
            total_net=float(total_net),
            total_ht=float(total_ht),
            total_tva=float(total_tva),
            transaction_count=transaction_count,
            first_transaction_number=first_number,
            last_transaction_number=last_number,
            last_transaction_hash=last_hash,
            payment_totals=payment_totals,
            opening_amount=float(opening_amount),
            closing_amount=float(closing_amount),
            expected_amount=float(expected_amount),
            discrepancy=float(discrepancy),
            cash_in_total=float(cash_in),
            cash_out_total=float(cash_out),
            cash_movement_count=len(movement_rows),
            counted=bool(counted),
            is_regularization=is_regularization,
            regularization_reason=regularization_reason,
            cumulative_sales=float(cum_sales),
            cumulative_refunds=float(cum_refunds),
            cumulative_net=float(cum_net),
            cumulative_transaction_count=cum_count,
            hash=report_hash,
            previous_hash=previous_hash,
            fiscal_signature_version=FISCAL_SIGNATURE_VERSION,
        )
        self.db.add(z_report)
        await self.db.flush()

        drawer.closing_amount = float(closing_amount)
        drawer.expected_amount = float(expected_amount)
        drawer.discrepancy = float(discrepancy)
        drawer.closed_at = closed_at
        drawer.is_open = False
        drawer.z_report_id = z_report.id

        await self.db.flush()
        await self.db.refresh(z_report)
        return z_report

    async def verify_z_chain_integrity(self) -> dict:
        """Recalcule chaque hash de Z, verifie le maillage ET la complétude
        (nombre de transactions couvertes == transaction_count, C-3)."""
        reports = (
            await self.db.execute(select(ZReport).order_by(ZReport.report_number.asc()))
        ).scalars().all()

        previous_hash = GENESIS_HASH
        for report in reports:
            if not report.hash:
                return {
                    "valid": False,
                    "checked": report.report_number,
                    "broken_at": report.report_number,
                    "reason": "unsigned_z_report",
                    "message": f"rapport Z non signé n° {report.report_number}",
                }
            if report.previous_hash != previous_hash:
                return {
                    "valid": False,
                    "checked": report.report_number,
                    "broken_at": report.report_number,
                    "reason": "previous_hash_mismatch",
                }
            drawer = (
                await self.db.execute(select(CashDrawer).where(CashDrawer.id == report.cash_drawer_id))
            ).scalar_one_or_none()
            if drawer is None:
                return {
                    "valid": False,
                    "checked": report.report_number,
                    "broken_at": report.report_number,
                    "reason": "drawer_missing",
                }
            actual_count = (
                await self.db.execute(
                    select(func.count(Transaction.id)).where(
                        Transaction.created_at >= report.opened_at,
                        Transaction.created_at <= report.closed_at,
                    )
                )
            ).scalar_one()
            if int(actual_count) != int(report.transaction_count):
                return {
                    "valid": False,
                    "checked": report.report_number,
                    "broken_at": report.report_number,
                    "reason": "completeness_mismatch",
                }
            movement_rows = (
                await self.db.execute(
                    select(CashMovement)
                    .where(CashMovement.drawer_id == drawer.id)
                    .order_by(CashMovement.created_at.asc())
                )
            ).scalars().all()
            payload = {
                "signature_version": report.fiscal_signature_version,
                "previous_hash": previous_hash,
                "report_number": report.report_number,
                "drawer_id": str(report.cash_drawer_id),
                "opened_at": self._iso(report.opened_at),
                "closed_at": self._iso(report.closed_at),
                "total_sales": self._money(report.total_sales),
                "total_refunds": self._money(report.total_refunds),
                "total_net": self._money(report.total_net),
                "total_ht": self._money(report.total_ht),
                "total_tva": self._money(report.total_tva),
                "transaction_count": report.transaction_count,
                "first_transaction_number": report.first_transaction_number,
                "last_transaction_number": report.last_transaction_number,
                "last_transaction_hash": report.last_transaction_hash,
                "payment_totals": report.payment_totals or {},
                "opening_amount": self._money(report.opening_amount),
                "closing_amount": self._money(report.closing_amount),
                "expected_amount": self._money(report.expected_amount),
                "discrepancy": self._money(report.discrepancy),
                "cash_in_total": self._money(report.cash_in_total),
                "cash_out_total": self._money(report.cash_out_total),
                "cash_movement_count": report.cash_movement_count,
                "cash_movements": [
                    {
                        "id": str(m.id),
                        "direction": m.direction.value,
                        "amount": self._money(m.amount),
                        "reason": m.reason.value,
                        "created_at": self._iso(m.created_at),
                    }
                    for m in movement_rows
                ],
                "counted": bool(report.counted),
                "cumulative_sales": self._money(report.cumulative_sales),
                "cumulative_refunds": self._money(report.cumulative_refunds),
                "cumulative_net": self._money(report.cumulative_net),
                "cumulative_transaction_count": report.cumulative_transaction_count,
                "is_regularization": bool(report.is_regularization),
                "regularization_reason": report.regularization_reason,
            }
            expected = self._hmac(payload)
            if not hmac.compare_digest(report.hash, expected):
                return {
                    "valid": False,
                    "checked": report.report_number,
                    "broken_at": report.report_number,
                    "reason": "signature_mismatch",
                }
            previous_hash = report.hash
        return {"valid": True, "checked": len(reports)}

    # ------------------------------------------------------------------
    # Garde fiscale 23:59 (§4.3) — ferme toute caisse oubliee.
    # ------------------------------------------------------------------

    async def close_open_drawers(self, user_id: uuid.UUID | None) -> list[ZReport]:
        await acquire_fiscal_write_lock(self.db)
        open_rows = (
            await self.db.execute(select(CashDrawer).where(CashDrawer.is_open.is_(True)))
        ).scalars().all()
        if not open_rows:
            return []
        reports: list[ZReport] = []
        now = datetime.now(timezone.utc)
        for drawer in open_rows:
            drawer.closed_at = now
            drawer.closed_by_guard = True
            drawer.closing_note = (
                (drawer.closing_note + " | " if drawer.closing_note else "")
                + f"Clôturée automatiquement le {now.strftime('%d/%m/%Y %H:%M')} (UTC) "
                "par la garde fiscale 23:59."
            )
            await self.db.flush()
            z_report = await self.generate_z_report(drawer, user_id or drawer.user_id, counted=False)
            await JournalService(self.db).record(
                EVENT_DRAWER_AUTO_CLOSED,
                user_id=user_id,
                payload={
                    "drawer_id": str(drawer.id),
                    "z_number": z_report.report_number,
                    "expected_amount": float(z_report.expected_amount),
                },
            )
            # PR4 (F2, docs/ARCHITECTURE_PR4.md §1/§3) — meme regle que
            # `pos.py::close_drawer` : l'ecriture comptable du Z est creee
            # dans la MEME transaction SQL que la cloture automatique.
            from app.services.accounting_service import AccountingService

            await AccountingService(self.db).create_export_for_z(z_report, user_id=user_id)
            await self.db.flush()
            reports.append(z_report)
        return reports

    # ------------------------------------------------------------------
    # Régularisation a posteriori (jour de caisse oublié)
    # ------------------------------------------------------------------

    async def _orphan_transactions(
        self, period_from: datetime, period_to: datetime
    ) -> tuple[list[Transaction], list[Transaction]]:
        tx_rows = (
            await self.db.execute(
                select(Transaction).where(
                    Transaction.created_at >= period_from, Transaction.created_at <= period_to
                )
            )
        ).scalars().all()
        drawers = (
            await self.db.execute(select(CashDrawer).where(CashDrawer.opened_at <= period_to))
        ).scalars().all()
        now = datetime.now(timezone.utc)

        def _covered(created_at: datetime) -> bool:
            for d in drawers:
                end = d.closed_at or now
                if d.opened_at <= created_at <= end:
                    return True
            return False

        orphans = [t for t in tx_rows if not _covered(t.created_at)]
        covered = [t for t in tx_rows if _covered(t.created_at)]
        return orphans, covered

    async def preview_regularization(self, period_from: datetime, period_to: datetime) -> dict:
        orphans, covered = await self._orphan_transactions(period_from, period_to)
        total_sales = sum(
            (Decimal(str(t.total_ttc)) for t in orphans if t.transaction_type == TransactionType.sale),
            Decimal("0"),
        )
        total_refunds = sum(
            (
                Decimal(str(t.total_ttc))
                for t in orphans
                if t.transaction_type == TransactionType.refund
            ),
            Decimal("0"),
        )
        return {
            "period_from": period_from.isoformat(),
            "period_to": period_to.isoformat(),
            "orphan_count": len(orphans),
            "covered_count": len(covered),
            "has_overlap": len(covered) > 0,
            "can_regularize": len(orphans) > 0 and len(covered) == 0,
            "total_sales": float(total_sales),
            "total_refunds": float(total_refunds),
            "total_net": float(total_sales - total_refunds),
            "transaction_numbers": sorted(t.transaction_number for t in orphans)[:100],
        }

    async def create_regularization_z(
        self, period_from: datetime, period_to: datetime, reason: str, user_id: uuid.UUID
    ) -> ZReport:
        reason = (reason or "").strip()
        if not reason:
            raise PosServiceError(
                "Motif de régularisation obligatoire.",
                code="regularization_reason_required",
                status_code=400,
            )
        orphans, covered = await self._orphan_transactions(period_from, period_to)
        if covered:
            raise PosServiceError(
                "La période chevauche une session de caisse existante "
                f"({len(covered)} transaction(s) déjà couverte(s)).",
                code="regularization_overlap",
                status_code=409,
            )
        if not orphans:
            raise PosServiceError(
                "Aucune transaction orpheline à régulariser sur cette période.",
                code="regularization_empty",
                status_code=400,
            )
        cash_expected = sum(
            (
                Decimal(str(p.amount))
                for t in orphans
                for p in (t.payments or [])
                if p.method == PaymentMethod.cash
            ),
            Decimal("0"),
        )
        now = datetime.now(timezone.utc)
        drawer = CashDrawer(
            user_id=user_id,
            opened_at=period_from,
            closed_at=period_to,
            opening_amount=0,
            closing_amount=float(cash_expected),
            is_open=False,
            closing_note=(
                f"Régularisation a posteriori — établie le {now.strftime('%d/%m/%Y %H:%M')} "
                f"(UTC). Comptage espèces non effectué. Motif : {reason}"
            ),
        )
        self.db.add(drawer)
        await self.db.flush()
        z_report = await self.generate_z_report(
            drawer,
            user_id,
            counted=False,
            is_regularization=True,
            regularization_reason=reason,
        )

        # PR4 (F2, docs/ARCHITECTURE_PR4.md §1/§3) — meme regle que
        # `pos.py::close_drawer` et `close_open_drawers` ci-dessus : sans
        # cet appel, un Z de regularisation resterait absent du CSV mensuel
        # et du FEC (la journee regularisee serait silencieusement
        # incomplete cote comptabilite, alors que la vente y figure bien
        # fiscalement). Meme transaction SQL que la creation du Z.
        from app.services.accounting_service import AccountingService

        await AccountingService(self.db).create_export_for_z(z_report, user_id=user_id)
        await self.db.flush()

        return z_report
