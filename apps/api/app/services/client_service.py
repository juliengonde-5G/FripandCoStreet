# Nouveau service (PR3, docs/ARCHITECTURE_PR3.md §3) — upsert par e-mail,
# registre de consentement, rattachement d'une vente, anonymisation RGPD
# (E4 : jamais de suppression de ligne, jamais de modification d'une vente
# hors `client_id`). Perimetre reduit a un seul purpose (`newsletter`) : pas
# de fidelite, pas de profilage, pas de SMS (cf. CLAUDE.md).
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client, Consent, ConsentPurpose, ConsentSource
from app.models.communication import Communication
from app.models.pos import Transaction
from app.models.receipt import Receipt
from app.services.fiscal import PosServiceError
from app.services.jet import (
    EVENT_BREVO_SYNC_FAILED,
    EVENT_BREVO_SYNCED,
    EVENT_CLIENT_ANONYMIZED,
    EVENT_CLIENT_CREATED,
    EVENT_CLIENT_EXPORTED,
    EVENT_CLIENT_LINKED,
    EVENT_CLIENT_UPDATED,
    EVENT_CONSENT_GRANTED,
    EVENT_CONSENT_REVOKED,
    JournalService,
)
from app.version import CONSENT_POLICY_VERSION


class InvalidEmail(PosServiceError):
    status_code = 422
    code = "invalid_email"


class ClientAlreadyLinked(PosServiceError):
    status_code = 409
    code = "client_already_linked"

    def __init__(self):
        super().__init__("Cette vente est déjà liée à un autre client.")


def normalize_email(raw: str) -> str:
    """Normalise (minuscules, trim) et valide un e-mail (`email-validator`).

    Leve `InvalidEmail` (-> 422 `{"detail": "...", "code": "invalid_email"}`)
    plutot qu'une erreur de validation Pydantic generique, pour respecter le
    format d'erreur metier du contrat (§4/CLAUDE.md).
    """
    candidate = (raw or "").strip().lower()
    if not candidate:
        raise InvalidEmail("Adresse e-mail requise.")
    try:
        validated = validate_email(candidate, check_deliverability=False)
    except EmailNotValidError as exc:
        raise InvalidEmail(f"Adresse e-mail invalide : {exc}") from exc
    return validated.normalized.lower()


def _email_correlation_hash(email: str) -> str:
    """Empreinte de corrélation NON réversible (8 hex = 32 bits) pour les
    payloads JET (revue RGPD) : le journal des événements techniques est
    IMMUABLE (aucun UPDATE/DELETE, migration 0001) — il ne doit donc JAMAIS
    porter d'e-mail, prénom ou nom en clair, sous peine d'être impossible à
    effacer lors d'une anonymisation (E4/art. 17). Suffisant pour recouper
    « de quel événement s'agit-il » avec `client_id`, jamais pour retrouver
    l'adresse elle-même.
    """
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:8]


def mask_email(email: str) -> str:
    """Masque partiel `m***@exemple.fr` — helper commun (revue RGPD) utilisé
    par `anonymize` pour les destinataires déjà tracés dans `communications`
    (table mutable, contrairement au JET)."""
    local, sep, domain = (email or "").partition("@")
    if not sep or not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


class ClientService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------

    async def get_by_id(self, client_id: uuid.UUID) -> Client | None:
        return (
            await self.db.execute(select(Client).where(Client.id == client_id))
        ).scalar_one_or_none()

    async def get_by_email(self, email: str) -> Client | None:
        return (
            await self.db.execute(select(Client).where(Client.email == email))
        ).scalar_one_or_none()

    async def search(self, q: str | None, *, limit: int = 50) -> list[Client]:
        query = select(Client).order_by(Client.created_at.desc()).limit(limit)
        if q and q.strip():
            like = f"%{q.strip().lower()}%"
            query = query.where(
                Client.email.ilike(like)
                | Client.first_name.ilike(like)
                | Client.last_name.ilike(like)
            )
        return (await self.db.execute(query)).scalars().all()

    # ------------------------------------------------------------------
    # Upsert / consentement / rattachement (§3)
    # ------------------------------------------------------------------

    async def upsert_by_email(
        self,
        *,
        email: str,
        first_name: str | None,
        last_name: str | None,
        user_id: uuid.UUID | None,
    ) -> tuple[Client, bool]:
        """Cree ou met a jour un client par e-mail normalise.

        Un nom/prenom fourni remplace une valeur vide, jamais l'inverse
        (ne jamais effacer une donnee deja saisie faute de la re-saisir).
        """
        normalized = normalize_email(email)
        existing = await self.get_by_email(normalized)
        if existing is not None:
            changed = False
            if first_name and first_name.strip() and existing.first_name != first_name.strip():
                existing.first_name = first_name.strip()
                changed = True
            if last_name and last_name.strip() and existing.last_name != last_name.strip():
                existing.last_name = last_name.strip()
                changed = True
            if changed:
                await self.db.flush()
                await JournalService(self.db).record(
                    EVENT_CLIENT_UPDATED,
                    user_id=user_id,
                    payload={"client_id": str(existing.id)},
                )
                await self.db.flush()
            return existing, False

        client = Client(
            email=normalized,
            first_name=(first_name or "").strip() or None,
            last_name=(last_name or "").strip() or None,
            created_by_user_id=user_id,
        )
        self.db.add(client)
        await self.db.flush()
        await JournalService(self.db).record(
            EVENT_CLIENT_CREATED,
            user_id=user_id,
            payload={"client_id": str(client.id), "email_hash": _email_correlation_hash(normalized)},
        )
        await self.db.flush()
        return client, True

    async def record_consent(
        self,
        *,
        client: Client,
        purpose: ConsentPurpose,
        granted: bool,
        source: ConsentSource,
        user_id: uuid.UUID | None,
        note: str | None = None,
    ) -> Consent | None:
        """Ajoute une ligne append-only au registre + met a jour le cache.

        Idempotent pour la source `pos` (§3) : si l'etat courant est deja
        celui demande, aucune ligne n'est ecrite (evite de spammer le
        registre a chaque vente d'une cliente deja abonnee). Une action
        manuelle (`admin`) ou une reponse webhook/RGPD ecrit toujours une
        nouvelle ligne, meme redondante, pour la piste d'audit.
        """
        current = client.newsletter_optin if purpose == ConsentPurpose.newsletter else False
        if source == ConsentSource.pos and bool(current) == bool(granted):
            return None

        entry = Consent(
            client_id=client.id,
            purpose=purpose,
            granted=granted,
            source=source,
            policy_version=CONSENT_POLICY_VERSION,
            recorded_by_user_id=user_id,
            note=note,
        )
        self.db.add(entry)
        if purpose == ConsentPurpose.newsletter:
            client.newsletter_optin = granted
        await self.db.flush()
        await JournalService(self.db).record(
            EVENT_CONSENT_GRANTED if granted else EVENT_CONSENT_REVOKED,
            user_id=user_id,
            payload={
                "client_id": str(client.id),
                "purpose": purpose.value,
                "source": source.value,
            },
        )
        await self.db.flush()
        return entry

    async def sync_brevo(self, client: Client, *, user_id: uuid.UUID | None) -> dict:
        """Point UNIQUE de synchro Brevo Contacts (E1/E2, revue RGPD) :
        pousse le contact sur la liste dédiée si `newsletter_optin`, le
        retire sinon (`brevo_contacts.push_contact`/`remove_from_list`).
        Best-effort — n'est JAMAIS appelé pour un consentement de source
        `webhook` (Brevo a déjà fait le retrait de son côté, voir
        `brevo_contacts.apply_webhook_event`) ; appelé explicitement après
        un `record_consent` de source `pos` ou `admin` par les appelants
        (`PosService.attach_client_and_send_receipt`,
        `api/admin/router.py::add_client_consent`). Un échec est tracé sur
        `client.brevo_last_error` (mutable) et journalisé `brevo.sync_failed`
        — SANS reproduire le détail de la réponse Brevo dans le JET
        (immuable) : celui-ci peut échoïr l'adresse en clair.
        """
        from app.services import brevo_contacts

        if client.newsletter_optin:
            result = await brevo_contacts.push_contact(client)
        else:
            result = await brevo_contacts.remove_from_list(client.email)

        if result.ok:
            client.brevo_synced_at = datetime.now(timezone.utc)
            client.brevo_last_error = None
            await JournalService(self.db).record(
                EVENT_BREVO_SYNCED, user_id=user_id, payload={"client_id": str(client.id)}
            )
        else:
            client.brevo_last_error = result.detail
            await JournalService(self.db).record(
                EVENT_BREVO_SYNC_FAILED, user_id=user_id, payload={"client_id": str(client.id)}
            )
        await self.db.flush()
        return {"status": "ok" if result.ok else "failed"}

    async def link_transaction(
        self, *, transaction: Transaction, client: Client, user_id: uuid.UUID | None
    ) -> None:
        """Rattache une vente a un client (E3) — UPDATE `client_id` sur une
        transaction potentiellement deja signee, seule colonne que le
        trigger d'immuabilite laisse passer."""
        if transaction.client_id is not None and transaction.client_id != client.id:
            raise ClientAlreadyLinked()
        transaction.client_id = client.id
        await self.db.flush()
        await JournalService(self.db).record(
            EVENT_CLIENT_LINKED,
            user_id=user_id,
            payload={
                "client_id": str(client.id),
                "transaction_id": str(transaction.id),
                "transaction_number": transaction.transaction_number,
            },
        )
        await self.db.flush()

    # ------------------------------------------------------------------
    # Fiche complete / export RGPD / anonymisation (E4)
    # ------------------------------------------------------------------

    async def get_full(self, client: Client) -> dict:
        consents = (
            await self.db.execute(
                select(Consent)
                .where(Consent.client_id == client.id)
                .order_by(Consent.created_at.desc())
            )
        ).scalars().all()
        communications = (
            await self.db.execute(
                select(Communication)
                .where(Communication.client_id == client.id)
                .order_by(Communication.created_at.desc())
            )
        ).scalars().all()
        transactions = (
            await self.db.execute(
                select(Transaction)
                .where(Transaction.client_id == client.id)
                .order_by(Transaction.transaction_number.desc())
            )
        ).scalars().all()
        return {
            "client": _serialize_client(client),
            "consents": [_serialize_consent(c) for c in consents],
            "communications": [_serialize_communication(c) for c in communications],
            "transactions": [
                {
                    "id": str(t.id),
                    "transaction_number": t.transaction_number,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "total_ttc": float(t.total_ttc),
                }
                for t in transactions
            ],
        }

    async def anonymize(
        self, *, client: Client, user_id: uuid.UUID | None, reason: str
    ) -> Client:
        """RGPD art. 17 (E4) — anonymise la fiche, JAMAIS de suppression de
        ligne ni de modification d'une vente : `transactions.client_id`
        continue de pointer vers cette fiche, desormais videe de toute
        donnee personnelle."""
        if client.anonymized_at is not None:
            return client

        # Destinataires déjà tracés dans `communications` (table mutable,
        # contrairement au JET) — masqués, jamais supprimés (E7 : la preuve
        # d'envoi doit rester, seule l'adresse en clair disparaît).
        comms = (
            await self.db.execute(
                select(Communication).where(Communication.client_id == client.id)
            )
        ).scalars().all()
        for comm in comms:
            comm.recipient = mask_email(comm.recipient)

        client.email = f"supprime-{uuid.uuid4()}@anonyme.invalid"
        client.first_name = None
        client.last_name = None
        client.newsletter_optin = False
        client.brevo_synced_at = None
        client.brevo_last_error = None
        client.anonymized_at = datetime.now(timezone.utc)
        self.db.add(
            Consent(
                client_id=client.id,
                purpose=ConsentPurpose.newsletter,
                granted=False,
                source=ConsentSource.rgpd,
                policy_version=CONSENT_POLICY_VERSION,
                recorded_by_user_id=user_id,
                note=reason,
            )
        )
        await self.db.flush()
        await JournalService(self.db).record(
            EVENT_CLIENT_ANONYMIZED,
            user_id=user_id,
            payload={"client_id": str(client.id), "reason": reason},
        )
        await self.db.flush()
        return client

    async def export(self, client: Client, *, user_id: uuid.UUID | None) -> dict:
        """Export RGPD JSON portable (Art. 20) — fiche, consentements,
        communications, tickets (texte)."""
        data = await self.get_full(client)
        transactions = (
            await self.db.execute(
                select(Transaction).where(Transaction.client_id == client.id)
            )
        ).scalars().all()
        tickets = []
        for tx in transactions:
            receipt = (
                await self.db.execute(
                    select(Receipt).where(Receipt.transaction_id == tx.id)
                )
            ).scalar_one_or_none()
            if receipt is not None:
                tickets.append({"transaction_number": tx.transaction_number, "content": receipt.content})
        data["tickets"] = tickets
        data["exported_at"] = datetime.now(timezone.utc).isoformat()
        await JournalService(self.db).record(
            EVENT_CLIENT_EXPORTED,
            user_id=user_id,
            payload={"client_id": str(client.id)},
        )
        await self.db.flush()
        return data


def _serialize_client(client: Client) -> dict:
    return {
        "id": str(client.id),
        "email": client.email,
        "first_name": client.first_name,
        "last_name": client.last_name,
        "newsletter_optin": client.newsletter_optin,
        "created_at": client.created_at.isoformat() if client.created_at else None,
        "anonymized_at": client.anonymized_at.isoformat() if client.anonymized_at else None,
    }


def _serialize_consent(c: Consent) -> dict:
    return {
        "id": str(c.id),
        "purpose": c.purpose.value,
        "granted": c.granted,
        "source": c.source.value,
        "policy_version": c.policy_version,
        "note": c.note,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _serialize_communication(c: Communication) -> dict:
    return {
        "id": str(c.id),
        "transaction_id": str(c.transaction_id) if c.transaction_id else None,
        "kind": c.kind.value,
        "channel": c.channel.value,
        "recipient": c.recipient,
        "subject": c.subject,
        "provider": c.provider.value,
        "status": c.status.value,
        "error": c.error,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }
