# Extrait de l'application source (apps/api/app/services/pos.py), fortement reduit au
# perimetre PR2 (docs/ARCHITECTURE_PR2.md §4.1/§4.3) : pas de produits,
# clients, fidelite, coupons, Solde — un panier de lignes libres (label +
# prix TTC), une remise globale, deux moyens de paiement (especes / CB).
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cash_movement import CashMovement, CashMovementDirection, CashMovementReason
from app.models.pos import (
    CashDrawer,
    DiscountType,
    Payment,
    PaymentMethod,
    Transaction,
    TransactionItem,
    TransactionType,
)
from app.services import cash_payment_validator
from app.services.fiscal import (
    DrawerAlreadyOpen,
    DrawerClosed,
    FiscalService,
    NoOpenDrawer,
    PosServiceError,
    acquire_fiscal_write_lock,
)
from app.services.jet import (
    EVENT_CASH_MOVEMENT_CREATED,
    EVENT_CLIENT_LINKED,
    EVENT_DRAWER_CLOSED,
    EVENT_DRAWER_OPENED,
    EVENT_RECEIPT_DUPLICATE,
    EVENT_RECEIPT_EMAIL_FAILED,
    EVENT_RECEIPT_EMAILED,
    EVENT_SALE_CREATED,
    JournalService,
)
from app.services.receipt import ReceiptService, apply_client_line, format_client_label
from app.services.settings_service import SettingsService
from app.services.tva_service import compute_line_totals


class CartInvalid(PosServiceError):
    status_code = 422
    code = "cart_invalid"


class PaymentMismatch(PosServiceError):
    status_code = 422
    code = "payment_mismatch"


class CashCapExceeded(PosServiceError):
    status_code = 422
    code = "cash_cap_exceeded"


class ClientNotFound(PosServiceError):
    """`client_id` inconnu (ou fiche anonymisee) dans le corps d'une vente —
    PR7/I3. 422 plutot que 404 : la ressource visee par le POST (la vente)
    existe bien, c'est un champ du corps qui est invalide."""

    status_code = 422
    code = "client_not_found"

    def __init__(self, message: str = "Fiche client introuvable."):
        super().__init__(message)


class CardNotConfirmedError(PosServiceError):
    status_code = 409
    code = "card_not_confirmed"

    def __init__(self, message: str = "Paiement CB non confirmé par SumUp."):
        super().__init__(message)


# ---------------------------------------------------------------------------
# Interface avec l'agent B (contrat §7) — `app/services/sumup_verify.py`
# n'existe pas forcement au moment ou ce module est importe (parallelisation).
# `verify_card_tender` est un attribut MODULE-LEVEL reassigne paresseusement
# (import local) au premier paiement CB reel. Les tests le remplacent AVANT
# tout appel via `monkeypatch.setattr("app.services.pos.verify_card_tender", fake)` ;
# `_resolve_verify_card_tender` ne reimporte que si l'attribut vaut encore
# None, donc un faux pose par un test n'est jamais ecrase.
#
# `CardTenderInput`/`VerifiedCardTender` (dataclasses definies cote B) ne sont
# JAMAIS importees ici : `_CardTender` (local) porte les deux seuls champs
# d'entree (`checkout_id`, `amount`) et le retour de `verify_card_tender` est
# lu par attribut (duck typing) — ce module reste donc chargeable et testable
# meme si `sumup_verify.py` n'existe pas encore.
verify_card_tender = None


def _resolve_verify_card_tender():
    global verify_card_tender
    if verify_card_tender is None:
        from app.services.sumup_verify import verify_card_tender as _real

        verify_card_tender = _real
    return verify_card_tender


@dataclass(frozen=True)
class _CardTender:
    checkout_id: str
    amount: Decimal


_PARIS = ZoneInfo("Europe/Paris")


def _round_eur(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PosService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Vente (§4.1)
    # ------------------------------------------------------------------

    async def create_transaction(
        self,
        *,
        user_id: uuid.UUID,
        client_uuid: uuid.UUID,
        items: list,
        discount,
        payments: list,
        client_id: uuid.UUID | None = None,
    ) -> tuple[Transaction, bool]:
        """Cree une vente. Retourne ``(transaction, created)`` — ``created``
        est ``False`` quand ``client_uuid`` correspondait deja a une vente
        existante (idempotence, §4.1 point 2).

        ``client_id`` (PR7/I3) rattache la vente a une fiche client DES sa
        creation. Ce rattachement reste HORS SIGNATURE, exactement comme le
        rattachement a posteriori de PR3 : `client_id` n'apparait nulle part
        dans `fiscal.py::_transaction_payload`, donc la valeur du
        `hash_chain` est rigoureusement la meme avec ou sans cliente. La
        colonne est posee a l'INSERT (pas par un UPDATE apres coup) — rien
        n'est donc jamais modifie sur une ligne deja signee.
        """
        existing = await self._find_by_client_uuid(client_uuid)
        if existing is not None:
            return existing, False

        await acquire_fiscal_write_lock(self.db)
        # Un replay concurrent a pu commiter pendant l'attente du verrou.
        existing = await self._find_by_client_uuid(client_uuid)
        if existing is not None:
            return existing, False

        drawer = await self.get_open_drawer()
        if drawer is None:
            raise DrawerClosed()

        client = await self._resolve_client(client_id)

        if not items:
            raise CartInvalid("Le panier est vide.")
        if len(items) > 50:
            raise CartInvalid("50 lignes maximum par vente.")

        resolved: list[dict] = []
        for raw in items:
            label = (getattr(raw, "label", None) or "").strip() or "Article"
            quantity = int(getattr(raw, "quantity", 1) or 1)
            if quantity < 1 or quantity > 99:
                raise CartInvalid(f"Quantité invalide pour « {label} » (1 à 99).")
            unit_price = Decimal(str(getattr(raw, "unit_price")))
            if unit_price < 0:
                raise CartInvalid(f"Prix négatif refusé pour « {label} ».")
            unit_price = _round_eur(unit_price)
            resolved.append({"label": label, "quantity": quantity, "unit_price": unit_price})

        gross_cents = [
            int((row["unit_price"] * row["quantity"] * 100).to_integral_value()) for row in resolved
        ]
        total_gross_cents = sum(gross_cents)
        if total_gross_cents <= 0:
            raise CartInvalid("Le total du panier doit être supérieur à 0.")

        discount_type, discount_value, discount_cents = self._resolve_discount(
            discount, total_gross_cents
        )

        share_cents = self._allocate_discount(gross_cents, discount_cents)

        tva_rate = await SettingsService(self.db).get_tva_rate()

        line_totals = []
        total_ht = Decimal("0")
        total_tva = Decimal("0")
        total_ttc = Decimal("0")
        for row, share in zip(resolved, share_cents):
            line_discount = Decimal(share) / Decimal("100")
            totals = compute_line_totals(
                row["unit_price"], row["quantity"], line_discount, tva_rate
            )
            line_totals.append((row, line_discount, totals))
            total_ht += totals.line_ht
            total_tva += totals.line_tva
            total_ttc += totals.line_ttc

        # Validation des paiements (§4.1 point 6)
        if not payments:
            raise PaymentMismatch("Aucun moyen de paiement fourni.")
        seen_methods: set[str] = set()
        for p in payments:
            method = str(getattr(p, "method"))
            if method in seen_methods:
                raise PaymentMismatch(f"Un seul paiement « {method} » autorisé par vente.")
            seen_methods.add(method)

        total_paid = sum((Decimal(str(getattr(p, "amount"))) for p in payments), Decimal("0"))
        if _round_eur(total_paid) != _round_eur(total_ttc):
            raise PaymentMismatch(
                f"Le total encaissé ({total_paid:.2f} €) ne correspond pas au total de la "
                f"vente ({total_ttc:.2f} €)."
            )

        cash_result = cash_payment_validator.validate(payments)
        if cash_result.over_cap:
            raise CashCapExceeded(cash_result.reason or "Plafond espèces dépassé.")

        # ------------------------------------------------------------
        # Numerotation + creation (verrou deja acquis plus haut)
        # ------------------------------------------------------------
        next_number = (
            await self.db.execute(select(func.coalesce(func.max(Transaction.transaction_number), 0)))
        ).scalar_one() + 1

        transaction = Transaction(
            transaction_number=next_number,
            transaction_type=TransactionType.sale,
            user_id=user_id,
            client_uuid=client_uuid,
            # Hors payload signe (cf. docstring) — pose des l'INSERT.
            client_id=client.id if client is not None else None,
            discount_type=discount_type,
            discount_value=(float(discount_value) if discount_value is not None else None),
            discount_amount=float(Decimal(discount_cents) / Decimal("100")),
            tva_rate=float(tva_rate),
            total_ht=float(total_ht),
            total_tva=float(total_tva),
            total_ttc=float(total_ttc),
            hash_chain="",
            previous_hash="",
            receipt_number=next_number,
        )
        self.db.add(transaction)
        await self.db.flush()

        for position, (row, line_discount, totals) in enumerate(line_totals):
            self.db.add(
                TransactionItem(
                    transaction_id=transaction.id,
                    label=row["label"],
                    quantity=row["quantity"],
                    unit_price=float(row["unit_price"]),
                    discount_amount=float(line_discount),
                    line_total=float(totals.line_ttc),
                    tva_rate=float(totals.tva_rate),
                    line_ht=float(totals.line_ht),
                    line_tva=float(totals.line_tva),
                    position=position,
                )
            )

        methods_used: list[str] = []
        for p in payments:
            method = PaymentMethod(str(getattr(p, "method")))
            amount = _round_eur(Decimal(str(getattr(p, "amount"))))
            payment = Payment(transaction_id=transaction.id, method=method, amount=float(amount))
            if method == PaymentMethod.cash:
                tendered = getattr(p, "tendered_amount", None)
                tendered_amount = _round_eur(Decimal(str(tendered))) if tendered is not None else amount
                if tendered_amount < amount:
                    raise PaymentMismatch("Le montant remis en espèces est inférieur au montant dû.")
                payment.tendered_amount = float(tendered_amount)
                change = tendered_amount - amount
                if change > 0:
                    payment.change_amount = float(change)
            elif method == PaymentMethod.card:
                checkout_id = getattr(p, "checkout_id", None)
                if not checkout_id:
                    raise PaymentMismatch("Un paiement CB nécessite un `checkout_id` SumUp.")
                verifier = _resolve_verify_card_tender()
                try:
                    verified = await verifier(
                        self.db, _CardTender(checkout_id=checkout_id, amount=amount), client_uuid
                    )
                except Exception as exc:  # noqa: BLE001 — cf. commentaire d'interface ci-dessus
                    raise CardNotConfirmedError(str(exc) or None) from exc
                for attr in (
                    "sumup_checkout_id",
                    "sumup_transaction_id",
                    "sumup_transaction_code",
                    "sumup_auth_code",
                    "sumup_card_brand",
                    "sumup_card_last4",
                ):
                    setattr(payment, attr, getattr(verified, attr, None))
            self.db.add(payment)
            methods_used.append(method.value)

        await FiscalService(self.db).sign_transaction(transaction)

        receipt_text = ReceiptService().generate_sale_text(
            transaction,
            shop=await SettingsService(self.db).get("shop"),
            client_label=(
                format_client_label(client.first_name, client.last_name)
                if client is not None
                else None
            ),
        )
        from app.models.receipt import Receipt

        self.db.add(Receipt(transaction_id=transaction.id, content=receipt_text))

        await JournalService(self.db).record(
            EVENT_SALE_CREATED,
            user_id=user_id,
            payload={
                "number": transaction.transaction_number,
                "total_ttc": float(total_ttc),
                "methods": sorted(set(methods_used)),
            },
        )

        if client is not None:
            # I3 — payload strictement limite aux deux identifiants
            # techniques : le JET est immuable, il ne porte jamais de nom,
            # d'e-mail ni de telephone.
            await JournalService(self.db).record(
                EVENT_CLIENT_LINKED,
                user_id=user_id,
                payload={
                    "client_id": str(client.id),
                    "transaction_id": str(transaction.id),
                },
            )

        await self.db.flush()
        # Recharge via `select()` plutot que `refresh()` : `items`/`payments`
        # sont `lazy="selectin"` (mapper-level) — cette strategie ne s'active
        # qu'au chargement par requete, pas sur un `refresh()` (qui ne
        # recharge que les colonnes propres). Sans ce reload, un premier
        # acces synchrone a `transaction.items` hors requete leverait une
        # erreur greenlet (lazy-load implicite en contexte async).
        transaction = (
            await self.db.execute(select(Transaction).where(Transaction.id == transaction.id))
        ).scalar_one()
        return transaction, True

    async def _resolve_client(self, client_id: uuid.UUID | None):
        """Charge la fiche visee par `client_id` (PR7/I3), ou ``None``.

        Une fiche ANONYMISEE (RGPD, E4) est traitee comme introuvable : elle
        ne porte plus aucune donnee personnelle, la rattacher a une vente
        nouvelle n'aurait aucun sens.
        """
        if client_id is None:
            return None
        from app.models.client import Client

        client = (
            await self.db.execute(select(Client).where(Client.id == client_id))
        ).scalar_one_or_none()
        if client is None or client.anonymized_at is not None:
            raise ClientNotFound()
        return client

    @staticmethod
    def _resolve_discount(
        discount, total_gross_cents: int
    ) -> tuple[DiscountType | None, Decimal | None, int]:
        if discount is None:
            return None, None, 0
        type_raw = str(getattr(discount, "type"))
        value = Decimal(str(getattr(discount, "value")))
        if type_raw == "percent":
            if value <= 0 or value > 100:
                raise CartInvalid("Remise en pourcentage invalide (0 à 100).")
            discount_cents = int(
                (Decimal(total_gross_cents) * value / Decimal("100")).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            return DiscountType.percent, value, discount_cents
        if type_raw == "amount":
            discount_cents = int((value * 100).to_integral_value())
            if discount_cents <= 0 or discount_cents > total_gross_cents:
                raise CartInvalid("Remise en euros invalide (doit être comprise entre 0 et le total).")
            return DiscountType.amount, value, discount_cents
        raise CartInvalid(f"Type de remise inconnu : {type_raw!r}")

    @staticmethod
    def _allocate_discount(gross_cents: list[int], discount_cents: int) -> list[int]:
        """Ventile ``discount_cents`` au prorata de ``gross_cents``, le reste
        d'arrondi allant sur la DERNIÈRE ligne (§4.1 point 4)."""
        if discount_cents <= 0 or not gross_cents:
            return [0] * len(gross_cents)
        total = sum(gross_cents)
        shares = []
        allocated = 0
        for cents in gross_cents[:-1]:
            share = (discount_cents * cents) // total
            shares.append(share)
            allocated += share
        shares.append(discount_cents - allocated)
        return shares

    async def _find_by_client_uuid(self, client_uuid: uuid.UUID) -> Transaction | None:
        return (
            await self.db.execute(select(Transaction).where(Transaction.client_uuid == client_uuid))
        ).scalar_one_or_none()

    async def list_transactions(
        self, *, date: str | None = None, limit: int = 100
    ) -> list[Transaction]:
        """Liste les ventes/annulations, jour courant Europe/Paris par
        defaut (contrat §5 : ``GET /pos/transactions?date=YYYY-MM-DD``)."""
        query = select(Transaction).order_by(Transaction.transaction_number.desc()).limit(limit)
        day = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now(_PARIS).date()
        start = datetime(day.year, day.month, day.day, tzinfo=_PARIS)
        end = start + timedelta(days=1)
        query = query.where(Transaction.created_at >= start, Transaction.created_at < end)
        return (await self.db.execute(query)).scalars().all()

    async def get_transaction(self, transaction_id: uuid.UUID) -> Transaction | None:
        return (
            await self.db.execute(select(Transaction).where(Transaction.id == transaction_id))
        ).scalar_one_or_none()

    async def get_receipt(self, transaction_id: uuid.UUID):
        from app.models.receipt import Receipt

        return (
            await self.db.execute(select(Receipt).where(Receipt.transaction_id == transaction_id))
        ).scalar_one_or_none()

    async def render_receipt_text(self, receipt, *, transaction: Transaction | None = None) -> str:
        """Texte du ticket tel qu'il doit etre RENDU aujourd'hui (PR7/I3).

        Le contenu stocke est immuable (trigger `trg_protect_receipt`) :
        c'est la trace du ticket emis, et c'est elle qui part dans
        l'archive fiscale. Mais le rattachement d'une cliente peut changer
        APRES la vente (rattachement a posteriori de PR3, detachement
        `DELETE /pos/transactions/{id}/client`) — la relecture, le renvoi
        par e-mail et la reimpression doivent alors montrer l'etat
        courant, pas celui de l'instant de la vente. Seule la ligne
        « Client : … » est reecrite (`apply_client_line`) ; tout le reste
        du ticket reste figé au mot pres.
        """
        transaction = transaction or receipt.transaction
        return apply_client_line(receipt.content, await self.client_label(transaction))

    async def client_label(self, transaction: Transaction | None) -> str | None:
        """« Prenom N. » de la cliente rattachee a une vente, ou ``None``."""
        if transaction is None or transaction.client_id is None:
            return None
        from app.models.client import Client

        client = (
            await self.db.execute(select(Client).where(Client.id == transaction.client_id))
        ).scalar_one_or_none()
        if client is None:
            return None
        return format_client_label(client.first_name, client.last_name)

    async def get_rendered_receipt_text(
        self, transaction_id: uuid.UUID, *, transaction: Transaction | None = None
    ) -> str | None:
        """`render_receipt_text` pour un ticket charge par son id de vente
        — ``None`` s'il n'y a pas (encore) de ticket."""
        receipt = await self.get_receipt(transaction_id)
        if receipt is None:
            return None
        return await self.render_receipt_text(receipt, transaction=transaction)

    # ------------------------------------------------------------------
    # Caisse espèces (§4.3)
    # ------------------------------------------------------------------

    async def get_open_drawer(self) -> CashDrawer | None:
        return (
            await self.db.execute(select(CashDrawer).where(CashDrawer.is_open.is_(True)).limit(1))
        ).scalar_one_or_none()

    async def open_drawer(
        self, *, user_id: uuid.UUID, opening_amount: Decimal, breakdown: list[dict] | None = None
    ) -> CashDrawer:
        if await self.get_open_drawer() is not None:
            raise DrawerAlreadyOpen()
        drawer = CashDrawer(
            user_id=user_id,
            opened_at=datetime.now(timezone.utc),
            opening_amount=float(_round_eur(opening_amount)),
            opening_breakdown=breakdown,
            is_open=True,
        )
        self.db.add(drawer)
        await self.db.flush()
        await JournalService(self.db).record(
            EVENT_DRAWER_OPENED,
            user_id=user_id,
            payload={"opening_amount": float(opening_amount)},
        )
        await self.db.flush()
        await self.db.refresh(drawer)
        return drawer

    async def add_cash_movement(
        self,
        *,
        user_id: uuid.UUID,
        direction: CashMovementDirection,
        amount: Decimal,
        reason: CashMovementReason,
        note: str | None,
    ) -> CashMovement:
        drawer = await self.get_open_drawer()
        if drawer is None:
            raise DrawerClosed()
        if amount <= 0:
            raise CartInvalid("Le montant du mouvement doit être positif.")
        if reason == CashMovementReason.other and not (note or "").strip():
            raise CartInvalid("Une note est obligatoire pour un mouvement de type « autre ».")
        movement = CashMovement(
            drawer_id=drawer.id,
            direction=direction,
            amount=float(_round_eur(amount)),
            reason=reason,
            note=note,
            user_id=user_id,
        )
        self.db.add(movement)
        await self.db.flush()
        await JournalService(self.db).record(
            EVENT_CASH_MOVEMENT_CREATED,
            user_id=user_id,
            payload={
                "direction": direction.value,
                "amount": float(amount),
                "reason": reason.value,
            },
        )
        await self.db.flush()
        await self.db.refresh(movement)
        return movement

    async def list_cash_movements(self, drawer_id: uuid.UUID) -> list[CashMovement]:
        return (
            await self.db.execute(
                select(CashMovement)
                .where(CashMovement.drawer_id == drawer_id)
                .order_by(CashMovement.created_at.asc())
            )
        ).scalars().all()

    async def drawer_snapshot(self, drawer: CashDrawer) -> dict:
        """Etat live du tiroir ouvert — pas de scellement (contrairement au
        Z), utilise par ``GET /pos/drawer/current``."""
        now = datetime.now(timezone.utc)
        sales_row = (
            await self.db.execute(
                select(func.coalesce(func.sum(Transaction.total_ttc), 0), func.count(Transaction.id))
                .where(
                    Transaction.transaction_type == TransactionType.sale,
                    Transaction.created_at >= drawer.opened_at,
                    Transaction.created_at <= now,
                )
            )
        ).one()
        refunds_total = (
            await self.db.execute(
                select(func.coalesce(func.sum(Transaction.total_ttc), 0)).where(
                    Transaction.transaction_type == TransactionType.refund,
                    Transaction.created_at >= drawer.opened_at,
                    Transaction.created_at <= now,
                )
            )
        ).scalar_one()
        cash_sales = (
            await self.db.execute(
                select(func.coalesce(func.sum(Payment.amount), 0))
                .join(Transaction, Payment.transaction_id == Transaction.id)
                .where(
                    Payment.method == PaymentMethod.cash,
                    Transaction.transaction_type == TransactionType.sale,
                    Transaction.created_at >= drawer.opened_at,
                    Transaction.created_at <= now,
                )
            )
        ).scalar_one()
        cash_refunds = (
            await self.db.execute(
                select(func.coalesce(func.sum(Payment.amount), 0))
                .join(Transaction, Payment.transaction_id == Transaction.id)
                .where(
                    Payment.method == PaymentMethod.cash,
                    Transaction.transaction_type == TransactionType.refund,
                    Transaction.created_at >= drawer.opened_at,
                    Transaction.created_at <= now,
                )
            )
        ).scalar_one()
        movements = await self.list_cash_movements(drawer.id)
        cash_in = sum(
            (Decimal(str(m.amount)) for m in movements if m.direction == CashMovementDirection.inflow),
            Decimal("0"),
        )
        cash_out = sum(
            (Decimal(str(m.amount)) for m in movements if m.direction == CashMovementDirection.outflow),
            Decimal("0"),
        )
        cash_expected = (
            Decimal(str(drawer.opening_amount))
            + Decimal(str(cash_sales))
            - Decimal(str(cash_refunds))
            + cash_in
            - cash_out
        )
        return {
            "sales_count": int(sales_row[1] or 0),
            "sales_total": float(sales_row[0] or 0),
            "refunds_total": float(refunds_total or 0),
            "cash_expected": float(cash_expected),
            "cash_in": float(cash_in),
            "cash_out": float(cash_out),
        }

    async def close_drawer(
        self,
        *,
        user_id: uuid.UUID,
        closing_amount: Decimal,
        breakdown: list[dict] | None = None,
        note: str | None = None,
    ):
        await acquire_fiscal_write_lock(self.db)
        drawer = await self.get_open_drawer()
        if drawer is None:
            raise NoOpenDrawer()
        drawer.closing_breakdown = breakdown
        drawer.closing_note = note
        drawer.closing_amount = float(_round_eur(closing_amount))
        await self.db.flush()

        z_report = await FiscalService(self.db).generate_z_report(drawer, user_id, counted=True)

        await JournalService(self.db).record(
            EVENT_DRAWER_CLOSED,
            user_id=user_id,
            payload={"z_number": z_report.report_number, "discrepancy": float(z_report.discrepancy)},
        )
        await self.db.flush()

        # PR4 (F2, docs/ARCHITECTURE_PR4.md §1/§3) — l'ecriture comptable du Z
        # est creee dans la MEME transaction SQL que la cloture (idempotent :
        # aucun effet si deja creee, ce qui ne peut arriver ici puisque le Z
        # vient d'etre scelle).
        from app.services.accounting_service import AccountingService

        await AccountingService(self.db).create_export_for_z(z_report, user_id=user_id)
        await self.db.flush()

        return z_report

    # ------------------------------------------------------------------
    # Client + ticket par e-mail (PR3, docs/ARCHITECTURE_PR3.md §3/§4)
    # ------------------------------------------------------------------

    async def attach_client_and_send_receipt(
        self,
        *,
        transaction: Transaction,
        email: str,
        first_name: str | None,
        last_name: str | None,
        newsletter_optin: bool,
        send_receipt: bool,
        user_id: uuid.UUID | None,
    ) -> dict:
        """Orchestre `POST /pos/transactions/{id}/client` (§3) : upsert du
        client -> consentement newsletter (si coché ; si la cliente existait
        déjà avec opt-in et décoche, révocation source `pos`) -> lien vente
        -> e-mail du ticket -> synchro Brevo (best-effort, un échec Brevo
        n'empêche ni la vente ni l'e-mail) — `ClientService.sync_brevo` pousse
        le contact si `newsletter_optin`, le retire sinon (revue RGPD :
        point unique, cf. `client_service.py`).

        Revue robustesse (double-tap « Envoyer le ticket ») :
        `upsert_by_email` sérialise par e-mail normalisé (verrou avisory,
        tenu jusqu'au COMMIT de CETTE requête) avant toute lecture/écriture
        sur `clients` — deux appels concurrents pour la même adresse ne
        courent donc plus sur la contrainte unique `clients.email`. Une
        fois le verrou acquis, la vente est RELUE (l'objet `transaction`
        passé en paramètre a pu devenir périmé pendant l'attente) : si elle
        est déjà liée à CE client (deuxième appel, concurrent ou
        séquentiel), la réponse est idempotente — aucun nouvel envoi,
        aucun nouvel évènement JET, aucune nouvelle synchro Brevo.
        """
        from app.models.client import ConsentPurpose, ConsentSource
        from app.services.client_service import ClientService

        clients = ClientService(self.db)
        client, _created = await clients.upsert_by_email(
            email=email, first_name=first_name, last_name=last_name, user_id=user_id
        )

        # Sous le verrou (acquis par `upsert_by_email` ci-dessus) : RELIT la
        # vente plutôt que de faire confiance à l'objet reçu en paramètre.
        # `db.refresh()` — et non un second `select()` — est indispensable
        # ici : `transaction` est déjà dans l'identity map de CETTE session
        # (chargé plus haut par le routeur), donc un simple `select()`
        # renverrait l'objet Python déjà en mémoire SANS relire ses
        # colonnes depuis la base (SQLAlchemy ne rafraîchit pas un objet
        # déjà identifié à partir d'un résultat de requête), et manquerait
        # donc le `client_id` qu'une requête concurrente vient de committer
        # pendant l'attente du verrou.
        await self.db.refresh(transaction)
        if transaction.client_id is not None and transaction.client_id == client.id:
            return await self._idempotent_attach_response(transaction, client)

        await clients.record_consent(
            client=client,
            purpose=ConsentPurpose.newsletter,
            granted=newsletter_optin,
            source=ConsentSource.pos,
            user_id=user_id,
        )
        await clients.link_transaction(transaction=transaction, client=client, user_id=user_id)

        receipt_result = None
        if send_receipt:
            receipt_result = await self._send_receipt_email(
                transaction=transaction, to=client.email, client_id=client.id, user_id=user_id
            )

        brevo_result = await clients.sync_brevo(client, user_id=user_id)

        return {"client": client, "receipt_email": receipt_result, "brevo": brevo_result}

    async def _idempotent_attach_response(self, transaction: Transaction, client) -> dict:
        """Réponse d'un double-tap déjà traité (`attach_client_and_send_receipt`,
        vente déjà liée à CE client) : renvoie l'état déjà produit par le
        tout premier appel — `receipt_email` reflète la DERNIÈRE
        communication existante pour cette vente/ce client (`None` si le
        premier appel avait `send_receipt=False`) — sans rien rejouer."""
        from app.models.communication import Communication

        comm = (
            await self.db.execute(
                select(Communication)
                .where(
                    Communication.transaction_id == transaction.id,
                    Communication.client_id == client.id,
                )
                .order_by(Communication.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        receipt_result = (
            {"status": comm.status.value, "provider": comm.provider.value} if comm is not None else None
        )
        return {"client": client, "receipt_email": receipt_result, "brevo": None}

    async def resend_receipt_email(
        self,
        *,
        transaction: Transaction,
        email: str | None,
        user_id: uuid.UUID | None,
    ) -> dict:
        """Orchestre `POST /pos/transactions/{id}/receipt/email` (§4) :
        e-mail par défaut = celui du client lié, sinon `email` est requis
        (422 `email_required`). Incrémente `duplicate_count` et journalise
        `receipt.duplicate` (§4.4), en plus de `receipt.emailed`/
        `receipt.email_failed`."""
        from app.models.client import Client
        from app.services.client_service import normalize_email

        client = None
        if transaction.client_id is not None:
            client = (
                await self.db.execute(select(Client).where(Client.id == transaction.client_id))
            ).scalar_one_or_none()

        to = email or (client.email if client is not None else None)
        if not to:
            raise PosServiceError(
                "Adresse e-mail requise : aucun client lié à cette vente.",
                code="email_required",
                status_code=422,
            )
        to = normalize_email(to)

        result = await self._send_receipt_email(
            transaction=transaction,
            to=to,
            client_id=client.id if client is not None else None,
            user_id=user_id,
        )

        receipt = await self.get_receipt(transaction.id)
        if receipt is not None:
            receipt.duplicate_count += 1
            await JournalService(self.db).record(
                EVENT_RECEIPT_DUPLICATE,
                user_id=user_id,
                payload={"transaction_id": str(transaction.id), "duplicate_count": receipt.duplicate_count},
            )
            await self.db.flush()
        return result

    async def _send_receipt_email(
        self,
        *,
        transaction: Transaction,
        to: str,
        client_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
    ) -> dict:
        from app.models.communication import (
            Communication,
            CommunicationChannel,
            CommunicationKind,
            CommunicationProvider,
            CommunicationStatus,
        )
        from app.services.email_gateway import send_email
        from app.services.receipt_email import build_receipt_email

        shop = await SettingsService(self.db).get("shop")
        receipt = await self.get_receipt(transaction.id)
        # Rendu courant (PR7/I3) : si la vente a ete rattachee — ou
        # detachee — apres coup, l'e-mail porte l'etat d'aujourd'hui, pas
        # celui de l'instant de la vente.
        receipt_text = (
            await self.render_receipt_text(receipt, transaction=transaction)
            if receipt is not None
            else ReceiptService().generate(
                transaction, shop=shop, client_label=await self.client_label(transaction)
            )
        )
        dpo_email = shop.get("dpo_email") if isinstance(shop, dict) else None
        message = build_receipt_email(
            transaction, to=to, receipt_text=receipt_text, shop=shop, dpo_email=dpo_email
        )
        result = await send_email(message)

        provider_map = {
            "brevo": CommunicationProvider.brevo,
            "smtp": CommunicationProvider.smtp,
            "simulated": CommunicationProvider.simulated,
        }
        status_map = {
            "sent": CommunicationStatus.sent,
            "failed": CommunicationStatus.failed,
            "simulated": CommunicationStatus.simulated,
        }
        self.db.add(
            Communication(
                client_id=client_id,
                transaction_id=transaction.id,
                kind=CommunicationKind.receipt,
                channel=CommunicationChannel.email,
                recipient=to,
                subject=message.subject,
                provider=provider_map.get(result.provider, CommunicationProvider.simulated),
                status=status_map.get(result.status, CommunicationStatus.failed),
                provider_message_id=result.message_id,
                error=result.error,
            )
        )
        await self.db.flush()

        event = EVENT_RECEIPT_EMAIL_FAILED if result.status == "failed" else EVENT_RECEIPT_EMAILED
        await JournalService(self.db).record(
            event,
            user_id=user_id,
            payload={
                "transaction_number": transaction.transaction_number,
                "provider": result.provider,
                "status": result.status,
            },
        )
        await self.db.flush()
        return {"status": result.status, "provider": result.provider}
