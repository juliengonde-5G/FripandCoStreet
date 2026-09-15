# Nouveau service (PR8, docs/ARCHITECTURE_PR8.md, contrat J5) — facture B2B
# et avoir.
#
# Deux regles structurantes, qui expliquent la forme du module :
#
# 1. Le client professionnel n'est PAS une fiche `clients`. Une raison
#    sociale et un SIRET ne sont pas des donnees personnelles de personne
#    physique : elles sont FIGEES dans la facture au moment de l'emission
#    (conservation 10 ans), hors du perimetre de l'anonymisation RGPD, et
#    ne passent JAMAIS par le registre de consentement.
#
# 2. La facture ne recopie PAS les montants de la vente en base : elle les
#    LIT sur la transaction (`total_ht`/`total_tva`/`total_ttc`), qui est
#    signee et immuable (trigger `fripco_protect_signed_transaction`).
#    Dupliquer les totaux dans `invoices` creerait une seconde source de
#    verite qu'il faudrait ensuite prouver egale a la premiere ; les lire
#    rend la divergence impossible par construction. C'est aussi pourquoi
#    la table `invoices` (migration 0008) ne porte aucune colonne de
#    montant.
#
# La numerotation (`F-AAAA-NNNN` / `A-AAAA-NNNN`, compteur par annee civile)
# est attribuee sous le MEME verrou consultatif que les ventes et les Z
# (`pg_advisory_xact_lock(5252026)`, `services/fiscal.py`) : deux emissions
# concurrentes obtiennent donc deux numeros distincts et consecutifs, sans
# trou ni doublon.
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.invoice import Invoice, InvoiceKind
from app.models.pos import Transaction, TransactionType
from app.services.fiscal import PosServiceError, acquire_fiscal_write_lock
from app.services.jet import (
    EVENT_INVOICE_CREDIT_NOTE_ISSUED,
    EVENT_INVOICE_ISSUED,
    JournalService,
)
from app.services.settings_service import SettingsService

_PARIS = ZoneInfo("Europe/Paris")

INVOICE_PREFIX = "F"
CREDIT_NOTE_PREFIX = "A"

_PREFIX_BY_KIND = {
    InvoiceKind.invoice: INVOICE_PREFIX,
    InvoiceKind.credit_note: CREDIT_NOTE_PREFIX,
}

_NUMBER_RE = re.compile(r"^[FA]-(\d{4})-(\d+)$")
# `FR` + cle de controle 2 caracteres + SIREN 9 chiffres = 13 caracteres
# (contrat J5 : « FR + 11 »). La cle peut etre alphanumerique (elle l'est
# pour certains numeros attribues avant 2004), le SIREN est numerique.
_VAT_RE = re.compile(r"^FR[0-9A-Z]{2}\d{9}$")


class InvoiceError(PosServiceError):
    status_code = 422
    code = "invoice_error"


class InvalidSiret(InvoiceError):
    status_code = 422
    code = "invalid_siret"

    def __init__(self, message: str = "SIRET invalide (14 chiffres, clé de contrôle)."):
        super().__init__(message)


class InvalidVatNumber(InvoiceError):
    status_code = 422
    code = "invalid_vat_number"

    def __init__(
        self, message: str = "Numéro de TVA invalide (FR suivi de 11 caractères)."
    ):
        super().__init__(message)


class NotASale(InvoiceError):
    """Seule une VENTE peut porter une facture : ni une annulation (qui
    porte un avoir, emis automatiquement), ni quoi que ce soit d'autre."""

    status_code = 409
    code = "not_a_sale"

    def __init__(self, message: str = "Seule une vente peut faire l'objet d'une facture."):
        super().__init__(message)


class TransactionCancelled(InvoiceError):
    status_code = 409
    code = "transaction_cancelled"

    def __init__(self, message: str = "Cette vente a été annulée : facture impossible."):
        super().__init__(message)


class InvoiceExists(InvoiceError):
    status_code = 409
    code = "invoice_exists"

    def __init__(self, message: str = "Une facture a déjà été émise pour cette vente."):
        super().__init__(message)


class InvoicedPartialRefund(InvoiceError):
    """Hors perimetre (§2 du contrat) : l'avoir couvre l'annulation TOTALE.
    Une annulation partielle d'une vente facturee n'a pas de document
    correspondant, elle est donc refusee plutot que produite a moitie."""

    status_code = 409
    code = "invoiced_partial_refund"

    def __init__(
        self,
        message: str = (
            "Cette vente est facturée : seule une annulation totale est possible "
            "(un avoir couvre la facture entière)."
        ),
    ):
        super().__init__(message)


class InvoiceNotFound(InvoiceError):
    status_code = 404
    code = "not_found"

    def __init__(self, message: str = "Facture introuvable."):
        super().__init__(message)


class PdfMismatch(InvoiceError):
    """Le PDF regenere ne correspond plus a l'empreinte scellee au premier
    telechargement : on refuse de servir le document plutot que de laisser
    circuler deux versions d'une meme facture."""

    status_code = 500
    code = "pdf_mismatch"

    def __init__(
        self,
        message: str = (
            "L'empreinte du PDF de cette facture ne correspond plus à celle enregistrée "
            "lors du premier téléchargement."
        ),
    ):
        super().__init__(message)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def normalize_siret(raw: str | None) -> str:
    """SIRET nettoye (espaces/points retires) et valide : 14 chiffres +
    cle de Luhn.

    La validation vit ICI et pas en base : une contrainte CHECK figerait
    l'algorithme dans le schema, alors que la regle de controle est une
    regle metier (elle connait par exemple les exceptions La Poste, que
    nous n'avons pas a gerer pour un client de boutique)."""
    digits = re.sub(r"[\s.\-]", "", raw or "")
    if not re.fullmatch(r"\d{14}", digits):
        raise InvalidSiret()
    if not _luhn_ok(digits):
        raise InvalidSiret()
    return digits


def _luhn_ok(digits: str) -> bool:
    total = 0
    # Luhn a partir de la DROITE : un chiffre sur deux (en partant de
    # l'avant-dernier) est double, et un resultat > 9 est ramene a sa somme
    # de chiffres.
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def normalize_vat_number(raw: str | None) -> str | None:
    """Numero de TVA intracommunautaire francais, ou ``None`` (le champ est
    facultatif : un auto-entrepreneur en franchise de TVA n'en a pas)."""
    value = re.sub(r"[\s.\-]", "", (raw or "")).upper()
    if not value:
        return None
    if not _VAT_RE.fullmatch(value):
        raise InvalidVatNumber()
    return value


def _required(value: str | None, label: str, *, max_length: int) -> str:
    text = (value or "").strip()
    if not text:
        raise InvoiceError(f"{label} est obligatoire pour établir une facture.")
    if len(text) > max_length:
        raise InvoiceError(f"{label} est trop long ({max_length} caractères maximum).")
    return text


def _optional(value: str | None, label: str, *, max_length: int) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > max_length:
        raise InvoiceError(f"{label} est trop long ({max_length} caractères maximum).")
    return text


# Champs du bloc vendeur figes sur la facture. Liste explicite (et pas une
# copie integrale des reglages `shop`) : le document n'a besoin que de ces
# coordonnees, et une cle ajoutee plus tard aux reglages ne doit pas
# changer la forme d'un bloc deja fige sur des factures existantes.
SELLER_SNAPSHOT_FIELDS = (
    "name",
    "address_line1",
    "address_line2",
    "postal_code",
    "city",
    "phone",
    "email",
    "siret",
    "vat_number",
)

DEFAULT_SELLER_NAME = "Frip & Co Street"


def build_seller_snapshot(shop: dict | None) -> dict:
    """Bloc vendeur fige a l'emission, a partir des reglages boutique.

    C'est LUI, et jamais les reglages courants, qui sert a rendre le PDF :
    une facture doit rester reproductible a vie, or la commercante peut
    changer de nom commercial, d'adresse ou de telephone apres coup — le
    document rendu changerait alors, et l'empreinte `pdf_sha256` scellee au
    premier telechargement ne correspondrait plus.
    """
    shop = shop if isinstance(shop, dict) else {}
    snapshot = {field: (shop.get(field) or "") for field in SELLER_SNAPSHOT_FIELDS}
    snapshot["name"] = snapshot["name"] or DEFAULT_SELLER_NAME
    return snapshot


@dataclass(frozen=True)
class InvoiceData:
    """Coordonnees du client professionnel, telles que saisies en caisse."""

    company_name: str
    siret: str
    address_line1: str
    postal_code: str
    city: str
    vat_number: str | None = None
    address_line2: str | None = None


def _money(value) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.01')):.2f}"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class InvoiceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    async def issue(
        self,
        transaction_id: uuid.UUID,
        data: InvoiceData,
        user_id: uuid.UUID | None,
    ) -> Invoice:
        """Emet la facture d'une vente (J5).

        Refus : vente inconnue (404), transaction qui n'est pas une vente
        (409 `not_a_sale`), vente deja annulee (409 `transaction_cancelled`),
        vente deja facturee (409 `invoice_exists`).
        """
        company_name = _required(data.company_name, "La raison sociale", max_length=120)
        siret = normalize_siret(data.siret)
        vat_number = normalize_vat_number(data.vat_number)
        address_line1 = _required(data.address_line1, "L'adresse", max_length=120)
        address_line2 = _optional(data.address_line2, "Le complément d'adresse", max_length=120)
        postal_code = _required(data.postal_code, "Le code postal", max_length=10)
        city = _required(data.city, "La ville", max_length=80)

        transaction = await self._get_transaction(transaction_id)
        if transaction.transaction_type != TransactionType.sale:
            raise NotASale()

        # Verrou fiscal : il protege a la fois le controle « deja facturee »
        # et l'attribution du numero. Deux emissions concurrentes sur la
        # meme vente ne peuvent donc pas passer toutes les deux le controle.
        await acquire_fiscal_write_lock(self.db)

        if await self._refund_of(transaction.id) is not None:
            raise TransactionCancelled()
        if await self.get_for_transaction(transaction.id, kind=InvoiceKind.invoice) is not None:
            raise InvoiceExists()

        issued_at = datetime.now(timezone.utc)
        # Reglages boutique lus UNE fois, ici, et figes dans la ligne : le
        # rendu du PDF n'ira jamais les relire (cf. `build_seller_snapshot`).
        seller_snapshot = build_seller_snapshot(await SettingsService(self.db).get("shop"))
        invoice = Invoice(
            transaction_id=transaction.id,
            kind=InvoiceKind.invoice,
            invoice_number=await self._next_number(InvoiceKind.invoice, issued_at),
            seller_snapshot=seller_snapshot,
            company_name=company_name,
            siret=siret,
            vat_number=vat_number,
            address_line1=address_line1,
            address_line2=address_line2,
            postal_code=postal_code,
            city=city,
            issued_at=issued_at,
            user_id=user_id,
        )
        self.db.add(invoice)
        await self.db.flush()

        # JET : identifiants SEULEMENT. La raison sociale et le SIRET ne
        # sont pas des donnees personnelles, mais le journal est immuable —
        # on s'y tient au principe « des identifiants, jamais du contenu ».
        await JournalService(self.db).record(
            EVENT_INVOICE_ISSUED,
            user_id=user_id,
            payload={
                "invoice_id": str(invoice.id),
                "transaction_id": str(transaction.id),
                "invoice_number": invoice.invoice_number,
            },
        )
        await self.db.flush()
        return invoice

    async def credit_note_for_cancellation(
        self,
        transaction: Transaction,
        refund: Transaction,
        *,
        user_id: uuid.UUID | None = None,
    ) -> Invoice | None:
        """Avoir emis AUTOMATIQUEMENT a l'annulation d'une vente facturee.

        Appele depuis `RefundService.cancel_transaction`, dans la meme
        transaction SQL et sous le meme verrou fiscal que l'annulation
        elle-meme. Retourne ``None`` si la vente n'etait pas facturee (cas
        de loin le plus frequent : rien a faire).

        ``transaction`` est la vente d'origine, ``refund`` l'annulation qui
        vient d'etre signee. L'avoir porte sur l'ANNULATION
        (`transaction_id = refund.id`) et pointe vers la facture qu'il
        annule (`original_invoice_id`).
        """
        invoice = await self.get_for_transaction(transaction.id, kind=InvoiceKind.invoice)
        if invoice is None:
            return None

        # Partiel vs total : `cancel_transaction` recopie a l'identique les
        # totaux de la vente (annulation TOTALE, D4). Un remboursement qui
        # ne solde pas la vente entiere est donc, par construction, un
        # remboursement partiel — sans document d'avoir possible (§2).
        if _money(refund.total_ttc) != _money(transaction.total_ttc):
            raise InvoicedPartialRefund()

        await acquire_fiscal_write_lock(self.db)
        existing = await self.get_for_transaction(refund.id, kind=InvoiceKind.credit_note)
        if existing is not None:
            return existing

        issued_at = datetime.now(timezone.utc)
        credit_note = Invoice(
            transaction_id=refund.id,
            kind=InvoiceKind.credit_note,
            invoice_number=await self._next_number(InvoiceKind.credit_note, issued_at),
            original_invoice_id=invoice.id,
            # Coordonnees RECOPIEES de la facture d'origine : l'avoir est
            # adresse au meme professionnel, tel qu'il etait identifie au
            # moment de la vente.
            company_name=invoice.company_name,
            siret=invoice.siret,
            vat_number=invoice.vat_number,
            address_line1=invoice.address_line1,
            address_line2=invoice.address_line2,
            postal_code=invoice.postal_code,
            city=invoice.city,
            # Bloc vendeur RECOPIE de la facture d'origine : l'avoir est
            # emis par le meme vendeur, tel qu'il etait identifie au moment
            # de la vente — pas tel que les reglages le decrivent le jour
            # de l'annulation.
            seller_snapshot=invoice.seller_snapshot,
            issued_at=issued_at,
            user_id=user_id,
        )
        self.db.add(credit_note)
        await self.db.flush()

        await JournalService(self.db).record(
            EVENT_INVOICE_CREDIT_NOTE_ISSUED,
            user_id=user_id,
            payload={
                "invoice_id": str(credit_note.id),
                "transaction_id": str(refund.id),
                "invoice_number": credit_note.invoice_number,
                "original_invoice_id": str(invoice.id),
            },
        )
        await self.db.flush()
        return credit_note

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------

    async def get(self, invoice_id: uuid.UUID) -> Invoice | None:
        return (
            await self.db.execute(select(Invoice).where(Invoice.id == invoice_id))
        ).scalar_one_or_none()

    async def get_for_transaction(
        self, transaction_id: uuid.UUID, *, kind: InvoiceKind | None = None
    ) -> Invoice | None:
        query = select(Invoice).where(Invoice.transaction_id == transaction_id)
        if kind is not None:
            query = query.where(Invoice.kind == kind)
        return (await self.db.execute(query.limit(1))).scalar_one_or_none()

    async def list_for_year(self, year: int) -> list[Invoice]:
        """Factures ET avoirs d'une annee civile, du plus recent au plus
        ancien (l'ordre d'affichage de l'onglet Comptabilite)."""
        return list(
            (
                await self.db.execute(
                    select(Invoice)
                    .where(
                        Invoice.invoice_number.like(f"{INVOICE_PREFIX}-{year:04d}-%")
                        | Invoice.invoice_number.like(f"{CREDIT_NOTE_PREFIX}-{year:04d}-%")
                    )
                    .order_by(Invoice.issued_at.desc(), Invoice.invoice_number.desc())
                )
            ).scalars().all()
        )

    async def numbers_by_transaction(
        self, transaction_ids: list[uuid.UUID]
    ) -> dict[str, str]:
        """``{str(transaction_id): invoice_number}`` pour toute une page de
        tickets — UNE requete, jamais une par ligne."""
        ids = [tid for tid in transaction_ids if tid is not None]
        if not ids:
            return {}
        rows = (
            await self.db.execute(
                select(Invoice.transaction_id, Invoice.invoice_number).where(
                    Invoice.transaction_id.in_(ids)
                )
            )
        ).all()
        return {str(tid): number for tid, number in rows}

    async def transaction_for(self, invoice: Invoice) -> Transaction:
        return await self._get_transaction(invoice.transaction_id)

    async def render_pdf(self, invoice: Invoice) -> bytes:
        """Charge ce dont le rendu a besoin (la vente, ses lignes, et le
        numero de la facture annulee s'il s'agit d'un avoir), puis appelle
        le rendu — qui est une fonction PURE, sans acces base : tout ce
        qu'il imprime vient de la facture elle-meme (bloc vendeur fige
        inclus) et de la vente signee."""
        from app.models.pos import TransactionItem
        from app.services.invoice_pdf import render_invoice_pdf

        transaction = await self.transaction_for(invoice)
        items = (
            await self.db.execute(
                select(TransactionItem)
                .where(TransactionItem.transaction_id == transaction.id)
                .order_by(TransactionItem.position.asc())
            )
        ).scalars().all()
        original_number = None
        if invoice.original_invoice_id is not None:
            original_number = (
                await self.db.execute(
                    select(Invoice.invoice_number).where(
                        Invoice.id == invoice.original_invoice_id
                    )
                )
            ).scalar_one_or_none()
        return render_invoice_pdf(
            invoice, transaction, items, original_invoice_number=original_number
        )

    # ------------------------------------------------------------------
    # Empreinte du PDF (posee une fois, verifiee ensuite)
    # ------------------------------------------------------------------

    async def seal_pdf_sha256(self, invoice: Invoice, sha256: str) -> bool:
        """Pose `pdf_sha256` au PREMIER telechargement, verifie l'egalite
        aux suivants. Retourne ``True`` si l'empreinte vient d'etre posee.

        L'UPDATE est conditionne a `pdf_sha256 IS NULL` cote SQL : deux
        premiers telechargements concurrents ne peuvent pas declencher deux
        fois le trigger d'immuabilite (le second ne trouve aucune ligne a
        modifier et retombe sur la verification d'egalite).
        """
        if invoice.pdf_sha256 is not None:
            if invoice.pdf_sha256 != sha256:
                raise PdfMismatch()
            return False

        result = await self.db.execute(
            Invoice.__table__.update()
            .where(Invoice.__table__.c.id == invoice.id)
            .where(Invoice.__table__.c.pdf_sha256.is_(None))
            .values(pdf_sha256=sha256)
        )
        if result.rowcount:
            # L'objet en session porte encore `None` : on le remet en phase
            # SANS le marquer « sale » (`set_committed_value`). Une simple
            # affectation declencherait un second UPDATE au prochain flush —
            # que le trigger d'immuabilite refuserait, `pdf_sha256` n'etant
            # plus NULL.
            set_committed_value(invoice, "pdf_sha256", sha256)
            return True

        stored = (
            await self.db.execute(
                select(Invoice.pdf_sha256).where(Invoice.id == invoice.id)
            )
        ).scalar_one_or_none()
        if stored != sha256:
            raise PdfMismatch()
        set_committed_value(invoice, "pdf_sha256", stored)
        return False

    # ------------------------------------------------------------------
    # Serialisation (contrat J5)
    # ------------------------------------------------------------------

    @staticmethod
    def serialize(invoice: Invoice, transaction: Transaction) -> dict:
        """Objet `invoice` du contrat. Les totaux sont LUS sur la vente
        (source unique de verite, cf. en-tete du module) et rendus en
        chaines a deux decimales, comme tous les montants exportes."""
        return {
            "id": str(invoice.id),
            "kind": invoice.kind.value,
            "invoice_number": invoice.invoice_number,
            "transaction_id": str(invoice.transaction_id),
            "transaction_number": transaction.transaction_number,
            "original_invoice_id": (
                str(invoice.original_invoice_id) if invoice.original_invoice_id else None
            ),
            "company_name": invoice.company_name,
            "siret": invoice.siret,
            "vat_number": invoice.vat_number,
            "address_line1": invoice.address_line1,
            "address_line2": invoice.address_line2,
            "postal_code": invoice.postal_code,
            "city": invoice.city,
            "seller": invoice.seller_snapshot,
            "total_ht": _money(transaction.total_ht),
            "total_tva": _money(transaction.total_tva),
            "total_ttc": _money(transaction.total_ttc),
            "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
        }

    async def serialize_with_transaction(self, invoice: Invoice) -> dict:
        return self.serialize(invoice, await self.transaction_for(invoice))

    # ------------------------------------------------------------------
    # Interne
    # ------------------------------------------------------------------

    async def _get_transaction(self, transaction_id: uuid.UUID) -> Transaction:
        transaction = (
            await self.db.execute(select(Transaction).where(Transaction.id == transaction_id))
        ).scalar_one_or_none()
        if transaction is None:
            raise InvoiceError("Vente introuvable.", code="not_found", status_code=404)
        return transaction

    async def _refund_of(self, transaction_id: uuid.UUID) -> uuid.UUID | None:
        return (
            await self.db.execute(
                select(Transaction.id).where(
                    Transaction.original_transaction_id == transaction_id,
                    Transaction.transaction_type == TransactionType.refund,
                )
            )
        ).scalar_one_or_none()

    async def _next_number(self, kind: InvoiceKind, issued_at: datetime) -> str:
        """`F-AAAA-NNNN` / `A-AAAA-NNNN`, compteur par annee CIVILE locale.

        L'annee est celle du fuseau de la boutique (Europe/Paris) et non
        UTC : une facture emise le 31 decembre a 23 h 30 porte le millesime
        que la commercante lit sur son calendrier.

        APPELANT : le verrou fiscal doit deja etre tenu (`issue` et
        `credit_note_for_cancellation` s'en chargent) — c'est lui, et non
        un `SELECT ... FOR UPDATE`, qui serialise l'attribution.
        """
        year = issued_at.astimezone(_PARIS).year
        prefix = _PREFIX_BY_KIND[kind]
        numbers = (
            await self.db.execute(
                select(Invoice.invoice_number).where(
                    Invoice.invoice_number.like(f"{prefix}-{year:04d}-%")
                )
            )
        ).scalars().all()
        last = 0
        for number in numbers:
            match = _NUMBER_RE.match(number)
            if match is not None:
                last = max(last, int(match.group(2)))
        return f"{prefix}-{year:04d}-{last + 1:04d}"


# ---------------------------------------------------------------------------
# Export fiscal / archive de cloture
# ---------------------------------------------------------------------------


async def invoice_snapshot_dicts(
    db: AsyncSession,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> list[dict]:
    """Factures et avoirs d'une periode, forme « export » (chaines a deux
    decimales, aucun horodatage de generation).

    Utilise par `FiscalExportService.build_snapshot` — donc, par ricochet,
    par l'archive de cloture scellee (`services/fiscal_closure.py`), qui
    construit sa snapshot avec ce meme service. Les montants sont relus sur
    la transaction associee : une facture ne stocke pas ses totaux."""
    query = select(Invoice)
    if period_from is not None:
        query = query.where(Invoice.issued_at >= period_from)
    if period_to is not None:
        query = query.where(Invoice.issued_at <= period_to)
    invoices = (
        await db.execute(query.order_by(Invoice.invoice_number.asc()))
    ).scalars().all()
    if not invoices:
        return []

    transactions = {
        t.id: t
        for t in (
            await db.execute(
                select(Transaction).where(
                    Transaction.id.in_([i.transaction_id for i in invoices])
                )
            )
        ).scalars().all()
    }
    numbers = {i.id: i.invoice_number for i in invoices}

    payload: list[dict] = []
    for invoice in invoices:
        transaction = transactions.get(invoice.transaction_id)
        payload.append(
            {
                "id": str(invoice.id),
                "kind": invoice.kind.value,
                "invoice_number": invoice.invoice_number,
                "original_invoice_number": (
                    numbers.get(invoice.original_invoice_id)
                    if invoice.original_invoice_id
                    else None
                ),
                "transaction_id": str(invoice.transaction_id),
                "transaction_number": (
                    transaction.transaction_number if transaction is not None else None
                ),
                "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else "",
                "company_name": invoice.company_name,
                "siret": invoice.siret,
                "vat_number": invoice.vat_number,
                "address_line1": invoice.address_line1,
                "address_line2": invoice.address_line2,
                "postal_code": invoice.postal_code,
                "city": invoice.city,
                "seller": invoice.seller_snapshot,
                "total_ht": _money(transaction.total_ht) if transaction is not None else "0.00",
                "total_tva": _money(transaction.total_tva) if transaction is not None else "0.00",
                "total_ttc": _money(transaction.total_ttc) if transaction is not None else "0.00",
                "pdf_sha256": invoice.pdf_sha256,
            }
        )
    return payload
