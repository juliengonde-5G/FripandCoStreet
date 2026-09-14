# Extrait de Vintiz (apps/api/app/services/receipt.py), reduit — pas de
# fidelite/facture B2B (hors perimetre PR2, §4.4 du contrat). Mention
# legale conforme a D14 (jamais « conforme NF525 »).
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.models.pos import Transaction, TransactionType
from app.version import FISCAL_SIGNATURE_VERSION

_PARIS = ZoneInfo("Europe/Paris")
_WIDTH = 42


class ReceiptService:
    """Genere le texte formate d'un ticket (80 mm, 42 colonnes)."""

    def generate(
        self, transaction: Transaction, *, shop: dict[str, Any], original_number: int | None = None
    ) -> str:
        if transaction.transaction_type == TransactionType.refund:
            return self.generate_refund_text(transaction, shop=shop, original_number=original_number)
        return self.generate_sale_text(transaction, shop=shop)

    def _header(self, shop: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        name = (shop.get("name") or "Frip & Co Street").upper()
        lines.append(name.center(_WIDTH))
        addr_parts = [
            shop.get("address_line1", ""),
            f"{shop.get('postal_code', '')} {shop.get('city', '')}".strip(),
        ]
        address = ", ".join(p for p in addr_parts if p)
        if address:
            lines.append(address.center(_WIDTH))
        if shop.get("siret"):
            lines.append(f"SIRET {shop['siret']}".center(_WIDTH))
        lines.append("=" * _WIDTH)
        return lines

    @staticmethod
    def _local_dt(transaction: Transaction) -> datetime:
        dt = transaction.created_at or datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_PARIS)

    def generate_sale_text(self, transaction: Transaction, *, shop: dict[str, Any]) -> str:
        lines = self._header(shop)
        dt = self._local_dt(transaction)
        lines.append(f"Ticket #{transaction.transaction_number}")
        lines.append(f"Date: {dt.strftime('%d/%m/%Y %H:%M')}")
        lines.append("-" * _WIDTH)

        for item in sorted(transaction.items or [], key=lambda i: i.position):
            name = item.label if len(item.label) <= 28 else item.label[:27] + "…"
            lines.append(f"{name:<28}{'':>14}")
            qty_price = f"  {item.quantity} x {float(item.unit_price):.2f}"
            total = f"{float(item.line_total):.2f} EUR"
            lines.append(f"{qty_price:<28}{total:>14}")
            if float(item.discount_amount or 0) > 0:
                lines.append(f"{'  remise ligne':<28}{-float(item.discount_amount):>13.2f} EUR")

        lines.append("-" * _WIDTH)

        if transaction.discount_type is not None and float(transaction.discount_amount or 0) > 0:
            label = (
                f"Remise {float(transaction.discount_value):.0f}%"
                if transaction.discount_type.value == "percent"
                else "Remise"
            )
            lines.append(f"{label:<28}{-float(transaction.discount_amount):>13.2f} EUR")
            lines.append("-" * _WIDTH)

        rate = float(transaction.tva_rate)
        lines.append(f"{'Total HT:':<28}{float(transaction.total_ht):>13.2f} EUR")
        lines.append(f"{f'TVA {rate:g}%:':<28}{float(transaction.total_tva):>13.2f} EUR")
        lines.append(f"{'Total TTC:':<28}{float(transaction.total_ttc):>13.2f} EUR")
        lines.append("-" * _WIDTH)

        for payment in transaction.payments or []:
            method_label = payment.method.value.upper()
            lines.append(f"{method_label:<28}{float(payment.amount):>13.2f} EUR")
            if payment.method.value == "cash" and payment.tendered_amount is not None:
                lines.append(f"{'  remis':<28}{float(payment.tendered_amount):>13.2f} EUR")
                if payment.change_amount:
                    lines.append(f"{'  rendu':<28}{float(payment.change_amount):>13.2f} EUR")
            elif payment.method.value == "card":
                if payment.sumup_card_brand or payment.sumup_card_last4:
                    brand = payment.sumup_card_brand or "CB"
                    last4 = payment.sumup_card_last4 or "----"
                    lines.append(f"  {brand} •••• {last4}")
                if payment.sumup_transaction_code:
                    lines.append(f"  Réf. SumUp {payment.sumup_transaction_code}")

        lines.append("=" * _WIDTH)
        hash_display = (transaction.hash_chain or "")[:16]
        lines.append(f"Hash: {hash_display}")
        lines.append(
            (
                "Logiciel de caisse Frip & Co Street — auto-attestation "
                f"art. 286 I-3° bis CGI, version fiscale {FISCAL_SIGNATURE_VERSION}"
            )
        )
        lines.append("")

        footer = (shop.get("footer_note") or "").strip() if isinstance(shop, dict) else ""
        if footer:
            for line in footer.splitlines():
                stripped = line.strip()
                lines.append(stripped[:_WIDTH].center(_WIDTH) if stripped else "")
        else:
            lines.append("Merci de votre visite !".center(_WIDTH))
        lines.append("")
        return "\n".join(lines)

    def generate_refund_text(
        self,
        transaction: Transaction,
        *,
        shop: dict[str, Any],
        original_number: int | None = None,
    ) -> str:
        lines = self._header(shop)
        lines.append("** TICKET D'ANNULATION **".center(_WIDTH))
        lines.append("=" * _WIDTH)

        dt = self._local_dt(transaction)
        lines.append(f"Annulation #{transaction.transaction_number}")
        lines.append(f"Date: {dt.strftime('%d/%m/%Y %H:%M')}")
        if original_number is not None:
            lines.append(f"Annule le ticket n° {original_number}")
        if transaction.refund_reason:
            reason = transaction.refund_reason
            if len(reason) > _WIDTH - 8:
                reason = reason[: _WIDTH - 9] + "…"
            lines.append(f"Motif: {reason}")
        lines.append("-" * _WIDTH)

        for item in sorted(transaction.items or [], key=lambda i: i.position):
            name = item.label if len(item.label) <= 28 else item.label[:27] + "…"
            lines.append(f"{name:<28}{'':>14}")
            qty_price = f"  {item.quantity} x {float(item.unit_price):.2f}"
            total = f"-{float(item.line_total):.2f} EUR"
            lines.append(f"{qty_price:<28}{total:>14}")

        lines.append("-" * _WIDTH)
        rate = float(transaction.tva_rate)
        lines.append(f"{'Total HT:':<28}{-float(transaction.total_ht):>13.2f} EUR")
        lines.append(f"{f'TVA {rate:g}%:':<28}{-float(transaction.total_tva):>13.2f} EUR")
        lines.append(f"{'TOTAL REMBOURSE:':<28}{-float(transaction.total_ttc):>13.2f} EUR")
        lines.append("-" * _WIDTH)

        for payment in transaction.payments or []:
            method_label = payment.method.value.upper() + " (rendu)"
            lines.append(f"{method_label:<28}{float(payment.amount):>13.2f} EUR")

        lines.append("=" * _WIDTH)
        hash_display = (transaction.hash_chain or "")[:16]
        lines.append(f"Hash: {hash_display}")
        lines.append("")
        lines.append("Conservez ce ticket".center(_WIDTH))
        lines.append("")
        return "\n".join(lines)
