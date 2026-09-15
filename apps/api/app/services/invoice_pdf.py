# Nouveau service (PR8, docs/ARCHITECTURE_PR8.md, contrat J5) — PDF de la
# facture B2B et de l'avoir (reportlab), construit sur le meme moule que
# `services/z_report_pdf.py` : canvas `invariant=1` (reportlab fige alors
# `CreationDate`/`ModDate` et l'identifiant de fichier), `pageCompression=0`
# (flux de contenu en clair, verifiable par simple recherche d'octets) et
# AUCUN horodatage de generation dans le document — seuls des horodatages
# figes y figurent (date d'emission de la facture, date du ticket).
#
# Consequence directe, et c'est la raison d'etre de ce determinisme : deux
# rendus de la meme facture produisent des octets identiques, donc le meme
# SHA-256. L'empreinte scellee au premier telechargement
# (`invoices.pdf_sha256`, pose une fois — trigger de la migration 0008) est
# donc verifiable a chaque telechargement ulterieur : si elle ne correspond
# plus, on refuse de servir le document (`pdf_mismatch`) au lieu de laisser
# circuler deux versions d'une meme facture.
#
# Le rendu est une fonction PURE : il ne lit ni la base ni les reglages
# boutique. Les coordonnees de l'emetteur viennent du bloc
# `invoice.seller_snapshot`, fige a l'emission — sinon un simple changement
# de nom commercial ou d'adresse dans les reglages changerait le document
# rendu, et l'empreinte scellee au premier telechargement ne correspondrait
# plus (500 `pdf_mismatch` sur une facture pourtant intacte). Une facture
# doit rester reproductible a vie.
#
# Mentions legales portees par le document (facture entre professionnels) :
# penalites de retard au taux d'interet legal, indemnite forfaitaire de
# recouvrement de 40 €, exigibilite de la TVA a la livraison. Jamais la
# mention « conforme NF525 » (CLAUDE.md) : la caisse s'auto-atteste.
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

from app.models.invoice import Invoice, InvoiceKind
from app.models.pos import Transaction, TransactionItem
from app.version import FISCAL_SIGNATURE_VERSION, FISCAL_VERSION_DATE

_PARIS = ZoneInfo("Europe/Paris")


def _eur(amount) -> str:
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    return f"{value:,.2f} €".replace(",", " ").replace(".", ",")


def _signed_eur(amount, *, negative: bool) -> str:
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    if negative and value != 0:
        return "-" + _eur(abs(value))
    return _eur(value)


def _dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_PARIS).strftime("%d/%m/%Y")


def _escape(value: str | None) -> str:
    """Echappe le balisage des `Paragraph` reportlab (mini-HTML) : une
    raison sociale peut legitimement contenir « & » ou « < »."""
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_invoice_pdf(
    invoice: Invoice,
    transaction: Transaction,
    items: Sequence[TransactionItem],
    *,
    original_invoice_number: str | None = None,
) -> bytes:
    """Rend la facture (ou l'avoir) en PDF A4 — octets deterministes.

    Fonction PURE : tout ce qui est imprime vient des objets recus (la
    facture et son bloc vendeur fige, la vente signee et ses lignes). Rien
    n'est relu en base au moment du rendu, donc rien de ce qui evolue
    ailleurs dans l'application ne peut changer un document deja emis.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    # Bloc vendeur fige a l'emission (jamais les reglages courants).
    seller = invoice.seller_snapshot if isinstance(invoice.seller_snapshot, dict) else {}

    is_credit_note = invoice.kind == InvoiceKind.credit_note
    original_number = original_invoice_number

    def _invariant_canvas(*args, **kwargs):
        kwargs["invariant"] = 1
        kwargs["pageCompression"] = 0
        return Canvas(*args, **kwargs)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=14 * mm,
        title=f"{'Avoir' if is_credit_note else 'Facture'} {invoice.invoice_number}",
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("InvH1", parent=styles["Title"], fontSize=18, leading=22, spaceAfter=4)
    h2 = ParagraphStyle(
        "InvH2", parent=styles["Heading2"], fontSize=11, leading=14,
        textColor=colors.HexColor("#0B7A6A"), spaceBefore=6, spaceAfter=2,
    )
    body = ParagraphStyle("InvBody", parent=styles["Normal"], fontSize=9, leading=12)
    body_right = ParagraphStyle("InvBodyRight", parent=body, alignment=2)
    body_small = ParagraphStyle("InvBodySmall", parent=styles["Normal"], fontSize=8, leading=10)

    story: list = []

    # ------------------------------------------------------------------
    # En-tete boutique (emetteur) + bloc client (destinataire)
    # ------------------------------------------------------------------
    title = "AVOIR" if is_credit_note else "FACTURE"
    story.append(Paragraph(f"<b>{title} {_escape(invoice.invoice_number)}</b>", h1))

    seller_lines = [f"<b>{_escape(seller.get('name') or 'Frip & Co Street')}</b>"]
    for key in ("address_line1", "address_line2"):
        if seller.get(key):
            seller_lines.append(_escape(seller[key]))
    city_line = f"{seller.get('postal_code', '')} {seller.get('city', '')}".strip()
    if city_line:
        seller_lines.append(_escape(city_line))
    if seller.get("siret"):
        seller_lines.append(f"SIRET {_escape(seller['siret'])}")
    if seller.get("vat_number"):
        seller_lines.append(f"N° TVA : {_escape(seller['vat_number'])}")
    if seller.get("phone"):
        seller_lines.append(f"Tél. {_escape(seller['phone'])}")
    if seller.get("email"):
        seller_lines.append(_escape(seller["email"]))

    buyer_lines = [
        "<b>Client</b>",
        _escape(invoice.company_name),
        _escape(invoice.address_line1),
    ]
    if invoice.address_line2:
        buyer_lines.append(_escape(invoice.address_line2))
    buyer_lines.append(_escape(f"{invoice.postal_code} {invoice.city}".strip()))
    buyer_lines.append(f"SIRET {_escape(invoice.siret)}")
    if invoice.vat_number:
        buyer_lines.append(f"N° TVA : {_escape(invoice.vat_number)}")

    parties = Table(
        [
            [
                Paragraph("<br/>".join(seller_lines), body),
                Paragraph("<br/>".join(buyer_lines), body),
            ]
        ],
        colWidths=[87 * mm, 87 * mm],
        hAlign="LEFT",
    )
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(parties)
    story.append(Spacer(1, 4 * mm))

    meta_lines = [
        f"{'Avoir' if is_credit_note else 'Facture'} n° <b>{_escape(invoice.invoice_number)}</b>",
        f"Date : {_dt(invoice.issued_at)}",
        f"Ticket n° {transaction.transaction_number} du {_dt(transaction.created_at)}",
    ]
    if is_credit_note and original_number:
        meta_lines.append(f"<b>Avoir sur facture {_escape(original_number)}</b>")
    story.append(Paragraph("<br/>".join(meta_lines), body))
    story.append(Spacer(1, 4 * mm))

    # ------------------------------------------------------------------
    # Lignes
    # ------------------------------------------------------------------
    story.append(Paragraph("Détail", h2))
    rows = [
        [
            Paragraph("<b>Désignation</b>", body),
            Paragraph("<b>Qté</b>", body_right),
            Paragraph("<b>P.U. HT</b>", body_right),
            Paragraph("<b>TVA</b>", body_right),
            Paragraph("<b>Total TTC</b>", body_right),
        ]
    ]
    for item in items:
        quantity = int(item.quantity) or 1
        # P.U. HT reconstitue depuis le HT de la LIGNE (remise ventilee
        # incluse) : c'est ce montant-la, et lui seul, qui s'additionne
        # jusqu'au total HT du pied de facture.
        unit_ht = (Decimal(str(item.line_ht)) / Decimal(quantity)).quantize(Decimal("0.01"))
        label = _escape(item.label)
        if is_credit_note:
            label = f"Avoir — {label}"
        rows.append(
            [
                Paragraph(label, body),
                Paragraph(str(quantity), body_right),
                Paragraph(_signed_eur(unit_ht, negative=is_credit_note), body_right),
                Paragraph(f"{float(item.tva_rate):g} %", body_right),
                Paragraph(
                    _signed_eur(item.line_total, negative=is_credit_note), body_right
                ),
            ]
        )
    items_table = Table(rows, colWidths=[78 * mm, 15 * mm, 27 * mm, 20 * mm, 34 * mm])
    items_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ECEAE3")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.HexColor("#0E0E0C")),
                ("LINEBELOW", (0, 1), (-1, -1), 0.2, colors.HexColor("#D5D3CC")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(items_table)
    story.append(Spacer(1, 4 * mm))

    # ------------------------------------------------------------------
    # Totaux
    # ------------------------------------------------------------------
    total_rows = [
        ["Total HT", _signed_eur(transaction.total_ht, negative=is_credit_note)],
        [
            f"TVA {float(transaction.tva_rate):g} %",
            _signed_eur(transaction.total_tva, negative=is_credit_note),
        ],
        [
            "Total TTC" if not is_credit_note else "Total avoir TTC",
            _signed_eur(transaction.total_ttc, negative=is_credit_note),
        ],
    ]
    totals_table = Table(total_rows, colWidths=[50 * mm, 40 * mm], hAlign="RIGHT")
    totals_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.2, colors.HexColor("#D5D3CC")),
                ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.HexColor("#0B7A6A")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story.append(totals_table)
    story.append(Spacer(1, 4 * mm))

    # ------------------------------------------------------------------
    # Mentions legales
    # ------------------------------------------------------------------
    story.append(Paragraph("Mentions légales", h2))
    if is_credit_note:
        story.append(
            Paragraph(
                "Avoir émis à l'annulation de la vente : aucun règlement n'est attendu "
                "du client, le montant ci-dessus lui a été remboursé.",
                body_small,
            )
        )
    else:
        story.append(Paragraph("Facture payée au comptant, en boutique.", body_small))
    story.append(
        Paragraph(
            "TVA exigible à la livraison. "
            "En cas de retard de paiement, des pénalités de retard sont exigibles au taux "
            "d'intérêt légal en vigueur (art. L.441-10 du code de commerce), ainsi qu'une "
            "indemnité forfaitaire pour frais de recouvrement de 40 € "
            "(art. D.441-5 du code de commerce). Pas d'escompte pour paiement anticipé.",
            body_small,
        )
    )
    story.append(Spacer(1, 2 * mm))
    story.append(
        Paragraph(
            "Auto-attestation art. 286 I-3° bis CGI, version fiscale "
            f"{FISCAL_SIGNATURE_VERSION} du {FISCAL_VERSION_DATE}. "
            "Document conservé 10 ans (art. L.123-22 du code de commerce).",
            body_small,
        )
    )

    doc.build(story, canvasmaker=_invariant_canvas)
    return buf.getvalue()
