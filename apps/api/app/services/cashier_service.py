# Nouveau service (PR8, docs/ARCHITECTURE_PR8.md, contrats J1/J2/J3) —
# vendeuses identifiees en caisse par un code PIN a 4 chiffres, avec releve
# en cours de journee.
#
# Une vendeuse n'est PAS un compte utilisateur : pas de login, pas de JWT,
# pas de role (le compte manager unique reste le seul compte de
# l'application, cf. CLAUDE.md). C'est une identite de caisse, portee par le
# tiroir ouvert (`cash_drawers.current_cashier_id`) et recopiee sur chaque
# vente, annulation, mouvement et cloture.
#
# Regles de securite appliquees ici :
#   - le PIN n'est jamais stocke en clair (bcrypt, `core/security`), jamais
#     renvoye par l'API, jamais journalise — le JET ne porte que
#     `cashier_id` ;
#   - 5 essais par vendeuse ET par IP sur 5 minutes glissantes
#     (`core/rate_limit.py`), compteur remis a zero sur une identification
#     reussie, exactement comme le login manager ;
#   - une vendeuse desactivee, ou sans PIN defini, ne peut pas s'identifier
#     et recoit la meme erreur qu'un mauvais code (`invalid_pin`) : l'ecran
#     de caisse n'a qu'un seul message a afficher, et rien ne distingue les
#     deux cas pour qui tape au hasard.
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import cashier_pin_rate_limit, reset_cashier_pin_rate_limit
from app.core.security import hash_password, verify_password
from app.models.cashier import Cashier
from app.models.pos import CashDrawer, Transaction, TransactionType, ZReport
from app.services.fiscal import PosServiceError
from app.services.jet import (
    EVENT_CASHIER_CREATED,
    EVENT_CASHIER_IDENTIFIED,
    EVENT_CASHIER_PIN_CHANGED,
    EVENT_CASHIER_PIN_REJECTED,
    EVENT_CASHIER_RELEASED,
    EVENT_CASHIER_UPDATED,
    JournalService,
)

# Libelle affiche pour les operations encaissees sans identification
# (reglage `pos.cashier_required` a false, ou historique anterieur a PR8).
UNIDENTIFIED_LABEL = "Non identifiée"


class CashierNotFound(PosServiceError):
    status_code = 404
    code = "not_found"

    def __init__(self, message: str = "Vendeuse introuvable."):
        super().__init__(message)


class CashierNameTaken(PosServiceError):
    status_code = 409
    code = "cashier_exists"

    def __init__(self, display_name: str):
        super().__init__(f"Une vendeuse s'appelle déjà « {display_name} ».")


class InvalidCashierName(PosServiceError):
    status_code = 422
    code = "invalid_cashier_name"


class WeakPin(PosServiceError):
    status_code = 422
    code = "weak_pin"


class InvalidPin(PosServiceError):
    status_code = 401
    code = "invalid_pin"

    def __init__(self, message: str = "Code incorrect."):
        super().__init__(message)


class PinRateLimited(PosServiceError):
    """Trop d'essais pour cette vendeuse depuis ce poste (J2).

    Porte `retry_after` (secondes) pour que l'ecran de caisse affiche un
    compte a rebours plutot qu'un message figé ; le routeur le recopie
    aussi dans l'en-tete `Retry-After`.
    """

    status_code = 429
    code = "pin_rate_limited"

    def __init__(self, retry_after: int):
        super().__init__(f"Trop de tentatives. Réessayez dans {retry_after} secondes.")
        self.retry_after = retry_after


class CashierRequired(PosServiceError):
    """Le reglage `pos.cashier_required` est actif et personne n'est
    identifie sur le tiroir ouvert (J2)."""

    status_code = 422
    code = "cashier_required"

    def __init__(
        self, message: str = "Identifiez-vous avec votre code avant d'encaisser."
    ):
        super().__init__(message)


# ---------------------------------------------------------------------------
# Code PIN
# ---------------------------------------------------------------------------

# Suites triviales refusees (J3). Les dix repetitions (`0000`…`9999`) sont
# calculees plutot qu'ecrites une a une ; la liste nommee ci-dessous couvre
# les suites croissantes/decroissantes et la croix du pave numerique, qui
# sont les premiers codes essayes par quelqu'un qui tente sa chance.
_WEAK_PINS = frozenset(
    {f"{digit}" * 4 for digit in range(10)}
    | {"1234", "4321", "0123", "9876", "2580"}
)


def validate_pin(raw: str | None) -> str:
    """Valide un code PIN et le retourne normalise (4 chiffres).

    Leve `WeakPin` (422 `weak_pin`) aussi bien sur un format invalide que
    sur une suite triviale : dans les deux cas, la reponse attendue par le
    manager est la meme — « choisissez un autre code ».
    """
    pin = (raw or "").strip()
    if len(pin) != 4 or not pin.isdigit():
        raise WeakPin("Le code doit comporter exactement 4 chiffres.")
    if pin in _WEAK_PINS:
        raise WeakPin("Code trop simple : évitez 0000, 1234, 2580… Choisissez autre chose.")
    return pin


def normalize_display_name(raw: str | None) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise InvalidCashierName("Le nom de la vendeuse est obligatoire.")
    if len(name) > 60:
        raise InvalidCashierName("Le nom de la vendeuse ne peut pas dépasser 60 caractères.")
    return name


def serialize_cashier(cashier: Cashier, *, admin: bool = False) -> dict:
    """Vue publique d'une vendeuse — JAMAIS `pin_hash`, seulement `has_pin`."""
    data = {"id": str(cashier.id), "display_name": cashier.display_name}
    if admin:
        data.update(
            {
                "has_pin": cashier.pin_hash is not None,
                "active": cashier.active,
                "created_at": cashier.created_at.isoformat() if cashier.created_at else None,
                "deactivated_at": (
                    cashier.deactivated_at.isoformat() if cashier.deactivated_at else None
                ),
            }
        )
    return data


class CashierService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------

    async def get(self, cashier_id: uuid.UUID) -> Cashier | None:
        return (
            await self.db.execute(select(Cashier).where(Cashier.id == cashier_id))
        ).scalar_one_or_none()

    async def get_or_404(self, cashier_id: uuid.UUID) -> Cashier:
        cashier = await self.get(cashier_id)
        if cashier is None:
            raise CashierNotFound()
        return cashier

    async def list(self, *, only_active: bool = False) -> list[Cashier]:
        """Vendeuses, triees par nom (insensible a la casse) — ordre stable
        d'un appel a l'autre, y compris sur l'ecran d'identification."""
        query = select(Cashier).order_by(func.lower(Cashier.display_name).asc())
        if only_active:
            query = query.where(Cashier.active.is_(True))
        return list((await self.db.execute(query)).scalars().all())

    # ------------------------------------------------------------------
    # Administration (J3)
    # ------------------------------------------------------------------

    async def create(
        self, *, display_name: str, pin: str | None, user_id: uuid.UUID | None
    ) -> Cashier:
        name = normalize_display_name(display_name)
        pin_hash = hash_password(validate_pin(pin)) if pin else None

        # Doublon detecte en amont pour rendre un message lisible, et
        # rattrape sur l'IntegrityError : l'index unique insensible a la
        # casse reste le seul arbitre en cas de course.
        if await self._name_taken(name):
            raise CashierNameTaken(name)

        cashier = Cashier(display_name=name, pin_hash=pin_hash, active=True)
        self.db.add(cashier)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise CashierNameTaken(name) from exc

        await JournalService(self.db).record(
            EVENT_CASHIER_CREATED,
            user_id=user_id,
            # Le nom de la vendeuse est une donnee d'exploitation (il figure
            # sur les tickets), pas une donnee personnelle de cliente : il a
            # sa place au JET. Le PIN et son hash, jamais.
            payload={"cashier_id": str(cashier.id), "has_pin": pin_hash is not None},
        )
        await self.db.flush()
        return cashier

    async def update(
        self,
        *,
        cashier: Cashier,
        display_name: str | None,
        active: bool | None,
        user_id: uuid.UUID | None,
    ) -> Cashier:
        changed: list[str] = []

        if display_name is not None:
            name = normalize_display_name(display_name)
            if name != cashier.display_name:
                if await self._name_taken(name, exclude_id=cashier.id):
                    raise CashierNameTaken(name)
                cashier.display_name = name
                changed.append("display_name")

        if active is not None and active != cashier.active:
            cashier.active = active
            # Une vendeuse desactivee ne peut plus s'identifier ; si elle
            # tenait la caisse, la vendeuse courante est retiree du tiroir
            # (sinon les ventes suivantes continueraient a lui etre
            # attribuees alors qu'elle ne peut plus se reconnecter).
            cashier.deactivated_at = datetime.now(timezone.utc) if not active else None
            if not active:
                await self._release_from_open_drawer(cashier.id)
            changed.append("active")

        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise CashierNameTaken(cashier.display_name) from exc

        await JournalService(self.db).record(
            EVENT_CASHIER_UPDATED,
            user_id=user_id,
            payload={
                "cashier_id": str(cashier.id),
                "changed_fields": sorted(changed),
                "active": cashier.active,
            },
        )
        await self.db.flush()
        return cashier

    async def set_pin(
        self, *, cashier: Cashier, pin: str, user_id: uuid.UUID | None
    ) -> Cashier:
        """Definit ou change le code d'une vendeuse (J3).

        Le JET porte `cashier.pin_changed` SANS le code ni son hash : un
        journal immuable ne doit jamais pouvoir restituer un secret.
        """
        cashier.pin_hash = hash_password(validate_pin(pin))
        await self.db.flush()
        await JournalService(self.db).record(
            EVENT_CASHIER_PIN_CHANGED,
            user_id=user_id,
            payload={"cashier_id": str(cashier.id)},
        )
        await self.db.flush()
        return cashier

    async def _name_taken(self, name: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        query = select(Cashier.id).where(func.lower(Cashier.display_name) == name.lower())
        if exclude_id is not None:
            query = query.where(Cashier.id != exclude_id)
        return (await self.db.execute(query.limit(1))).scalar_one_or_none() is not None

    # ------------------------------------------------------------------
    # Identification en caisse (J2)
    # ------------------------------------------------------------------

    async def identify(
        self,
        *,
        cashier_id: uuid.UUID,
        pin: str,
        ip: str | None,
        user_id: uuid.UUID | None = None,
    ) -> Cashier:
        """Identifie une vendeuse par son code et la pose sur le tiroir.

        L'appelant DOIT committer meme quand cette methode leve : les refus
        ecrivent `cashier.pin_rejected` au JET avant de lever, et un
        rollback effacerait l'evenement de securite (meme discipline que
        `/auth/login`, cf. `api/auth/router.py`).
        """
        cashier = await self.get(cashier_id)
        if cashier is None:
            raise CashierNotFound()

        # Le compteur est incremente AVANT la verification (comme le login
        # manager) : cinq essais, reussis ou non, puis blocage.
        try:
            await cashier_pin_rate_limit(str(cashier_id), ip)
        except HTTPException as exc:
            retry_after = int(exc.headers.get("Retry-After", "1")) if exc.headers else 1
            await self._record_rejection(cashier_id, "rate_limited", user_id=user_id)
            raise PinRateLimited(retry_after) from exc

        if not cashier.active or not cashier.pin_hash or not verify_password(pin, cashier.pin_hash):
            reason = (
                "inactive"
                if not cashier.active
                else ("no_pin" if not cashier.pin_hash else "invalid_pin")
            )
            await self._record_rejection(cashier_id, reason, user_id=user_id)
            raise InvalidPin()

        await reset_cashier_pin_rate_limit(str(cashier_id), ip)

        # Caisse ouverte : la vendeuse prend la caisse tout de suite. Caisse
        # fermee : l'identification est acceptee quand meme (elle precede
        # l'ouverture), le front la conserve et la renvoie dans le corps de
        # `POST /pos/drawer/open` — il n'y a alors aucun tiroir ou l'ecrire.
        drawer = (
            await self.db.execute(select(CashDrawer).where(CashDrawer.is_open.is_(True)).limit(1))
        ).scalar_one_or_none()
        if drawer is not None:
            drawer.current_cashier_id = cashier.id
            await self.db.flush()

        await JournalService(self.db).record(
            EVENT_CASHIER_IDENTIFIED,
            user_id=user_id,
            payload={"cashier_id": str(cashier.id)},
        )
        await self.db.flush()
        return cashier

    async def release(self, *, user_id: uuid.UUID | None = None) -> Cashier | None:
        """Releve : retire l'identite courante du tiroir ouvert (J2).

        Retourne la vendeuse qui vient d'etre relevee, ou ``None`` si
        personne n'etait identifie (l'operation reste un succes : le
        resultat voulu — plus personne en caisse — est atteint).
        """
        drawer = (
            await self.db.execute(select(CashDrawer).where(CashDrawer.is_open.is_(True)).limit(1))
        ).scalar_one_or_none()
        if drawer is None or drawer.current_cashier_id is None:
            return None

        cashier = await self.get(drawer.current_cashier_id)
        drawer.current_cashier_id = None
        await self.db.flush()
        await JournalService(self.db).record(
            EVENT_CASHIER_RELEASED,
            user_id=user_id,
            payload={"cashier_id": str(cashier.id) if cashier is not None else None},
        )
        await self.db.flush()
        return cashier

    async def current_for_drawer(self, drawer: CashDrawer | None) -> Cashier | None:
        if drawer is None or drawer.current_cashier_id is None:
            return None
        return await self.get(drawer.current_cashier_id)

    async def _release_from_open_drawer(self, cashier_id: uuid.UUID) -> None:
        drawer = (
            await self.db.execute(select(CashDrawer).where(CashDrawer.is_open.is_(True)).limit(1))
        ).scalar_one_or_none()
        if drawer is not None and drawer.current_cashier_id == cashier_id:
            drawer.current_cashier_id = None

    async def _record_rejection(
        self, cashier_id: uuid.UUID, reason: str, *, user_id: uuid.UUID | None
    ) -> None:
        """`cashier.pin_rejected` — jamais le code saisi, ni sa longueur, ni
        son hash : uniquement l'identifiant technique de la vendeuse et la
        nature du refus."""
        await JournalService(self.db).record(
            EVENT_CASHIER_PIN_REJECTED,
            user_id=user_id,
            payload={"cashier_id": str(cashier_id), "reason": reason},
        )
        await self.db.flush()


# ---------------------------------------------------------------------------
# Ventilation des ventes par vendeuse (J2 — JSON du Z et PDF du Z)
# ---------------------------------------------------------------------------


async def sales_by_cashier(
    db: AsyncSession, z_reports: list[ZReport]
) -> dict[str, list[dict]]:
    """Ventes ventilees par vendeuse, pour chaque Z passe en parametre.

    Retourne ``{z_report_id: [{cashier_id, display_name, sales_count,
    sales_total}, …]}``.

    Calcule A LA LECTURE plutot que scelle dans le Z : ajouter un champ au
    payload signe du Z serait une evolution fiscale majeure (bump de
    `FISCAL_SIGNATURE_VERSION`, cf. CLAUDE.md), pour une information qui se
    rededuit exactement des ventes de la periode — lesquelles sont
    immuables. Le resultat est donc stable dans le temps, sans toucher a la
    chaine de preuve.

    Une seule requete pour toute une page de Z (jointure par intervalle
    `opened_at`/`closed_at`), pour ne pas payer un aller-retour par ligne.
    Les ventes encaissees sans identification sont regroupees sous
    `cashier_id: null`.
    """
    if not z_reports:
        return {}

    rows = (
        await db.execute(
            select(
                ZReport.id,
                Transaction.cashier_id,
                Cashier.display_name,
                func.count(Transaction.id),
                func.coalesce(func.sum(Transaction.total_ttc), 0),
            )
            .join(
                Transaction,
                (Transaction.created_at >= ZReport.opened_at)
                & (Transaction.created_at <= ZReport.closed_at)
                & (Transaction.transaction_type == TransactionType.sale),
            )
            .outerjoin(Cashier, Cashier.id == Transaction.cashier_id)
            .where(ZReport.id.in_([z.id for z in z_reports]))
            .group_by(ZReport.id, Transaction.cashier_id, Cashier.display_name)
        )
    ).all()

    grouped: dict[str, list[dict]] = {str(z.id): [] for z in z_reports}
    for z_id, cashier_id, display_name, count, total in rows:
        grouped[str(z_id)].append(
            {
                "cashier_id": str(cashier_id) if cashier_id else None,
                "display_name": display_name or UNIDENTIFIED_LABEL,
                "sales_count": int(count or 0),
                "sales_total": float(Decimal(str(total or 0))),
            }
        )
    # Ordre deterministe (le PDF du Z doit rester octet pour octet
    # identique d'un rendu a l'autre) : par nom, puis par identifiant.
    for entries in grouped.values():
        entries.sort(key=lambda e: (e["display_name"].lower(), e["cashier_id"] or ""))
    return grouped


async def sales_by_cashier_for(db: AsyncSession, z_report: ZReport) -> list[dict]:
    """`sales_by_cashier` pour un seul Z."""
    return (await sales_by_cashier(db, [z_report])).get(str(z_report.id), [])
