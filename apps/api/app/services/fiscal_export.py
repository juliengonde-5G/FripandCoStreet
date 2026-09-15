# Nouveau service (PR4, docs/ARCHITECTURE_PR4.md §1/§3, F6) — export fiscal a
# la demande (JSON/XML), extrait du module equivalent de l'application source
# (`services/fiscal_export.py`) et etendu : le JET et les mouvements de
# caisse sont inclus (contrairement a l'application source), et
# `generated_at` est EXCLU du corps (le corps servi est donc reproductible —
# meme periode => memes octets => meme `X-Export-SHA256`). La verification
# des chaines AVANT de servir (409 si invalide) est faite par l'appelant
# (`app/api/admin/router.py`), pas ici — ce service reste une pure lecture.
from __future__ import annotations

import json
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, tostring

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cash_movement import CashMovement
from app.models.jet import JournalEvent
from app.models.pos import Payment, Transaction, TransactionItem, TransactionType, ZReport
from app.services.invoice_service import invoice_snapshot_dicts


def _fmt(value) -> str:
    return f"{float(value):.2f}"


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


class FiscalExportService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_snapshot(
        self,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        merchant_name: str = "Frip & Co Street",
        merchant_id: str = "",
    ) -> dict:
        """Corps signe/servi de l'export fiscal — AUCUN horodatage de
        generation (F6 : le corps doit etre reproductible d'un appel a
        l'autre sur la meme periode)."""
        tx_query = select(Transaction)
        if period_from is not None:
            tx_query = tx_query.where(Transaction.created_at >= period_from)
        if period_to is not None:
            tx_query = tx_query.where(Transaction.created_at <= period_to)
        tx_query = tx_query.order_by(Transaction.transaction_number.asc())
        transactions = (await self.db.execute(tx_query)).scalars().all()

        z_query = select(ZReport)
        if period_from is not None:
            z_query = z_query.where(ZReport.created_at >= period_from)
        if period_to is not None:
            z_query = z_query.where(ZReport.created_at <= period_to)
        z_query = z_query.order_by(ZReport.report_number.asc())
        z_reports = (await self.db.execute(z_query)).scalars().all()

        mv_query = select(CashMovement)
        if period_from is not None:
            mv_query = mv_query.where(CashMovement.created_at >= period_from)
        if period_to is not None:
            mv_query = mv_query.where(CashMovement.created_at <= period_to)
        mv_query = mv_query.order_by(CashMovement.created_at.asc())
        movements = (await self.db.execute(mv_query)).scalars().all()

        # `export.downloaded` (F7) est volontairement EXCLU du JET embarque :
        # chaque appel a CET endpoint ecrit un evenement de ce type (F7), qui
        # ne serait donc PAS encore visible dans le corps qu'il vient de
        # produire mais LE DEVIENDRAIT au prochain appel — cassant la
        # reproductibilite (F6 : corps identique => sha256 stable). C'est un
        # evenement operationnel sur l'export lui-meme, pas une donnee
        # fiscale/metier ; il reste consultable via `GET /admin/jet` comme
        # tout evenement JET.
        jet_query = select(JournalEvent).where(JournalEvent.event_type != "export.downloaded")
        if period_from is not None:
            jet_query = jet_query.where(JournalEvent.created_at >= period_from)
        if period_to is not None:
            jet_query = jet_query.where(JournalEvent.created_at <= period_to)
        jet_query = jet_query.order_by(JournalEvent.seq.asc())
        jet_events = (await self.db.execute(jet_query)).scalars().all()

        # PR8/J5 — factures et avoirs de la periode. Le service dedie
        # (`services/invoice_service.py`) porte la serialisation : un seul
        # endroit decide de la forme « export » d'une facture, et l'archive
        # de cloture (`services/fiscal_closure.py`, qui construit sa
        # snapshot avec CE service) en herite sans duplication.
        invoices = await invoice_snapshot_dicts(self.db, period_from, period_to)

        sales_ttc = sum(
            (float(t.total_ttc) for t in transactions if t.transaction_type == TransactionType.sale),
            0.0,
        )
        refunds_ttc = sum(
            (float(t.total_ttc) for t in transactions if t.transaction_type == TransactionType.refund),
            0.0,
        )

        return {
            "version": "1.0",
            "format": "fripco-nf525-export",
            "merchant_name": merchant_name,
            "merchant_id": merchant_id,
            "period_from": _iso(period_from),
            "period_to": _iso(period_to),
            "notice_fr": (
                "Export fiscal Frip & Co Street en format ouvert. Les ventes/"
                "annulations et les clôtures Z sont ordonnées par numéro. Les "
                "champs hash_chain, previous_hash et signature_version "
                "permettent de contrôler le chaînage ; le journal des "
                "événements techniques (JET), les mouvements de caisse et "
                "les factures/avoirs professionnels complètent la "
                "traçabilité. Les montants sont exprimés en "
                "euros TTC. Ce document est une auto-attestation (art. 286 "
                "I-3° bis du CGI) et n'est jamais présenté comme « conforme "
                "NF525 »."
            ),
            "totals": {
                "sales_count": sum(
                    1 for t in transactions if t.transaction_type == TransactionType.sale
                ),
                "refunds_count": sum(
                    1 for t in transactions if t.transaction_type == TransactionType.refund
                ),
                "transactions_count": len(transactions),
                "z_reports_count": len(z_reports),
                "invoices_count": sum(1 for i in invoices if i["kind"] == "invoice"),
                "credit_notes_count": sum(1 for i in invoices if i["kind"] == "credit_note"),
                "sales_ttc": _fmt(sales_ttc),
                "refunds_ttc": _fmt(refunds_ttc),
                "net_ttc": _fmt(sales_ttc - refunds_ttc),
            },
            "transactions": [await self._tx_dict(t) for t in transactions],
            "z_reports": [self._z_dict(z) for z in z_reports],
            "cash_movements": [self._movement_dict(m) for m in movements],
            "journal_events": [self._jet_dict(e) for e in jet_events],
            "invoices": invoices,
        }

    async def _tx_dict(self, t: Transaction) -> dict:
        items = (
            await self.db.execute(
                select(TransactionItem)
                .where(TransactionItem.transaction_id == t.id)
                .order_by(TransactionItem.position.asc())
            )
        ).scalars().all()
        payments = (
            await self.db.execute(
                select(Payment)
                .where(Payment.transaction_id == t.id)
                .order_by(Payment.created_at.asc(), Payment.id.asc())
            )
        ).scalars().all()
        return {
            "id": str(t.id),
            "number": t.transaction_number,
            "type": t.transaction_type.value,
            "created_at": _iso(t.created_at),
            "user_id": str(t.user_id) if t.user_id else None,
            "client_id": str(t.client_id) if t.client_id else None,
            "client_uuid": str(t.client_uuid) if t.client_uuid else None,
            "original_transaction_id": (
                str(t.original_transaction_id) if t.original_transaction_id else None
            ),
            "refund_reason": t.refund_reason,
            "discount_type": t.discount_type.value if t.discount_type else None,
            "discount_amount": _fmt(t.discount_amount),
            "tva_rate": _fmt(t.tva_rate),
            "total_ht": _fmt(t.total_ht),
            "total_tva": _fmt(t.total_tva),
            "total_ttc": _fmt(t.total_ttc),
            "signature_version": int(t.fiscal_signature_version or 1),
            "previous_hash": t.previous_hash,
            "hash_chain": t.hash_chain,
            "items": [
                {
                    "label": it.label,
                    "quantity": it.quantity,
                    "unit_price": _fmt(it.unit_price),
                    "discount_amount": _fmt(it.discount_amount),
                    "line_total": _fmt(it.line_total),
                    "tva_rate": _fmt(it.tva_rate),
                    "line_ht": _fmt(it.line_ht),
                    "line_tva": _fmt(it.line_tva),
                }
                for it in items
            ],
            "payments": [
                {
                    "method": p.method.value,
                    "amount": _fmt(p.amount),
                    "tendered_amount": _fmt(p.tendered_amount) if p.tendered_amount is not None else None,
                    "sumup_checkout_id": p.sumup_checkout_id,
                    "sumup_transaction_id": p.sumup_transaction_id,
                    "sumup_transaction_code": p.sumup_transaction_code,
                    "sumup_auth_code": p.sumup_auth_code,
                    "sumup_card_brand": p.sumup_card_brand,
                    "sumup_card_last4": p.sumup_card_last4,
                }
                for p in payments
            ],
        }

    def _z_dict(self, z: ZReport) -> dict:
        return {
            "number": z.report_number,
            "created_at": _iso(z.created_at),
            "cash_drawer_id": str(z.cash_drawer_id),
            "total_sales": _fmt(z.total_sales),
            "total_refunds": _fmt(z.total_refunds),
            "total_net": _fmt(z.total_net),
            "total_ht": _fmt(z.total_ht),
            "total_tva": _fmt(z.total_tva),
            "transaction_count": z.transaction_count,
            "first_transaction_number": z.first_transaction_number,
            "last_transaction_number": z.last_transaction_number,
            "last_transaction_hash": z.last_transaction_hash,
            "payment_totals": z.payment_totals or {},
            "cumulative_sales": _fmt(z.cumulative_sales),
            "cumulative_refunds": _fmt(z.cumulative_refunds),
            "cumulative_net": _fmt(z.cumulative_net),
            "cumulative_transaction_count": z.cumulative_transaction_count,
            "signature_version": int(z.fiscal_signature_version or 1),
            "hash": z.hash,
            "previous_hash": z.previous_hash,
        }

    def _movement_dict(self, m: CashMovement) -> dict:
        return {
            "id": str(m.id),
            "drawer_id": str(m.drawer_id),
            "direction": m.direction.value,
            "amount": _fmt(m.amount),
            "reason": m.reason.value,
            "note": m.note,
            "created_at": _iso(m.created_at),
        }

    def _jet_dict(self, e: JournalEvent) -> dict:
        return {
            "seq": e.seq,
            "event_type": e.event_type,
            "created_at": _iso(e.created_at),
            "payload": e.payload or {},
            "previous_hash": e.previous_hash,
            "hash": e.hash,
            "signature_version": e.signature_version,
        }

    # ------------------------------------------------------------------
    # Encodeurs — deterministes (memes octets pour le meme snapshot).
    # ------------------------------------------------------------------

    def to_json(self, snapshot: dict) -> str:
        return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True)

    def to_xml(self, snapshot: dict) -> str:
        root = Element(
            "FiscalExport",
            attrib={
                "version": snapshot["version"],
                "format": snapshot["format"],
                "merchant_name": snapshot["merchant_name"],
                "merchant_id": snapshot["merchant_id"],
                "period_from": snapshot["period_from"],
                "period_to": snapshot["period_to"],
            },
        )

        totals = snapshot["totals"]
        SubElement(root, "Totals", {k: str(v) for k, v in totals.items()})

        tx_root = SubElement(root, "Transactions", {"count": str(len(snapshot["transactions"]))})
        for tx in snapshot["transactions"]:
            tx_el = SubElement(
                tx_root,
                "Transaction",
                {
                    "number": str(tx["number"]),
                    "id": tx["id"],
                    "type": tx["type"],
                    "created_at": tx["created_at"],
                    "total_ht": tx["total_ht"],
                    "total_tva": tx["total_tva"],
                    "total_ttc": tx["total_ttc"],
                    "hash_chain": tx["hash_chain"] or "",
                    "previous_hash": tx["previous_hash"] or "",
                    "signature_version": str(tx["signature_version"]),
                },
            )
            if tx["user_id"]:
                tx_el.set("user_id", tx["user_id"])
            if tx["client_id"]:
                tx_el.set("client_id", tx["client_id"])
            if tx["client_uuid"]:
                tx_el.set("client_uuid", tx["client_uuid"])
            if tx["original_transaction_id"]:
                tx_el.set("original_transaction_id", tx["original_transaction_id"])
            if tx["refund_reason"]:
                tx_el.set("refund_reason", tx["refund_reason"])

            items_el = SubElement(tx_el, "Items", {"count": str(len(tx["items"]))})
            for it in tx["items"]:
                SubElement(
                    items_el,
                    "Item",
                    {
                        "label": it["label"] or "",
                        "quantity": str(it["quantity"]),
                        "unit_price": it["unit_price"],
                        "line_total": it["line_total"],
                        "tva_rate": it["tva_rate"],
                    },
                )

            pays_el = SubElement(tx_el, "Payments", {"count": str(len(tx["payments"]))})
            for p in tx["payments"]:
                SubElement(
                    pays_el,
                    "Payment",
                    {
                        "method": p["method"],
                        "amount": p["amount"],
                        "tendered_amount": p["tendered_amount"] or "",
                        "sumup_checkout_id": p["sumup_checkout_id"] or "",
                        "sumup_transaction_id": p["sumup_transaction_id"] or "",
                    },
                )

        z_root = SubElement(root, "ZReports", {"count": str(len(snapshot["z_reports"]))})
        for z in snapshot["z_reports"]:
            z_el = SubElement(
                z_root,
                "ZReport",
                {
                    "number": str(z["number"]),
                    "created_at": z["created_at"],
                    "cash_drawer_id": z["cash_drawer_id"],
                    "total_sales": z["total_sales"],
                    "total_refunds": z["total_refunds"],
                    "total_net": z["total_net"],
                    "transaction_count": str(z["transaction_count"]),
                    "hash": z["hash"],
                    "previous_hash": z["previous_hash"] or "",
                },
            )
            for method, totals_ in (z["payment_totals"] or {}).items():
                SubElement(
                    z_el,
                    "PaymentMethod",
                    {
                        "method": str(method),
                        "sales": str(totals_.get("sales", "0.00")),
                        "refunds": str(totals_.get("refunds", "0.00")),
                        "net": str(totals_.get("net", "0.00")),
                    },
                )

        mv_root = SubElement(
            root, "CashMovements", {"count": str(len(snapshot["cash_movements"]))}
        )
        for m in snapshot["cash_movements"]:
            SubElement(
                mv_root,
                "CashMovement",
                {
                    "id": m["id"],
                    "direction": m["direction"],
                    "amount": m["amount"],
                    "reason": m["reason"],
                    "created_at": m["created_at"],
                },
            )

        inv_root = SubElement(root, "Invoices", {"count": str(len(snapshot["invoices"]))})
        for inv in snapshot["invoices"]:
            SubElement(
                inv_root,
                "Invoice",
                {
                    "kind": inv["kind"],
                    "invoice_number": inv["invoice_number"],
                    "original_invoice_number": inv["original_invoice_number"] or "",
                    "transaction_number": str(inv["transaction_number"] or ""),
                    "issued_at": inv["issued_at"],
                    "company_name": inv["company_name"],
                    "siret": inv["siret"],
                    "vat_number": inv["vat_number"] or "",
                    "total_ht": inv["total_ht"],
                    "total_tva": inv["total_tva"],
                    "total_ttc": inv["total_ttc"],
                },
            )

        jet_root = SubElement(
            root, "JournalEvents", {"count": str(len(snapshot["journal_events"]))}
        )
        for e in snapshot["journal_events"]:
            SubElement(
                jet_root,
                "JournalEvent",
                {
                    "seq": str(e["seq"]),
                    "event_type": e["event_type"],
                    "created_at": e["created_at"],
                    "hash": e["hash"],
                    "previous_hash": e["previous_hash"],
                },
            )

        xml_bytes = tostring(root, encoding="utf-8", xml_declaration=True)
        return xml_bytes.decode("utf-8")
