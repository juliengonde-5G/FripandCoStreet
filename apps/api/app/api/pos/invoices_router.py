# Nouveau routeur (PR8, docs/ARCHITECTURE_PR8.md, contrat J5) — facture B2B
# et avoir. Monte a part dans `app/main.py` (meme prefixe `/pos` que
# `pos/router.py`) : ce bloc est entierement nouveau, il n'a rien a aller
# chercher dans le routeur de caisse.
#
# L'avoir n'a PAS de route d'emission : il est produit automatiquement par
# l'annulation d'une vente facturee (`services/refund.py` ->
# `InvoiceService.credit_note_for_cancellation`). On ne peut donc pas
# fabriquer un avoir sans annulation correspondante.
from __future__ import annotations

import hashlib
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.invoice_service import (
    InvoiceData,
    InvoiceNotFound,
    InvoiceService,
    PdfMismatch,
)
from app.services.jet import (
    EVENT_EXPORT_DOWNLOADED,
    EVENT_SYSTEM_INTEGRITY_ALERT,
    JournalService,
)

router = APIRouter(prefix="/pos", tags=["pos"])


class IssueInvoiceRequest(BaseModel):
    """Coordonnees du client professionnel.

    Aucune validation de SIRET/TVA ici : elle vit dans le service
    (`normalize_siret`/`normalize_vat_number`), pour que l'erreur soit une
    erreur METIER `{detail, code}` lisible en caisse (`invalid_siret`,
    `invalid_vat_number`) et non un 422 Pydantic illisible."""

    company_name: str
    siret: str
    vat_number: str | None = None
    address_line1: str
    address_line2: str | None = None
    postal_code: str
    city: str


@router.post("/transactions/{transaction_id}/invoice", status_code=201)
async def issue_invoice(
    transaction_id: uuid.UUID,
    body: IssueInvoiceRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    service = InvoiceService(db)
    invoice = await service.issue(
        transaction_id,
        InvoiceData(
            company_name=body.company_name,
            siret=body.siret,
            vat_number=body.vat_number,
            address_line1=body.address_line1,
            address_line2=body.address_line2,
            postal_code=body.postal_code,
            city=body.city,
        ),
        user.id,
    )
    payload = await service.serialize_with_transaction(invoice)
    await db.commit()
    return {"invoice": payload}


@router.get("/transactions/{transaction_id}/invoice")
async def get_transaction_invoice(
    transaction_id: uuid.UUID,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    service = InvoiceService(db)
    invoice = await service.get_for_transaction(transaction_id)
    if invoice is None:
        raise InvoiceNotFound("Aucune facture pour cette vente.")
    return {"invoice": await service.serialize_with_transaction(invoice)}


@router.get("/invoices/{invoice_id}/pdf")
async def get_invoice_pdf(
    invoice_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PDF deterministe de la facture (ou de l'avoir).

    Premier telechargement : l'empreinte SHA-256 du document est SCELLEE
    dans `invoices.pdf_sha256` (seul UPDATE que le trigger d'immuabilite
    tolere, et une seule fois). Telechargements suivants : le PDF est
    regenere et son empreinte comparee a celle scellee — une divergence
    signifie que le document servi aujourd'hui n'est plus celui remis au
    client, on refuse donc de le servir (500 `pdf_mismatch`) apres avoir
    journalise `system.integrity_alert`.
    """
    service = InvoiceService(db)
    invoice = await service.get(invoice_id)
    if invoice is None:
        raise InvoiceNotFound()

    pdf_bytes = await service.render_pdf(invoice)
    sha = hashlib.sha256(pdf_bytes).hexdigest()

    try:
        await service.seal_pdf_sha256(invoice, sha)
    except PdfMismatch as exc:
        # `get_db` fait un ROLLBACK sur toute exception qui remonte au
        # routeur : l'alerte doit donc etre COMMITTEE ici, sans quoi
        # l'incident d'integrite ne laisserait aucune trace.
        await JournalService(db).record(
            EVENT_SYSTEM_INTEGRITY_ALERT,
            user_id=user.id,
            payload={
                "kind": "invoice_pdf",
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "expected_sha256": invoice.pdf_sha256,
                "computed_sha256": sha,
            },
        )
        await db.commit()
        raise exc

    await JournalService(db).record(
        EVENT_EXPORT_DOWNLOADED,
        user_id=user.id,
        payload={
            "kind": "invoice_pdf",
            "period_start": None,
            "period_end": None,
            "sha256": sha,
            "rows": 1,
            "invoice_number": invoice.invoice_number,
        },
    )
    await db.commit()
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{invoice.invoice_number}.pdf"',
            "X-PDF-SHA256": sha,
        },
    )
