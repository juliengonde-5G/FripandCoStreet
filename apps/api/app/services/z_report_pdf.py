# Nouveau service (PR4, docs/ARCHITECTURE_PR4.md §1/§3, F8) — PDF du rapport
# Z (reportlab), extrait/adapte du module equivalent de l'application source
# (`services/z_report_pdf.py`), reduit au perimetre Frip & Co Street (deux
# moyens de paiement — especes/CB, pas de tolerance de caisse configurable)
# et rendu DETERMINISTE (contrairement a l'application
# source, qui laisse reportlab horodater le PDF) : le canvas est cree en
# mode `invariant=1` (reportlab fige alors `CreationDate`/`ModDate` et
# l'identifiant de fichier a une valeur fixe — voir
# `reportlab.lib.utils.TimeStamp` — au lieu de l'horodatage systeme), et le
# contenu du document ne reference AUCUN horodatage de generation (seuls des
# horodatages fixes du Z lui-meme — `opened_at`/`closed_at`/`created_at` —
# apparaissent). Deux appels sur le meme Z produisent donc des octets, et
# donc un SHA-256, identiques. Mention D14 explicite (jamais « conforme
# NF525 » — CLAUDE.md).
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cash_movement import CashMovement, CashMovementDirection
from app.models.pos import CashDrawer, ZReport
from app.version import FISCAL_SIGNATURE_VERSION, FISCAL_VERSION_DATE


def _format_eur(amount: float) -> str:
    return f"{amount:,.2f} €".replace(",", " ").replace(".", ",")


def _reason_label(reason: str) -> str:
    return {
        "bank_deposit": "Dépôt banque",
        "supplier_payment": "Paiement fournisseur",
        "float_top_up": "Réappro. fonds de caisse",
        "other": "Autre",
    }.get(reason, reason)


def _dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.strftime("%d/%m/%Y %H:%M")


async def generate_z_report_pdf(db: AsyncSession, z_report: ZReport) -> bytes:
    """Rend le rapport Z en PDF A4 — octets deterministes (voir docstring
    du module)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    drawer = (
        await db.execute(select(CashDrawer).where(CashDrawer.id == z_report.cash_drawer_id))
    ).scalar_one()

    movements = (
        await db.execute(
            select(CashMovement)
            .where(CashMovement.drawer_id == drawer.id)
            .order_by(CashMovement.created_at.asc())
        )
    ).scalars().all()

    # PR8 (J2) — ventilation des ventes par vendeuse. Recalculee a la
    # lecture depuis les ventes de la periode (immuables) et triee de facon
    # deterministe : le PDF reste octet pour octet identique d'un rendu a
    # l'autre, donc son SHA-256 aussi.
    from app.services.cashier_service import sales_by_cashier_for

    by_cashier = await sales_by_cashier_for(db, z_report)

    def _invariant_canvas(*args, **kwargs):
        kwargs["invariant"] = 1
        # Compression desactivee : le flux de contenu reste en clair, ce qui
        # permet de verifier par simple recherche d'octets (tests, audit
        # manuel) que le PDF porte bien la mention D14 et jamais « conforme
        # NF525 » — sans rien changer au determinisme (zlib serait lui aussi
        # deterministe a niveau egal, ce n'est qu'un choix de lisibilite).
        kwargs["pageCompression"] = 0
        return Canvas(*args, **kwargs)

    from io import BytesIO

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=14 * mm,
        title=f"Rapport Z #{z_report.report_number}",
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("ZH1", parent=styles["Title"], fontSize=18, leading=22, spaceAfter=4)
    h2 = ParagraphStyle(
        "ZH2", parent=styles["Heading2"], fontSize=11, leading=14,
        textColor=colors.HexColor("#0B7A6A"), spaceBefore=6, spaceAfter=2,
    )
    body = ParagraphStyle("ZBody", parent=styles["Normal"], fontSize=9, leading=12)
    body_right = ParagraphStyle("ZBodyRight", parent=body, alignment=2)
    body_small = ParagraphStyle("ZBodySmall", parent=styles["Normal"], fontSize=8, leading=10)
    body_alert = ParagraphStyle("ZBodyAlert", parent=body, textColor=colors.HexColor("#9B2D2D"))

    story: list = []

    story.append(Paragraph(f"<b>Rapport Z #{z_report.report_number:04d}</b>", h1))

    if getattr(z_report, "is_regularization", False):
        story.append(
            Paragraph(
                "<b>⚠ CLÔTURE DE RÉGULARISATION A POSTERIORI</b><br/>"
                f"Motif : {z_report.regularization_reason or '—'}",
                body,
            )
        )
        story.append(Spacer(1, 2 * mm))

    head_meta = "<br/>".join(
        [
            "<b>Frip &amp; Co Street</b>",
            f"Période : {_dt(drawer.opened_at)} → {_dt(drawer.closed_at)}",
            f"Caisse comptée : {'oui' if z_report.counted else 'non (garde 23:59)'}",
            f"Émis le : {_dt(z_report.created_at)}",
        ]
    )
    story.append(Paragraph(head_meta, body))
    story.append(Spacer(1, 4 * mm))

    # ------------------------------------------------------------------
    # Caisse : fond / attendu / compté / écart
    # ------------------------------------------------------------------
    story.append(Paragraph("Réconciliation tiroir-caisse", h2))
    discrepancy = float(z_report.discrepancy or 0)
    drawer_rows = [
        ["Fond initial", _format_eur(float(z_report.opening_amount))],
        ["Entrées (mouvements)", _format_eur(float(z_report.cash_in_total))],
        ["Sorties (mouvements)", "-" + _format_eur(float(z_report.cash_out_total))],
        ["Attendu en caisse", _format_eur(float(z_report.expected_amount))],
        ["Compté en caisse", _format_eur(float(z_report.closing_amount))],
    ]
    sign = "+" if discrepancy >= 0 else "-"
    drawer_rows.append(["Écart", f"{sign}{_format_eur(abs(discrepancy))}"])
    drawer_table = Table(drawer_rows, colWidths=[80 * mm, 40 * mm], hAlign="LEFT")
    drawer_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.2, colors.HexColor("#D5D3CC")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story.append(drawer_table)
    if abs(discrepancy) > 0.01:
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph("<b>⚠ Écart de caisse constaté</b>", body_alert))
        if drawer.closing_note:
            story.append(Paragraph("Commentaire : " + drawer.closing_note, body_small))
    story.append(Spacer(1, 4 * mm))

    # ------------------------------------------------------------------
    # Encaissements par moyen de paiement
    # ------------------------------------------------------------------
    story.append(Paragraph("Encaissements par moyen de paiement", h2))
    method_header = [
        Paragraph("<b>Moyen</b>", body),
        Paragraph("<b>Ventes</b>", body_right),
        Paragraph("<b>Remb.</b>", body_right),
        Paragraph("<b>Net</b>", body_right),
    ]
    method_rows = [method_header]
    payment_totals = z_report.payment_totals or {}
    method_labels = {"cash": "Espèces", "card": "Carte bancaire"}
    for method, label in method_labels.items():
        bucket = payment_totals.get(method)
        if not bucket:
            continue
        method_rows.append(
            [
                Paragraph(label, body),
                Paragraph(_format_eur(float(bucket.get("sales", 0))), body_right),
                Paragraph("-" + _format_eur(float(bucket.get("refunds", 0))), body_right),
                Paragraph(_format_eur(float(bucket.get("net", 0))), body_right),
            ]
        )
    method_rows.append(
        [
            Paragraph("<b>Total</b>", body),
            Paragraph(_format_eur(float(z_report.total_sales)), body_right),
            Paragraph("-" + _format_eur(float(z_report.total_refunds)), body_right),
            Paragraph(_format_eur(float(z_report.total_net)), body_right),
        ]
    )
    methods_table = Table(method_rows, colWidths=[45 * mm, 35 * mm, 35 * mm, 35 * mm])
    methods_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ECEAE3")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.HexColor("#0E0E0C")),
                ("LINEBELOW", (0, 1), (-1, -2), 0.2, colors.HexColor("#D5D3CC")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.HexColor("#0B7A6A")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(methods_table)
    story.append(Spacer(1, 2 * mm))
    story.append(
        Paragraph(
            f"HT net : {_format_eur(float(z_report.total_ht))} — "
            f"TVA nette : {_format_eur(float(z_report.total_tva))} — "
            f"Transactions : {z_report.transaction_count}",
            body_small,
        )
    )
    story.append(Spacer(1, 4 * mm))

    # ------------------------------------------------------------------
    # Ventes par vendeuse (PR8/J2) — absente des Z anterieurs a PR8 et des
    # journees encaissees sans identification : la section n'apparait que
    # lorsqu'il y a quelque chose a ventiler.
    # ------------------------------------------------------------------
    if by_cashier:
        story.append(Paragraph("Ventes par vendeuse", h2))
        # PR9/K0 — trois colonnes : brut des ventes, annulations, net. Le
        # total de la colonne Net se reconcilie avec le net du Z ; sans
        # elle, une vendeuse dont la vente a ete annulee affichait un
        # chiffre que rien ne recoupait.
        cashier_rows = [
            [
                Paragraph("<b>Vendeuse</b>", body),
                Paragraph("<b>Ventes</b>", body_right),
                Paragraph("<b>Annulations</b>", body_right),
                Paragraph("<b>Net</b>", body_right),
            ]
        ]
        for entry in by_cashier:
            # `.get` avec repli : un Z peut etre rendu a partir d'un
            # dictionnaire produit avant PR9 (cache, test, appelant tiers).
            sales_total = float(entry["sales_total"])
            refunds_total = float(entry.get("refunds_total", 0.0))
            net_total = float(entry.get("net_total", sales_total - refunds_total))
            cashier_rows.append(
                [
                    Paragraph(entry["display_name"], body),
                    Paragraph(
                        f"{entry['sales_count']} · {_format_eur(sales_total)}", body_right
                    ),
                    Paragraph(
                        f"{int(entry.get('refunds_count', 0))} · {_format_eur(refunds_total)}",
                        body_right,
                    ),
                    Paragraph(_format_eur(net_total), body_right),
                ]
            )
        cashier_table = Table(cashier_rows, colWidths=[60 * mm, 35 * mm, 35 * mm, 30 * mm])
        cashier_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ECEAE3")),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.HexColor("#0E0E0C")),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.2, colors.HexColor("#D5D3CC")),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(cashier_table)
        story.append(Spacer(1, 4 * mm))

    # ------------------------------------------------------------------
    # Mouvements de caisse
    # ------------------------------------------------------------------
    if movements:
        story.append(Paragraph("Mouvements de caisse", h2))
        mv_rows = [
            [
                Paragraph("<b>Heure</b>", body),
                Paragraph("<b>Sens</b>", body),
                Paragraph("<b>Motif</b>", body),
                Paragraph("<b>Montant</b>", body_right),
                Paragraph("<b>Note</b>", body),
            ]
        ]
        for mv in movements:
            ts = mv.created_at.strftime("%H:%M") if mv.created_at else ""
            sign_mv = "Entrée" if mv.direction == CashMovementDirection.inflow else "Sortie"
            mv_rows.append(
                [
                    Paragraph(ts, body),
                    Paragraph(sign_mv, body),
                    Paragraph(_reason_label(mv.reason.value), body),
                    Paragraph(_format_eur(float(mv.amount)), body_right),
                    Paragraph(mv.note or "—", body_small),
                ]
            )
        mv_table = Table(mv_rows, colWidths=[18 * mm, 18 * mm, 45 * mm, 30 * mm, 65 * mm])
        mv_table.setStyle(
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
        story.append(mv_table)
        story.append(Spacer(1, 4 * mm))

    # ------------------------------------------------------------------
    # Cumuls perpétuels
    # ------------------------------------------------------------------
    story.append(Paragraph("Cumuls perpétuels", h2))
    story.append(
        Paragraph(
            f"Ventes cumulées : {_format_eur(float(z_report.cumulative_sales))} — "
            f"Remboursements cumulés : {_format_eur(float(z_report.cumulative_refunds))} — "
            f"Net cumulé : {_format_eur(float(z_report.cumulative_net))} — "
            f"Transactions cumulées : {z_report.cumulative_transaction_count}",
            body_small,
        )
    )
    story.append(Spacer(1, 4 * mm))

    # ------------------------------------------------------------------
    # Chaîne de preuve + mention D14
    # ------------------------------------------------------------------
    chain = (
        f"Hash courant : {z_report.hash}<br/>"
        f"Hash précédent : {z_report.previous_hash or '0'}<br/>"
        f"Version de signature fiscale : {z_report.fiscal_signature_version}"
    )
    story.append(Paragraph(chain, body_small))
    story.append(Spacer(1, 2 * mm))
    story.append(
        Paragraph(
            f"Auto-attestation art. 286 I-3° bis CGI, version fiscale "
            f"{FISCAL_SIGNATURE_VERSION} du {FISCAL_VERSION_DATE}. "
            "Document conservé 6 ans (art. L.102B LPF).",
            body_small,
        )
    )

    doc.build(story, canvasmaker=_invariant_canvas)
    return buf.getvalue()
