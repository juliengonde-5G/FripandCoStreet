# Nouveau service (PR3, docs/ARCHITECTURE_PR3.md §3) — construit l'e-mail du
# ticket de caisse. Sujet « Votre ticket Frip & Co Street n° X », HTML sobre
# (en-tete boutique + ticket en <pre> + mentions legales + paragraphe RGPD
# court avec `dpo_email`), texte brut = ticket (`Receipt.content`, deja
# genere par `ReceiptService`). AUCUN lien de tracking, AUCUNE image
# externe — contrainte non negociable sur un envoi transactionnel.
from __future__ import annotations

from html import escape
from typing import Any

from app.models.pos import Transaction
from app.services.email_gateway import EmailMessage


def build_receipt_email(
    transaction: Transaction,
    *,
    to: str,
    receipt_text: str,
    shop: dict[str, Any],
    dpo_email: str | None,
) -> EmailMessage:
    subject = f"Votre ticket Frip & Co Street n° {transaction.transaction_number}"

    shop_name = escape(shop.get("name") or "Frip & Co Street")
    address_parts = [
        shop.get("address_line1") or "",
        f"{shop.get('postal_code') or ''} {shop.get('city') or ''}".strip(),
    ]
    address = ", ".join(escape(p) for p in address_parts if p)

    legal_bits: list[str] = []
    if shop.get("siret"):
        legal_bits.append(f"SIRET {escape(shop['siret'])}")
    if shop.get("vat_number"):
        legal_bits.append(f"N° TVA {escape(shop['vat_number'])}")
    legal_line = " — ".join(legal_bits)

    footer_note = escape((shop.get("footer_note") or "").strip())

    rgpd_paragraph = ""
    if dpo_email:
        rgpd_paragraph = (
            '<p style="font-size:12px;color:#666;margin-top:24px">'
            "Cet e-mail vous a été envoyé pour vous transmettre votre ticket "
            "de caisse. Pour toute question relative à vos données "
            f"personnelles, contactez {escape(dpo_email)}."
            "</p>"
        )

    html = (
        '<div style="font-family:sans-serif;max-width:480px;margin:0 auto;color:#111">'
        f'<h2 style="margin:0 0 4px">{shop_name}</h2>'
        + (f'<p style="margin:0 0 2px;color:#444">{address}</p>' if address else "")
        + (f'<p style="margin:0 0 16px;color:#444;font-size:12px">{legal_line}</p>' if legal_line else "")
        + '<pre style="white-space:pre-wrap;font-family:monospace;font-size:13px;'
        'border:1px solid #ddd;border-radius:4px;padding:12px;background:#fafafa">'
        f"{escape(receipt_text)}</pre>"
        + (f'<p style="margin-top:16px;color:#444">{footer_note}</p>' if footer_note else "")
        + rgpd_paragraph
        + "</div>"
    )

    return EmailMessage(to=to, subject=subject, html=html, text=receipt_text)
