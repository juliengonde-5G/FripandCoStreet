# Nouveau test (PR3, §7 ARCHITECTURE_PR3.md) — construction de l'e-mail du
# ticket : sujet, ticket en texte integre, AUCUN lien de tracking ni image
# externe, paragraphe RGPD avec `dpo_email`.
from __future__ import annotations

from datetime import datetime, timezone

from app.models.pos import Transaction, TransactionType
from app.services.receipt_email import build_receipt_email

_SHOP = {
    "name": "Frip & Co Street",
    "address_line1": "12 rue de la Gare",
    "postal_code": "76000",
    "city": "Rouen",
    "siret": "12345678901234",
    "vat_number": "FR12345678901",
    "footer_note": "A bientôt !",
}

_RECEIPT_TEXT = "FRIP & CO STREET\nTicket #42\nTotal TTC: 25.00 EUR"


def _tx() -> Transaction:
    tx = Transaction(
        transaction_number=42,
        transaction_type=TransactionType.sale,
        tva_rate=20.0,
        hash_chain="deadbeef",
        previous_hash="0",
    )
    tx.created_at = datetime.now(timezone.utc)
    return tx


def test_subject_contains_transaction_number():
    message = build_receipt_email(
        _tx(), to="cliente@example.com", receipt_text=_RECEIPT_TEXT, shop=_SHOP, dpo_email=None
    )
    assert message.subject == "Votre ticket Frip & Co Street n° 42"
    assert message.to == "cliente@example.com"


def test_html_contains_the_receipt_text():
    message = build_receipt_email(
        _tx(), to="cliente@example.com", receipt_text=_RECEIPT_TEXT, shop=_SHOP, dpo_email=None
    )
    assert "Ticket #42" in message.html
    assert "Total TTC: 25.00 EUR" in message.html
    assert message.text == _RECEIPT_TEXT


def test_html_contains_shop_legal_mentions():
    message = build_receipt_email(
        _tx(), to="cliente@example.com", receipt_text=_RECEIPT_TEXT, shop=_SHOP, dpo_email=None
    )
    assert "SIRET 12345678901234" in message.html
    assert "FR12345678901" in message.html
    assert "Rouen" in message.html


def test_html_never_contains_tracking_link_or_external_image():
    message = build_receipt_email(
        _tx(),
        to="cliente@example.com",
        receipt_text=_RECEIPT_TEXT,
        shop=_SHOP,
        dpo_email="dpo@fripco-street.fr",
    )
    assert "<a " not in message.html
    assert "href=" not in message.html
    assert "<img" not in message.html
    assert "http://" not in message.html
    assert "https://" not in message.html


def test_rgpd_paragraph_included_when_dpo_email_set():
    message = build_receipt_email(
        _tx(),
        to="cliente@example.com",
        receipt_text=_RECEIPT_TEXT,
        shop=_SHOP,
        dpo_email="dpo@fripco-street.fr",
    )
    assert "dpo@fripco-street.fr" in message.html


def test_rgpd_paragraph_omitted_when_dpo_email_missing():
    message = build_receipt_email(
        _tx(), to="cliente@example.com", receipt_text=_RECEIPT_TEXT, shop=_SHOP, dpo_email=None
    )
    assert "données personnelles" not in message.html


def test_html_content_is_escaped_against_injection():
    malicious_shop = dict(_SHOP, name="<script>alert(1)</script>")
    message = build_receipt_email(
        _tx(), to="cliente@example.com", receipt_text=_RECEIPT_TEXT, shop=malicious_shop, dpo_email=None
    )
    assert "<script>" not in message.html
    assert "&lt;script&gt;" in message.html
