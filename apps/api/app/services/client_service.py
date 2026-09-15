# Nouveau service (PR3, docs/ARCHITECTURE_PR3.md §3) — upsert par e-mail,
# registre de consentement, rattachement d'une vente, anonymisation RGPD
# (E4 : jamais de suppression de ligne, jamais de modification d'une vente
# hors `client_id`). Perimetre reduit a un seul purpose (`newsletter`) : pas
# de fidelite, pas de profilage, pas de SMS (cf. CLAUDE.md).
#
# PR7 (docs/ARCHITECTURE_PR7.md, I3) — le client saisi en caisse peut n'avoir
# qu'un telephone : normalisation du numero, recherche par chiffres,
# `create_or_get(email?, phone?)` sous verrou consultatif, masquage du
# numero, effacement par l'anonymisation, synchro Brevo sans objet sans
# e-mail.
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client, Consent, ConsentPurpose, ConsentSource
from app.models.communication import Communication
from app.models.pos import Transaction, TransactionType
from app.models.receipt import Receipt
from app.services.fiscal import PosServiceError
from app.services.jet import (
    EVENT_BREVO_SYNC_FAILED,
    EVENT_BREVO_SYNCED,
    EVENT_CLIENT_ANONYMIZED,
    EVENT_CLIENT_CREATED,
    EVENT_CLIENT_EXPORTED,
    EVENT_CLIENT_LINKED,
    EVENT_CLIENT_UNLINKED,
    EVENT_CLIENT_UPDATED,
    EVENT_CONSENT_GRANTED,
    EVENT_CONSENT_REVOKED,
    JournalService,
)
from app.version import CONSENT_POLICY_VERSION

logger = logging.getLogger("fripco")


class InvalidEmail(PosServiceError):
    status_code = 422
    code = "invalid_email"


class InvalidPhone(PosServiceError):
    status_code = 422
    code = "invalid_phone"


class ContactRequired(PosServiceError):
    """Aucun moyen de contact fourni (I3) — une fiche client exige au moins
    un e-mail OU un telephone (contrainte CHECK en base, migration 0007)."""

    status_code = 422
    code = "contact_required"

    def __init__(self, message: str = "Renseignez au moins un e-mail ou un téléphone."):
        super().__init__(message)


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


# Separateurs de saisie tolerés dans un numero : espaces (y compris
# insecables), points, tirets (y compris typographiques), barres obliques,
# parentheses. Tout le reste est refusé.
_PHONE_SEPARATORS = re.compile(r"[\s.\-\u00a0\u202f\u2010-\u2015/()]+")
_PHONE_ALLOWED = re.compile(r"^\+?\d+$")
_PHONE_MIN_DIGITS = 6
_PHONE_MAX_DIGITS = 15  # E.164


def normalize_phone(raw: str | None) -> str | None:
    """Normalise un numero de telephone vers UNE seule forme canonique.

    En boutique, la vendeuse tape toujours le numero a la francaise
    (`06 99 88 77 66`) alors que la cliente a pu le dicter en `+33` ou en
    `0033` : si la caisse stockait chaque saisie telle quelle, la meme
    personne existerait en trois fiches et ne serait retrouvee que par la
    forme exacte tapee le jour de sa creation. On canonise donc a
    l'ecriture :

    - `+…` (deja international, France ou etranger) : conserve tel quel ;
    - `0033X…` : le prefixe international compose devient `+33X…` ;
    - `0X…` a 10 chiffres avec `X != 0` (numero national francais) :
      `+33X…` ;
    - tout le reste (etranger compose sans indicatif) : les chiffres bruts.

    Espaces, points, tirets, parentheses et barres obliques sont retires
    avant tout traitement. Renvoie ``None`` pour une saisie vide (le
    telephone est facultatif tant qu'un e-mail est fourni), leve
    `InvalidPhone` (422 `invalid_phone`) pour une saisie non vide mais
    inexploitable : caracteres interdits, moins de 6 chiffres ou plus de 15
    (limite E.164).
    """
    candidate = _PHONE_SEPARATORS.sub("", (raw or "").strip())
    if not candidate:
        return None
    if not _PHONE_ALLOWED.match(candidate):
        raise InvalidPhone(
            "Numéro de téléphone invalide : chiffres uniquement, "
            "éventuellement précédés de « + »."
        )
    digits = candidate.lstrip("+")
    if not _PHONE_MIN_DIGITS <= len(digits) <= _PHONE_MAX_DIGITS:
        raise InvalidPhone(
            "Numéro de téléphone invalide : entre 6 et 15 chiffres attendus."
        )

    if candidate.startswith("+"):
        return candidate
    if digits.startswith("0033"):
        return "+33" + digits[4:]
    if len(digits) == 10 and digits.startswith("0") and digits[1] != "0":
        return "+33" + digits[1:]
    return digits


def phone_digits(phone: str | None) -> str:
    """Chiffres d'un numero stocke (le `+` en moins) — c'est sur eux que
    porte la recherche."""
    return (phone or "").lstrip("+")


def phone_search_digits(raw: str | None) -> str | None:
    """Chiffres significatifs d'une recherche par telephone.

    Renvoie ``None`` si la saisie n'est PAS un numero (elle contient autre
    chose que des chiffres, des separateurs et un `+` eventuel) —
    l'appelant retombe alors sur la seule recherche e-mail/nom/prenom.

    Sinon la saisie est reduite a ses chiffres, puis debarrassee de ce qui
    n'identifie pas la ligne : le prefixe international `0033`, puis
    l'indicatif `33`, puis le `0` national de tete. Il reste le numero
    « nu », cherche en sous-chaine dans les chiffres stockes — de sorte que
    `06 99 88`, `+33 6 99 88`, `0033 699 88` et `699 88` retrouvent tous la
    fiche `+33699887766`. Un numero etranger (`+41791234567`) se retrouve
    par ses propres chiffres (`41 79 12`, `79 123 45`…).
    """
    candidate = _PHONE_SEPARATORS.sub("", (raw or "").strip())
    if not candidate:
        return None
    digits = candidate.lstrip("+")
    if not digits.isdigit():
        return None
    if digits.startswith("0033"):
        digits = digits[4:]
    elif digits.startswith("33"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    return digits or None


def _correlation_hash(value: str) -> str:
    """Empreinte de corrélation NON réversible (8 hex = 32 bits) pour les
    payloads JET (revue RGPD) : le journal des événements techniques est
    IMMUABLE (aucun UPDATE/DELETE, migration 0001) — il ne doit donc JAMAIS
    porter d'e-mail, prénom ou nom en clair, sous peine d'être impossible à
    effacer lors d'une anonymisation (E4/art. 17). Suffisant pour recouper
    « de quel événement s'agit-il » avec `client_id`, jamais pour retrouver
    l'adresse (ou le numéro) elle-même.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def mask_email(email: str | None) -> str | None:
    """Masque partiel `m***@exemple.fr` — helper commun (revue RGPD) utilisé
    par `anonymize` pour les destinataires déjà tracés dans `communications`
    (table mutable, contrairement au JET), et par la recherche client en
    caisse (I3 : la vendeuse identifie sans lire l'adresse complète).

    ``None`` en entree (fiche sans e-mail depuis PR7/I3) -> ``None`` : il n'y
    a rien a masquer, et la caisse affiche simplement le telephone.
    """
    if email is None:
        return None
    local, sep, domain = email.partition("@")
    if not sep or not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


def mask_phone(phone: str | None) -> str | None:
    """Masque partiel d'un numéro : seuls les DEUX derniers chiffres restent
    lisibles (I3), le reste est remplacé par des `•`. `None` si la fiche n'a
    pas de téléphone."""
    if not phone:
        return None
    if len(phone) <= 2:
        return "••"
    return "•" * (len(phone) - 2) + phone[-2:]


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

    async def get_by_phone(self, phone: str) -> Client | None:
        return (
            await self.db.execute(select(Client).where(Client.phone == phone))
        ).scalar_one_or_none()

    async def search(
        self, q: str | None, *, limit: int = 50, include_anonymized: bool = True
    ) -> list[Client]:
        """Recherche par e-mail, prenom, nom — et par telephone (I3).

        Quand la saisie ne contient que des chiffres, des separateurs et un
        eventuel `+` (`phone_search_digits`), la recherche porte AUSSI sur
        `phone`, en `LIKE` sur les chiffres STOCKES (le `+` retire cote
        base) et sur les chiffres NUS de la saisie (prefixes `0033`, `33`
        et `0` national retires cote saisie). C'est ce qui permet a la
        vendeuse de taper le numero a la francaise — `06 99 88` — et de
        retrouver une fiche enregistree en `+33699887766`. La recherche par
        nom/e-mail est inchangee ; une saisie alphabetique ne declenche
        jamais de clause `phone` (le numero stocke n'a pas de lettres).
        """
        query = select(Client).order_by(Client.created_at.desc()).limit(limit)
        if not include_anonymized:
            query = query.where(Client.anonymized_at.is_(None))
        if q and q.strip():
            like = f"%{q.strip().lower()}%"
            condition = (
                Client.email.ilike(like)
                | Client.first_name.ilike(like)
                | Client.last_name.ilike(like)
            )
            digits = phone_search_digits(q)
            if digits:
                condition = condition | func.replace(Client.phone, "+", "").like(
                    f"%{digits}%"
                )
            query = query.where(condition)
        return (await self.db.execute(query)).scalars().all()

    async def visit_stats(self, client_ids: list[uuid.UUID]) -> dict[str, dict]:
        """Nombre de visites et date de la derniere visite, par client, en
        UNE seule requete agregee pour toute la page appelante (jamais une
        requete par fiche).

        « Visite » = une VENTE rattachee (`transaction_type = sale`) : une
        annulation n'est pas une visite et ne doit ni incrementer le
        compteur, ni avancer la date.
        """
        ids = {cid for cid in client_ids if cid is not None}
        if not ids:
            return {}
        rows = (
            await self.db.execute(
                select(
                    Transaction.client_id,
                    func.count(Transaction.id),
                    func.max(Transaction.created_at),
                )
                .where(
                    Transaction.client_id.in_(ids),
                    Transaction.transaction_type == TransactionType.sale,
                )
                .group_by(Transaction.client_id)
            )
        ).all()
        return {
            str(client_id): {
                "visits_count": int(count or 0),
                "last_visit_at": last.isoformat() if last else None,
            }
            for client_id, count, last in rows
        }

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
        """Cree ou met a jour un client par e-mail normalise (PR3).

        Conserve comme point d'entree du parcours « ticket par e-mail »
        (`attach_client_and_send_receipt`) : c'est desormais un cas
        particulier de `create_or_get` (I3), sans telephone. L'e-mail y
        reste OBLIGATOIRE — d'ou la validation explicite ci-dessous, qui
        garantit un 422 `invalid_email` (et non `contact_required`) sur une
        saisie vide, comme avant PR7.
        """
        normalize_email(email)
        return await self.create_or_get(
            email=email,
            phone=None,
            first_name=first_name,
            last_name=last_name,
            user_id=user_id,
        )

    async def create_or_get(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> tuple[Client, bool]:
        """Retrouve (par e-mail, sinon par telephone) ou cree une fiche
        client. Renvoie ``(client, created)``.

        Au moins un moyen de contact est exige (`ContactRequired` -> 422
        `contact_required`), en echo de la contrainte CHECK posee en base
        par la migration 0007. Un nom/prenom — ou le second moyen de
        contact — fourni complete une fiche existante ; rien n'est jamais
        efface faute d'avoir ete re-saisi.

        Revue robustesse (double-tap POS) : serialise par un verrou avisory
        Postgres scope sur le moyen de contact principal (l'e-mail
        normalise s'il y en a un, sinon le telephone normalise) AVANT toute
        lecture/ecriture (`_acquire_client_write_lock`) — deux appels
        concurrents pour la MEME cliente ne courent donc jamais sur
        l'INSERT de `clients` (index uniques partiels sur `email` et
        `phone`) ; le second attend que le premier ait commite (le verrou
        est tenu jusqu'au COMMIT de CETTE transaction SQL), puis retrouve
        la ligne deja creee au lieu d'en tenter une seconde. Filet
        supplementaire : si une course residuelle leve quand meme une
        violation d'unicite (chemin qui contournerait le verrou, ou course
        sur l'AUTRE moyen de contact que celui qui porte le verrou),
        l'INSERT est tente dans un SAVEPOINT (`db.begin_nested()`) —
        l'`IntegrityError` est rattrapee, le savepoint annule juste
        l'INSERT rate (pas toute la transaction), et la ligne est relue :
        Postgres bloque un INSERT concurrent tant que la premiere
        transaction n'a pas fini, donc si l'erreur survient c'est que
        l'autre a deja commite — la relecture la trouve forcement.
        """
        normalized_email = normalize_email(email) if (email or "").strip() else None
        normalized_phone = normalize_phone(phone)
        if not normalized_email and not normalized_phone:
            raise ContactRequired()

        await self._acquire_client_write_lock(normalized_email or normalized_phone)

        existing = await self._find_by_contact(normalized_email, normalized_phone)
        if existing is not None:
            await self._complete_existing(
                existing,
                email=normalized_email,
                phone=normalized_phone,
                first_name=first_name,
                last_name=last_name,
                user_id=user_id,
            )
            return existing, False

        client = Client(
            email=normalized_email,
            phone=normalized_phone,
            first_name=(first_name or "").strip() or None,
            last_name=(last_name or "").strip() or None,
            created_by_user_id=user_id,
        )
        try:
            async with self.db.begin_nested():
                self.db.add(client)
                await self.db.flush()
        except IntegrityError:
            self.db.expunge(client)
            existing = await self._find_by_contact(normalized_email, normalized_phone)
            if existing is None:
                raise
            return existing, False

        # JET immuable : jamais d'e-mail ni de numero en clair, seulement
        # une empreinte de correlation non reversible (cf. `_correlation_hash`).
        payload: dict = {"client_id": str(client.id)}
        if normalized_email:
            payload["email_hash"] = _correlation_hash(normalized_email)
        if normalized_phone:
            payload["phone_hash"] = _correlation_hash(normalized_phone)
        await JournalService(self.db).record(
            EVENT_CLIENT_CREATED, user_id=user_id, payload=payload
        )
        await self.db.flush()
        return client, True

    async def _find_by_contact(self, email: str | None, phone: str | None) -> Client | None:
        """Retrouve par e-mail d'abord (identifiant historique), sinon par
        telephone (I3)."""
        if email:
            found = await self.get_by_email(email)
            if found is not None:
                return found
        if phone:
            return await self.get_by_phone(phone)
        return None

    async def _complete_existing(
        self,
        client: Client,
        *,
        email: str | None,
        phone: str | None,
        first_name: str | None,
        last_name: str | None,
        user_id: uuid.UUID | None,
    ) -> None:
        """Complete une fiche retrouvee avec ce que la caisse vient de
        saisir — sans jamais ecraser par du vide, et sans jamais remplacer
        un moyen de contact deja renseigne par un autre (une adresse
        differente cree une autre fiche, elle ne remplace pas celle-ci).

        Un moyen de contact deja porte par une AUTRE fiche n'est pas
        recopie ici : ce serait une violation des index uniques partiels
        (0007) au flush, donc un 500 en pleine caisse. La saisie est
        ignoree en silence, les deux fiches restent distinctes, et le
        back-office tranche a froid.
        """
        changed = False
        if first_name and first_name.strip() and client.first_name != first_name.strip():
            client.first_name = first_name.strip()
            changed = True
        if last_name and last_name.strip() and client.last_name != last_name.strip():
            client.last_name = last_name.strip()
            changed = True
        if email and not client.email and (await self.get_by_email(email)) is None:
            client.email = email
            changed = True
        if phone and not client.phone and (await self.get_by_phone(phone)) is None:
            client.phone = phone
            changed = True
        if changed:
            await self.db.flush()
            await JournalService(self.db).record(
                EVENT_CLIENT_UPDATED,
                user_id=user_id,
                payload={"client_id": str(client.id)},
            )
            await self.db.flush()

    async def _acquire_client_write_lock(self, contact_key: str) -> None:
        """Verrou avisory Postgres scope par moyen de contact normalise
        (revue robustesse, double-tap POS) — tenu jusqu'au COMMIT de la
        transaction SQL en cours ; reentrant (un meme appelant peut
        l'acquerir plusieurs fois sans se bloquer lui-meme, cf. doc
        Postgres sur `pg_advisory_xact_lock`)."""
        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('client:' || :key))"),
            {"key": contact_key},
        )

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

        # I3 — une fiche « telephone seul » n'a rien a synchroniser : Brevo
        # est un carnet d'adresses e-mail. On ne tente pas l'appel, on ne
        # marque pas d'erreur sur la fiche, on trace simplement le
        # non-evenement dans le log serveur (jamais de PII : `client_id`).
        if not client.email:
            logger.info(
                "brevo: synchro ignoree (fiche sans e-mail) client_id=%s", client.id
            )
            return {"status": "skipped"}

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

    async def unlink_transaction(
        self, *, transaction: Transaction, user_id: uuid.UUID | None
    ) -> bool:
        """Detache une vente de sa fiche client (I3, `DELETE
        /pos/transactions/{id}/client`) — remise a NULL de `client_id`, la
        seule colonne mutable hors hash sur une transaction signee (E3) :
        ni les montants, ni les paiements, ni la signature ne bougent.

        Renvoie ``False`` (sans rien journaliser) si la vente n'etait
        rattachee a personne : l'operation est idempotente, un second clic
        sur « Detacher » n'ecrit pas un second evenement.
        """
        previous_client_id = transaction.client_id
        if previous_client_id is None:
            return False
        transaction.client_id = None
        await self.db.flush()
        await JournalService(self.db).record(
            EVENT_CLIENT_UNLINKED,
            user_id=user_id,
            payload={
                "client_id": str(previous_client_id),
                "transaction_id": str(transaction.id),
            },
        )
        await self.db.flush()
        return True

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
        # I3 — le telephone est une donnee personnelle au meme titre que
        # l'adresse : il est EFFACE (la contrainte CHECK reste satisfaite
        # par l'adresse `@anonyme.invalid` posee juste au-dessus).
        client.phone = None
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
        # I3 — la fiche admin (et l'export RGPD, qui la reutilise) expose le
        # telephone en clair : c'est la donnee de la cliente, elle doit lui
        # etre restituee entiere (art. 15/20).
        "phone": client.phone,
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
