# Extrait de l'application source (apps/api/app/services/receipt.py), reduit — pas de
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


# Prefixe EXACT de la ligne client du ticket — partage entre l'ecriture
# (`generate_sale_text`) et la reecriture a la lecture
# (`apply_client_line`), pour qu'aucune des deux ne puisse deriver.
CLIENT_LINE_PREFIX = "Client : "
# PR8 (J2) — ligne de la vendeuse, ecrite UNE FOIS a l'emission, juste sous
# la ligne Date. Contrairement a la ligne « Client : », elle n'est jamais
# reecrite a la lecture : le ticket doit porter le nom de celle qui a
# encaisse, meme si la releve a eu lieu depuis.
CASHIER_LINE_PREFIX = "Vendeuse : "
_DATE_LINE_PREFIX = "Date: "


def apply_client_line(content: str, client_label: str | None) -> str:
    """Remet la ligne « Client : … » d'un ticket DEJA FIGE en accord avec
    la cliente rattachee AUJOURD'HUI a la vente.

    Pourquoi reecrire a la lecture plutot que regenerer et re-stocker :
    `receipts.content` est IMMUABLE en base — le trigger
    `trg_protect_receipt` (migration 0002, fonction reecrite en 0004)
    refuse tout UPDATE de `content` et tout DELETE, seuls les compteurs
    `duplicate_count`/`printed_count`/`printed_at` bougent. La ligne
    stockee reste donc la trace exacte du ticket tel qu'il a ete emis
    (c'est elle, et elle seule, qui part dans l'archive fiscale scellee,
    cf. `services/fiscal_closure.py`), et c'est le RENDU — relecture,
    renvoi par e-mail, reimpression ESC/POS — qui suit le rattachement
    courant. Rien d'autre du ticket n'est recalcule : un changement
    d'adresse ou de mention de pied ne doit pas modifier retroactivement
    un ticket deja remis.

    Le rattachement d'une cliente est par ailleurs la seule mutation
    autorisee hors signature sur une vente signee (E3/PR3) : la ligne
    client n'entre dans aucun hash, la reecrire ici ne touche donc a
    aucune preuve.
    """
    lines = [
        line for line in content.split("\n") if not line.startswith(CLIENT_LINE_PREFIX)
    ]
    if client_label:
        for index, line in enumerate(lines):
            if line.startswith(_DATE_LINE_PREFIX):
                # La ligne « Vendeuse : … » (PR8/J2), quand elle existe,
                # suit immediatement la date et reste au-dessus du client :
                # on insere donc APRES elle, sinon une simple relecture du
                # ticket en inverserait l'ordre.
                position = index + 1
                if position < len(lines) and lines[position].startswith(CASHIER_LINE_PREFIX):
                    position += 1
                lines.insert(position, f"{CLIENT_LINE_PREFIX}{client_label}")
                break
        # Pas de ligne « Date: » (format inattendu) : on n'invente pas un
        # emplacement, le ticket est rendu tel quel.
    return "\n".join(lines)


# Prefixes EXACTS des lignes « document » du ticket (PR8/J5), sur le meme
# modele que `CLIENT_LINE_PREFIX` : une seule definition partagee entre
# l'ecriture et la reecriture a la lecture.
INVOICE_LINE_PREFIX = "Facture : "
CREDIT_NOTE_LINE_PREFIX = "Avoir : "


def apply_invoice_line(
    content: str, invoice_number: str | None, *, credit_note: bool = False
) -> str:
    """Fonction soeur d'`apply_client_line` (PR8/J5) : pose la ligne
    « Facture : F-2026-0001 » (ou « Avoir : A-2026-0001 ») sur un ticket
    DEJA FIGE, au moment du RENDU.

    Meme mecanique, et pour la meme raison : `receipts.content` est
    immuable en base (trigger `trg_protect_receipt`), or la facture est
    emise APRES la vente — le ticket stocke ne peut donc pas la porter. Le
    numero de facture n'entre dans aucun hash : l'ajouter au rendu ne
    touche a aucune preuve.

    La ligne se place sous la derniere des lignes d'en-tete presentes —
    « Client : … », a defaut « Vendeuse : … » (PR8/J2), a defaut « Date: »
    — de sorte que l'ordre rendu est toujours le meme, quel que soit
    l'ordre dans lequel ces fonctions de rendu sont appliquees.
    """
    prefix = CREDIT_NOTE_LINE_PREFIX if credit_note else INVOICE_LINE_PREFIX
    lines = [
        line
        for line in content.split("\n")
        if not line.startswith(INVOICE_LINE_PREFIX)
        and not line.startswith(CREDIT_NOTE_LINE_PREFIX)
    ]
    if invoice_number:
        anchors = (CLIENT_LINE_PREFIX, CASHIER_LINE_PREFIX, _DATE_LINE_PREFIX)
        for anchor_prefix in anchors:
            for index, line in enumerate(lines):
                if line.startswith(anchor_prefix):
                    lines.insert(index + 1, f"{prefix}{invoice_number}")
                    return "\n".join(lines)
        # Format inattendu (ni ligne client, ni ligne date) : on n'invente
        # pas un emplacement, le ticket est rendu tel quel.
    return "\n".join(lines)


def format_client_label(first_name: str | None, last_name: str | None) -> str | None:
    """« Prenom N. » — le seul identifiant client imprime sur un ticket (PR7,
    I3).

    Le ticket est un document remis en main propre, potentiellement oublie
    sur le comptoir : il ne porte JAMAIS l'e-mail ni le telephone de la
    cliente, seulement son prenom et l'initiale de son nom. Sans prenom,
    rien n'est imprime (``None``) — une ligne « Client : » vide n'aiderait
    personne.
    """
    first = (first_name or "").strip()
    if not first:
        return None
    last = (last_name or "").strip()
    label = f"{first} {last[0].upper()}." if last else first
    return label[:_WIDTH - 9]


class ReceiptService:
    """Genere le texte formate d'un ticket (80 mm, 42 colonnes)."""

    def generate(
        self,
        transaction: Transaction,
        *,
        shop: dict[str, Any],
        original_number: int | None = None,
        client_label: str | None = None,
        cashier_label: str | None = None,
    ) -> str:
        if transaction.transaction_type == TransactionType.refund:
            return self.generate_refund_text(
                transaction,
                shop=shop,
                original_number=original_number,
                cashier_label=cashier_label,
            )
        return self.generate_sale_text(
            transaction, shop=shop, client_label=client_label, cashier_label=cashier_label
        )

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
        if shop.get("vat_number"):
            lines.append(f"N° TVA : {shop['vat_number']}".center(_WIDTH))
        lines.append("=" * _WIDTH)
        return lines

    @staticmethod
    def _local_dt(transaction: Transaction) -> datetime:
        dt = transaction.created_at or datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_PARIS)

    def generate_sale_text(
        self,
        transaction: Transaction,
        *,
        shop: dict[str, Any],
        client_label: str | None = None,
        cashier_label: str | None = None,
    ) -> str:
        lines = self._header(shop)
        dt = self._local_dt(transaction)
        lines.append(f"Ticket #{transaction.transaction_number}")
        lines.append(f"{_DATE_LINE_PREFIX}{dt.strftime('%d/%m/%Y %H:%M')}")
        # PR8/J2 — vendeuse qui a encaisse, sous la ligne Date.
        if cashier_label:
            lines.append(f"{CASHIER_LINE_PREFIX}{cashier_label[:_WIDTH - len(CASHIER_LINE_PREFIX)]}")
        # PR7/I3 — « Prenom N. » sous l'en-tete quand une cliente est
        # rattachee a la vente. Jamais son e-mail ni son telephone
        # (`format_client_label`).
        if client_label:
            lines.append(f"{CLIENT_LINE_PREFIX}{client_label}")
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
        cashier_label: str | None = None,
    ) -> str:
        lines = self._header(shop)
        lines.append("** TICKET D'ANNULATION **".center(_WIDTH))
        lines.append("=" * _WIDTH)

        dt = self._local_dt(transaction)
        lines.append(f"Annulation #{transaction.transaction_number}")
        lines.append(f"{_DATE_LINE_PREFIX}{dt.strftime('%d/%m/%Y %H:%M')}")
        # PR8/J2 — la vendeuse qui a passe l'annulation, au meme endroit que
        # sur un ticket de vente.
        if cashier_label:
            lines.append(f"{CASHIER_LINE_PREFIX}{cashier_label[:_WIDTH - len(CASHIER_LINE_PREFIX)]}")
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
