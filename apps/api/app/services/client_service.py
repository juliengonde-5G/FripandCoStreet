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
#
# PR10 (docs/ARCHITECTURE_PR10.md, L2/L3) — la meme personne finit par avoir
# deux fiches : elle a donne son telephone un jour, son e-mail un autre, ou
# son nom a ete tape avec un accent en moins. D'ou la detection des doublons
# (`name_key`, `find_duplicate_candidates`, `list_duplicate_groups`) et la
# FUSION (`merge`), qui ne touche jamais une vente au sens fiscal : elle
# repointe `transactions.client_id`, seule colonne mutable hors hash (E3).
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.client import Client, Consent, ConsentPurpose, ConsentSource
from app.models.communication import Communication
from app.models.pos import Transaction, TransactionType
from app.models.receipt import Receipt
from app.services.csv_safety import neutralize_csv_row
from app.services.fiscal import PosServiceError
from app.services.jet import (
    EVENT_BREVO_SYNC_FAILED,
    EVENT_BREVO_SYNCED,
    EVENT_CLIENT_ANONYMIZED,
    EVENT_CLIENT_CREATED,
    EVENT_CLIENT_DELETION_CANCELLED,
    EVENT_CLIENT_DELETION_REQUESTED,
    EVENT_CLIENT_EXPORTED,
    EVENT_CLIENT_LINKED,
    EVENT_CLIENT_MERGED,
    EVENT_CLIENT_UNLINKED,
    EVENT_CLIENT_UPDATED,
    EVENT_CONSENT_GRANTED,
    EVENT_CONSENT_REVOKED,
    JournalService,
)
from app.services.receipt import apply_client_line, format_client_label
from app.services.rgpd_email import build_deletion_request_email
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


class MergeSameClient(PosServiceError):
    """PR10/L3 — fusionner une fiche avec elle-meme."""

    status_code = 409
    code = "same_client"

    def __init__(self):
        super().__init__("Choisissez deux fiches différentes.")


class MergeClientInactive(PosServiceError):
    """PR10/L3 — l'une des deux fiches est anonymisee (RGPD) ou a deja ete
    absorbee par une autre fusion : il n'y a plus rien a y fusionner, et la
    rattacher une seconde fois romprait la chaine des renvois."""

    status_code = 409
    code = "client_inactive"

    def __init__(self, message: str = "Cette fiche n'est plus active (anonymisée ou déjà fusionnée)."):
        super().__init__(message)


class MergeDeletionPending(PosServiceError):
    """PR10/L3 — la fiche CONSERVEE est programmee pour suppression. On
    refuse plutot que de deverser sur elle l'historique d'une autre fiche
    qui serait efface a la date d'effet : la vendeuse annule d'abord la
    suppression, puis fusionne."""

    status_code = 409
    code = "deletion_pending"

    def __init__(self):
        super().__init__(
            "La fiche conservée a une suppression programmée : annulez-la avant de fusionner."
        )


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


# PR10/L4 — l'historique d'achats est lu EN CAISSE, sur une tablette, par
# une vendeuse qui a une cliente devant elle : les montants partent en
# chaines a deux decimales (« 42.00 »), jamais en flottants. Un `float`
# arrondi a l'affichage finirait par montrer « 41,999999 » sur un ticket que
# la cliente a paye 42 € — et la caisse, elle, raisonne en `Decimal` de bout
# en bout (`Numeric(10, 2)`).
def format_amount(value) -> str:
    """Montant monetaire tel qu'il part dans une reponse JSON."""
    return f"{Decimal(str(value if value is not None else 0)):.2f}"


# Nombre de lignes d'articles detaillees par ticket dans l'historique CAISSE.
# Au-dela, la caisse annonce « … et N autres articles » a partir de
# `items_count` : l'ecran de caisse sert a reconnaitre un achat (« le manteau
# de la semaine derniere »), pas a reimprimer le ticket — le detail complet
# reste en back-office.
HISTORY_ITEMS_PREVIEW = 5


def mask_phone(phone: str | None) -> str | None:
    """Masque partiel d'un numéro : seuls les DEUX derniers chiffres restent
    lisibles (I3), le reste est remplacé par des `•`. `None` si la fiche n'a
    pas de téléphone."""
    if not phone:
        return None
    if len(phone) <= 2:
        return "••"
    return "•" * (len(phone) - 2) + phone[-2:]


# Separateurs internes a un nom, reduits a un espace simple : espaces (y
# compris insecables), tirets ordinaires et typographiques, apostrophes.
# « Anne-Marie », « Anne Marie » et « anne  marie » designent la meme
# personne aux yeux du dedoublonnage.
_NAME_SEPARATORS = re.compile(r"[\s\-\u00a0\u202f\u2010-\u2015']+")

# Nombre maximal de fiches parcourues par la detection de doublons. La
# comparaison se fait en Python (accents retires) et non en SQL : cela
# exigerait l'extension `unaccent`, donc une extension Postgres a installer
# sur le VPS pour un seul ecran de back-office. Une boutique mono-caisse
# n'atteindra pas ce plafond avant des annees ; au-dela, la detection
# resterait correcte sur les fiches parcourues, simplement partielle.
DUPLICATE_SCAN_LIMIT = 5000
# Au plus 5 suggestions a la vendeuse (L2) : au-dela, ce n'est plus une
# aide a la saisie, c'est une liste a trier en pleine file d'attente.
DUPLICATE_CANDIDATES_MAX = 5


def _normalize_name_part(value: str | None) -> str:
    """Minuscules, accents retires (NFKD), separateurs reduits a un espace."""
    decomposed = unicodedata.normalize("NFKD", (value or "").strip())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NAME_SEPARATORS.sub(" ", stripped).strip().lower()


def name_key(first_name: str | None, last_name: str | None) -> str | None:
    """Cle de comparaison d'identite (PR10/L2) — « Elodie DUPONT-Martin » et
    « elodie dupont martin » donnent la meme cle.

    ``None`` quand le NOM DE FAMILLE est vide : un prenom seul ne suffit pas
    a soupconner un doublon (deux « Sophie » differentes passent a la caisse
    dans la meme journee), et on ne veut surtout pas proposer a la vendeuse
    de fusionner deux clientes distinctes.
    """
    last = _normalize_name_part(last_name)
    if not last:
        return None
    first = _normalize_name_part(first_name)
    return f"{first} {last}".strip()


def phone_variants(digits: str) -> list[str]:
    """Toutes les ecritures sous lesquelles un meme numero a pu etre stocke.

    `normalize_phone` canonise depuis PR7, mais l'index unique partiel
    `uq_clients_phone_present` (0007) interdit deja deux fiches portant le
    MEME texte : un doublon par telephone n'existe donc que si les deux
    fiches ont ete stockees sous des ecritures differentes du meme numero
    (`+33699887766` d'un cote, `699887766` de l'autre pour un numero dicte
    sans indicatif). Comparer les chaines telles quelles ne trouverait rien ;
    on interroge donc la base sur toutes les ecritures possibles de la meme
    ligne — recherche exacte, donc indexable, contrairement a un `LIKE`.
    """
    return [digits, f"+{digits}", f"0{digits}", f"33{digits}", f"+33{digits}", f"0033{digits}"]


def _safe_normalize_email(raw: str | None) -> str | None:
    """`normalize_email` tolerant : une saisie invalide n'est pas une erreur
    ici, juste un critere inutilisable.

    La detection de doublons est appelee PENDANT la frappe (L7, debounce
    400 ms) : refuser la requete en 422 parce que l'adresse n'est pas encore
    finie ferait clignoter une erreur sous les doigts de la vendeuse.
    """
    if not (raw or "").strip():
        return None
    try:
        return normalize_email(raw)
    except InvalidEmail:
        return None


def _safe_normalize_phone(raw: str | None) -> str | None:
    """`normalize_phone` tolerant — meme raison que `_safe_normalize_email`."""
    try:
        return normalize_phone(raw)
    except InvalidPhone:
        return None


def serialize_duplicate_client(client: Client, stats: dict[str, dict] | None = None) -> dict:
    """Fiche telle qu'elle apparait dans un groupe de doublons.

    Coordonnees MASQUEES : reconnaitre la bonne fiche n'exige pas de lire
    l'adresse entiere, et cet ecran peut etre ouvert devant du public.
    `created_at` sert au front a preselectionner la fiche a conserver (la
    plus visitee, puis la plus ancienne).
    """
    row = (stats or {}).get(str(client.id), {})
    return {
        "id": str(client.id),
        "first_name": client.first_name,
        "last_name": client.last_name,
        "email_masked": mask_email(client.email),
        "phone_masked": mask_phone(client.phone),
        "visits_count": row.get("visits_count", 0),
        "last_visit_at": row.get("last_visit_at"),
        "created_at": client.created_at.isoformat() if client.created_at else None,
    }


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

    def _not_merged(self, query):
        """Filtre commun (PR10/L3) : une fiche ABSORBEE n'existe plus pour
        les lectures courantes. Elle n'est pas supprimee — ses ventes ont
        ete repointees, son URL repond encore pour rediriger vers la fiche
        conservee — mais elle ne doit jamais ressortir d'une recherche ni
        etre retrouvee par `create_or_get` : ce serait ressusciter le
        doublon qu'on vient tout juste de resorber."""
        return query.where(Client.merged_into_client_id.is_(None))

    async def get_by_email(self, email: str) -> Client | None:
        return (
            await self.db.execute(
                self._not_merged(select(Client).where(Client.email == email))
            )
        ).scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> Client | None:
        return (
            await self.db.execute(
                self._not_merged(select(Client).where(Client.phone == phone))
            )
        ).scalar_one_or_none()

    async def _contact_taken(self, *, email: str | None = None, phone: str | None = None) -> bool:
        """Ce moyen de contact est-il DEJA porte par une ligne `clients`,
        fiches absorbees comprises ?

        Distinct de `get_by_email`/`get_by_phone` a dessein : ces deux-la
        ignorent les fiches absorbees (c'est le comportement metier voulu),
        alors qu'ici on interroge l'index unique PARTIEL de la base (0007),
        qui, lui, ne connait pas cette nuance. Confondre les deux, c'est
        recopier un e-mail deja pris et se prendre une violation d'unicite
        au flush, en pleine caisse.
        """
        query = select(Client.id).limit(1)
        if email is not None:
            query = query.where(Client.email == email)
        elif phone is not None:
            query = query.where(Client.phone == phone)
        else:
            return False
        return (await self.db.execute(query)).first() is not None

    async def search(
        self,
        q: str | None,
        *,
        limit: int = 50,
        include_anonymized: bool = True,
        newsletter_only: bool = False,
        deletion_pending: bool = False,
    ) -> list[Client]:
        """Recherche par e-mail, prenom, nom — et par telephone (I3).

        PR11 (M4) — deux filtres ADDITIFS, cumulables avec la recherche :
        `newsletter_only` ne garde que les fiches abonnees, `deletion_pending`
        celles dont la suppression est programmee et pas encore executee
        (demande posee, fiche pas encore anonymisee). Ils se posent en SQL,
        jamais apres coup en memoire : sinon la limite ramenerait 50 fiches
        dont trois abonnees.

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
        query = self._not_merged(
            select(Client).order_by(Client.created_at.desc()).limit(limit)
        )
        if not include_anonymized:
            query = query.where(Client.anonymized_at.is_(None))
        if newsletter_only:
            query = query.where(Client.newsletter_optin.is_(True))
        if deletion_pending:
            query = query.where(
                Client.deletion_requested_at.is_not(None),
                Client.anonymized_at.is_(None),
            )
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
                # PR10/L3 — une fiche absorbee ne compte aucune visite : ses
                # ventes ont ete repointees vers la fiche conservee, et si
                # une ligne residuelle la referencait encore, elle serait
                # comptee deux fois a l'ecran.
                .join(Client, Client.id == Transaction.client_id)
                .where(
                    Transaction.client_id.in_(ids),
                    Transaction.transaction_type == TransactionType.sale,
                    Client.merged_into_client_id.is_(None),
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

    async def _refunded_sale_ids(self, sale_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        """Parmi les ventes passees, celles qu'une annulation reference.

        Une annulation est une transaction `refund` a part entiere qui
        pointe la vente d'origine (`original_transaction_id`) : rien n'est
        jamais modifie ni supprime sur la vente elle-meme (CLAUDE.md). Une
        SEULE requete pour toute la page — l'historique caisse ne doit pas
        declencher un aller-retour par ticket.
        """
        if not sale_ids:
            return set()
        refund = aliased(Transaction)
        rows = (
            await self.db.execute(
                select(refund.original_transaction_id).where(
                    refund.transaction_type == TransactionType.refund,
                    refund.original_transaction_id.in_(sale_ids),
                )
            )
        ).scalars().all()
        return {rid for rid in rows if rid is not None}

    async def history(self, client: Client, *, limit: int = 5) -> dict:
        """Historique d'achats d'une cliente, tel qu'il s'affiche EN CAISSE
        (PR10/L4) : « elle est deja venue 3 fois, dont la semaine derniere ».

        Ventes uniquement : une annulation n'est pas une visite, elle est
        signalee sur la vente concernee (`refunded`) et retiree du cumul
        `total_spent`. Les compteurs (`visits_count`, `last_visit_at`) et le
        cumul portent sur TOUT l'historique, pas seulement sur les `limit`
        derniers tickets affiches — d'ou l'agregat SQL separe plutot qu'une
        somme des lignes chargees.
        """
        limit = max(1, min(int(limit or 1), 20))
        stats = (await self.visit_stats([client.id])).get(str(client.id), {})

        sales = (
            await self.db.execute(
                select(Transaction)
                .where(
                    Transaction.client_id == client.id,
                    Transaction.transaction_type == TransactionType.sale,
                )
                .order_by(
                    Transaction.created_at.desc(),
                    Transaction.transaction_number.desc(),
                )
                .limit(limit)
            )
        ).scalars().all()
        refunded = await self._refunded_sale_ids([t.id for t in sales])

        # Cumul « hors annulations » : NOT EXISTS correle plutot que deux
        # listes chargees en memoire — la somme porte sur toutes les ventes
        # de la fiche, meme celles que l'ecran n'affiche pas.
        refund = aliased(Transaction)
        cancelled = (
            select(refund.id)
            .where(
                refund.transaction_type == TransactionType.refund,
                refund.original_transaction_id == Transaction.id,
            )
            .exists()
        )
        total_spent = (
            await self.db.execute(
                select(func.coalesce(func.sum(Transaction.total_ttc), 0)).where(
                    Transaction.client_id == client.id,
                    Transaction.transaction_type == TransactionType.sale,
                    ~cancelled,
                )
            )
        ).scalar_one()

        return {
            "client_id": str(client.id),
            "visits_count": stats.get("visits_count", 0),
            "last_visit_at": stats.get("last_visit_at"),
            "total_spent": format_amount(total_spent),
            "transactions": [
                {
                    "id": str(t.id),
                    "transaction_number": t.transaction_number,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "total_ttc": format_amount(t.total_ttc),
                    "items_count": len(t.items),
                    "items": _preview_items(t),
                    "refunded": t.id in refunded,
                }
                for t in sales
            ],
        }

    # ------------------------------------------------------------------
    # Doublons (PR10/L2)
    # ------------------------------------------------------------------

    def _active_clients(self):
        """Fiches EXPLOITABLES pour le dedoublonnage : ni anonymisees (il
        n'y reste rien a rapprocher), ni deja absorbees.

        Une fiche en attente de suppression reste candidate : la personne
        est toujours cliente jusqu'a la date d'effet, et la fusion annule sa
        demande (L3, operation 4).
        """
        return select(Client).where(
            Client.anonymized_at.is_(None),
            Client.merged_into_client_id.is_(None),
        )

    async def find_duplicate_candidates(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        exclude_id: uuid.UUID | None = None,
    ) -> list[dict]:
        """Fiches susceptibles d'etre la MEME personne que ce qu'on est en
        train de saisir (L2), dans l'ordre de certitude decroissante :
        meme e-mail, puis meme telephone, puis meme nom.

        Une fiche deja retenue sur un critere plus sur n'est pas reproposee
        sur un critere plus faible : c'est le premier motif rencontre qui
        est affiche a la vendeuse (« meme e-mail » est plus parlant que
        « meme nom » quand les deux sont vrais).
        """
        normalized_email = _safe_normalize_email(email)
        normalized_phone = _safe_normalize_phone(phone)
        key = name_key(first_name, last_name)

        found: dict[uuid.UUID, dict] = {}

        async def _keep(clients, reason: str) -> None:
            for candidate in clients:
                if exclude_id is not None and candidate.id == exclude_id:
                    continue
                if candidate.id in found:
                    continue
                if len(found) >= DUPLICATE_CANDIDATES_MAX:
                    return
                found[candidate.id] = {"client": candidate, "reason": reason}

        if normalized_email:
            rows = (
                await self.db.execute(
                    self._active_clients()
                    .where(Client.email == normalized_email)
                    .limit(DUPLICATE_CANDIDATES_MAX)
                )
            ).scalars().all()
            await _keep(rows, "email")

        phone_key = phone_search_digits(normalized_phone)
        if phone_key and len(found) < DUPLICATE_CANDIDATES_MAX:
            rows = (
                await self.db.execute(
                    self._active_clients()
                    .where(Client.phone.in_(phone_variants(phone_key)))
                    .limit(DUPLICATE_CANDIDATES_MAX)
                )
            ).scalars().all()
            await _keep(rows, "phone")

        if key and len(found) < DUPLICATE_CANDIDATES_MAX:
            # Comparaison en Python (accents retires) : voir
            # `DUPLICATE_SCAN_LIMIT` pour le choix de ne pas dependre de
            # l'extension Postgres `unaccent`.
            rows = (
                await self.db.execute(
                    self._active_clients()
                    .where(Client.last_name.is_not(None))
                    .order_by(Client.created_at.desc())
                    .limit(DUPLICATE_SCAN_LIMIT)
                )
            ).scalars().all()
            await _keep(
                [c for c in rows if name_key(c.first_name, c.last_name) == key],
                "name",
            )

        return list(found.values())

    async def list_duplicate_groups(self, limit: int = 50) -> list[dict]:
        """Groupes de fiches qui semblent designer la meme personne (L2),
        pour la carte « Doublons possibles » du back-office.

        Un groupe = au moins deux fiches partageant un e-mail, un telephone
        ou une cle de nom. Un meme ensemble de fiches n'est annonce qu'UNE
        fois, sur le motif le plus sur : deux fiches qui partagent a la fois
        l'e-mail et le nom forment un seul doublon, pas deux.
        """
        clients = (
            await self.db.execute(
                self._active_clients()
                .order_by(Client.created_at.asc())
                .limit(DUPLICATE_SCAN_LIMIT)
            )
        ).scalars().all()

        buckets: dict[str, dict[str, list[Client]]] = {
            "email": {},
            "phone": {},
            "name": {},
        }
        for candidate in clients:
            if candidate.email:
                buckets["email"].setdefault(candidate.email, []).append(candidate)
            # Regroupement sur les chiffres SIGNIFICATIFS du numero (voir
            # `phone_variants`) : deux fiches portant la meme chaine ne
            # peuvent pas coexister, seules deux ecritures differentes du
            # meme numero le peuvent.
            key_phone = phone_search_digits(candidate.phone)
            if key_phone:
                buckets["phone"].setdefault(key_phone, []).append(candidate)
            key = name_key(candidate.first_name, candidate.last_name)
            if key:
                buckets["name"].setdefault(key, []).append(candidate)

        groups: list[tuple[str, list[Client]]] = []
        already_seen: set[frozenset[uuid.UUID]] = set()
        for reason in ("email", "phone", "name"):
            for members in buckets[reason].values():
                if len(members) < 2:
                    continue
                signature = frozenset(c.id for c in members)
                if signature in already_seen:
                    continue
                already_seen.add(signature)
                groups.append((reason, members))
                if len(groups) >= limit:
                    break
            if len(groups) >= limit:
                break

        # UNE seule requete de statistiques pour toute la page (jamais une
        # par fiche) — le back-office affiche le nombre de visites de chaque
        # fiche pour aider a choisir laquelle conserver.
        stats = await self.visit_stats([c.id for _, members in groups for c in members])
        return [
            {
                "reason": reason,
                "clients": [serialize_duplicate_client(c, stats) for c in members],
            }
            for reason, members in groups
        ]

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

        Le NOM suit exactement la meme regle que les coordonnees : il n'est
        ecrit que s'il manque. Une fiche est retrouvee par son e-mail ou son
        telephone ; si la vendeuse tape un autre nom sur ce meme numero,
        c'est presque toujours une confusion — un conjoint, un proche a qui
        on a redonne le numero de la maison, une ligne de la boutique. La
        version precedente renommait silencieusement « Marie DUPONT » en
        « Sophie Martin » et la cliente d'origine devenait introuvable par
        son nom, sans que personne ne l'ait demande. Corriger un nom reste
        possible la ou c'est un acte deliberé : la fiche du back-office.
        """
        changed = False
        if first_name and first_name.strip() and not client.first_name:
            client.first_name = first_name.strip()
            changed = True
        if last_name and last_name.strip() and not client.last_name:
            client.last_name = last_name.strip()
            changed = True
        if email and not client.email and not await self._contact_taken(email=email):
            client.email = email
            changed = True
        if phone and not client.phone and not await self._contact_taken(phone=phone):
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
        # PR10/L4 — la fiche back-office montre desormais CE QUI a ete
        # achete, pas seulement le total : c'est ce qui permet de reconnaitre
        # une cliente au telephone. Contrairement a l'historique caisse, le
        # detail n'est pas tronque a cinq lignes (l'ecran n'est pas une
        # tablette de comptoir) et les annulations sont signalees.
        refunded = await self._refunded_sale_ids(
            [t.id for t in transactions if t.transaction_type == TransactionType.sale]
        )
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
                    "items": [_serialize_item(i) for i in _ordered_items(t)],
                    "refunded": t.id in refunded,
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
        donnee personnelle.

        Les FACTURES professionnelles (PR8/J5) sont hors perimetre et ne
        sont donc pas touchees ici : une raison sociale et un SIRET ne sont
        pas des donnees personnelles d'une personne physique, la facture est
        une piece comptable a conserver 10 ans, et elle est de toute facon
        immuable en base (trigger `fripco_protect_invoice`). Le client
        professionnel n'a jamais de fiche `clients` : rien ne relie les deux
        objets."""
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
        # PR10/L5 — une suppression immediate SOLDE une demande differee en
        # cours : la fiche vient d'etre videe, il n'y a plus rien a
        # supprimer a echeance. Sans cet effacement, le cron quotidien
        # reverrait la fiche chaque nuit (il filtre certes sur
        # `anonymized_at IS NULL`, mais le front afficherait encore un
        # badge « Suppression programmee » sur une fiche deja anonyme).
        # Vaut aussi pour l'anonymisation faite PAR le cron a echeance.
        self._clear_deletion_request(client)
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

    # ------------------------------------------------------------------
    # Suppression RGPD differee (PR10/L5)
    # ------------------------------------------------------------------

    @staticmethod
    def _clear_deletion_request(client: Client) -> None:
        """Remet la fiche a l'etat « aucune suppression en cours ».

        Les trois champs bougent ensemble : une date d'effet sans
        demandeur, ou l'inverse, serait un etat qu'aucun ecran ne sait
        afficher — et que le cron interpreterait de travers.
        """
        client.deletion_requested_at = None
        client.deletion_scheduled_for = None
        client.deletion_requested_by_user_id = None

    async def _lock_deletion_row(self, client: Client) -> None:
        """Verrouille la ligne `clients` (SELECT … FOR UPDATE) puis relit
        la fiche.

        Les deux gestes du manager (programmer / annuler) et le cron de
        04:00 touchent les MEMES trois colonnes. Sans verrou, une
        annulation posee pendant que le cron travaille peut repondre 200
        au manager et voir la fiche videe la seconde d'apres. Le verrou
        les serialise, et la relecture garantit qu'on decide sur l'etat
        reellement commite, pas sur l'objet charge avant l'attente.
        """
        await self.db.execute(
            select(Client.id).where(Client.id == client.id).with_for_update()
        )
        await self.db.refresh(client)

    async def request_deletion(
        self, *, client: Client, user_id: uuid.UUID | None
    ) -> Client:
        """Programme l'effacement de la fiche (PR10/L5).

        Le RGPD donne un droit a l'effacement, pas un droit a l'effacement
        INSTANTANE : un differe de 30 jours (reglage
        `rgpd.deletion_delay_days`) laisse a la cliente le temps de se
        raviser — « finalement je garde ma fiche » — et a la boutique celui
        de constater qu'elle n'efface pas une fiche par erreur de clic. La
        fiche reste PLEINEMENT utilisable en caisse jusqu'a la date
        d'effet : la personne est toujours cliente.

        Refus 409 : `already_requested` (une demande court deja — il faut
        l'annuler pour en poser une autre) et `client_inactive` (fiche deja
        anonymisee, ou absorbee par une fusion : il n'y a plus rien a
        supprimer ici).
        """
        await self._lock_deletion_row(client)
        if client.anonymized_at is not None or client.merged_into_client_id is not None:
            raise PosServiceError(
                "Cette fiche n'est plus active : rien à supprimer.",
                code="client_inactive",
                status_code=409,
            )
        if client.deletion_requested_at is not None:
            raise PosServiceError(
                "Une suppression est déjà programmée pour cette fiche.",
                code="already_requested",
                status_code=409,
            )

        from app.services.settings_service import SettingsService

        delay_days = await SettingsService(self.db).get_deletion_delay_days()
        now = datetime.now(timezone.utc)
        scheduled_for = now + timedelta(days=delay_days)
        client.deletion_requested_at = now
        client.deletion_scheduled_for = scheduled_for
        client.deletion_requested_by_user_id = user_id
        await self.db.flush()

        # JET : jamais avale (CLAUDE.md). La date d'effet n'est pas une
        # donnee personnelle ; le nom et l'adresse, eux, n'y figurent pas.
        await JournalService(self.db).record(
            EVENT_CLIENT_DELETION_REQUESTED,
            user_id=user_id,
            payload={
                "client_id": str(client.id),
                "scheduled_for": scheduled_for.isoformat(),
            },
        )
        await self.db.flush()

        # Information de la cliente — best-effort : une panne de la
        # passerelle e-mail ne doit pas empecher d'enregistrer la demande
        # (ce serait lui refuser son droit pour une raison technique).
        await self._notify_deletion_requested(client, scheduled_for)
        return client

    async def cancel_deletion(
        self, *, client: Client, user_id: uuid.UUID | None
    ) -> Client:
        """Annule une suppression programmee (PR10/L5) — 409 `not_requested`
        s'il n'y en avait pas. C'est le geste qui donne son sens au differe :
        tant que la date d'effet n'est pas atteinte, tout est reversible.

        Verrouille la ligne avant de decider : si le cron de 04:00 est en
        train de solder cette fiche, on attend son issue plutot que de
        repondre « annulée » a un manager dont la fiche vient d'etre
        videe (la relecture voit alors `anonymized_at`, et il n'y a plus
        de demande a annuler -> 409)."""
        await self._lock_deletion_row(client)
        if client.deletion_requested_at is None:
            raise PosServiceError(
                "Aucune suppression n'est programmée pour cette fiche.",
                code="not_requested",
                status_code=409,
            )
        self._clear_deletion_request(client)
        await self.db.flush()
        await JournalService(self.db).record(
            EVENT_CLIENT_DELETION_CANCELLED,
            user_id=user_id,
            payload={"client_id": str(client.id)},
        )
        await self.db.flush()
        return client

    async def _notify_deletion_requested(
        self, client: Client, scheduled_for: datetime
    ) -> None:
        """« Votre demande de suppression est enregistrée » — accuse de
        reception envoye a la cliente, trace dans `communications`.

        Sans e-mail sur la fiche (PR7/I3 : une cliente peut n'avoir qu'un
        numero), il n'y a rien a envoyer : la vendeuse l'a informee au
        comptoir, la demande est enregistree, on s'arrete la — pas de SMS
        (CLAUDE.md).

        L'envoi ET l'ecriture de la ligne `Communication` sont
        best-effort et enveloppes ensemble : la demande, elle, est deja
        enregistree et journalisee au JET.
        """
        if not client.email:
            return
        try:
            from app.models.communication import (
                Communication,
                CommunicationChannel,
                CommunicationKind,
                CommunicationProvider,
                CommunicationStatus,
            )
            from app.services.email_gateway import send_email
            from app.services.settings_service import SettingsService

            shop = await SettingsService(self.db).get("shop")
            message = build_deletion_request_email(
                to=client.email,
                scheduled_for=scheduled_for,
                shop=shop,
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
                    client_id=client.id,
                    transaction_id=None,
                    kind=CommunicationKind.rgpd,
                    channel=CommunicationChannel.email,
                    recipient=client.email,
                    subject=message.subject,
                    provider=provider_map.get(
                        result.provider, CommunicationProvider.simulated
                    ),
                    status=status_map.get(result.status, CommunicationStatus.failed),
                    provider_message_id=result.message_id,
                    error=result.error,
                )
            )
            await self.db.flush()
        except Exception:  # noqa: BLE001 — cf. docstring
            logger.exception(
                "Accusé de réception de suppression non envoyé (client_id=%s)", client.id
            )

    # ------------------------------------------------------------------
    # Fusion de deux fiches en double (PR10/L3)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_merge_allowed(winner: Client, source: Client) -> None:
        """Conditions d'une fusion (L3). Extrait en methode parce qu'il est
        joue DEUX fois : une premiere pour refuser au plus tot, sans prendre
        de verrou, et une seconde sur les valeurs relues sous verrou — c'est
        cette seconde qui fait foi."""
        if winner.id == source.id:
            raise MergeSameClient()
        for candidate in (winner, source):
            if candidate.anonymized_at is not None or candidate.merged_into_client_id is not None:
                raise MergeClientInactive()
        if winner.deletion_requested_at is not None or winner.deletion_scheduled_for is not None:
            raise MergeDeletionPending()

    async def merge(
        self, *, winner: Client, source: Client, user_id: uuid.UUID | None
    ) -> dict:
        """Fusionne la fiche `source` DANS la fiche `winner`.

        Aucune vente n'est modifiee au sens fiscal ni supprimee : seule
        `transactions.client_id` bouge, la colonne que le trigger
        d'inaltérabilité laisse passer et qui n'entre pas dans le payload
        signe (E3/PR3). La chaine HMAC des ventes est donc rigoureusement
        identique avant et apres la fusion — c'est ce qui rend l'operation
        possible sans evolution fiscale.

        La fiche absorbee n'est jamais supprimee : elle est videe de ses
        donnees personnelles (meme mecanique que l'anonymisation) et garde
        un renvoi vers la fiche conservee, pour que l'ancienne URL et les
        anciens exports continuent de mener quelque part.
        """
        self._check_merge_allowed(winner, source)

        # Meme verrou que `create_or_get`, pris sur les DEUX moyens de
        # contact et dans un ordre deterministe (tri) : une vente en caisse
        # qui retrouverait la fiche source pendant la fusion attend la fin
        # de l'operation, et deux fusions concurrentes ne peuvent pas
        # s'inter-bloquer en prenant les memes verrous en sens inverse.
        for key in sorted(
            {
                winner.email or winner.phone or str(winner.id),
                source.email or source.phone or str(source.id),
            }
        ):
            await self._acquire_client_write_lock(key)

        # RELECTURE SOUS VERROU, puis memes controles sur les valeurs
        # fraiches (revue de code).
        #
        # Les deux fiches ont ete chargees par la route AVANT le verrou :
        # entre ce chargement et ici, une autre requete a pu fusionner la
        # meme source vers une AUTRE conservee et commiter. Poursuivre avec
        # les objets en memoire, valides sur un etat perime, ecraserait son
        # `merged_into_client_id` et recopierait ses coordonnees sur la
        # mauvaise fiche, pendant que ses ventes resteraient rattachees a la
        # premiere. Les controles ne valent donc que rejoues ici.
        #
        # `FOR UPDATE` verrouille les deux lignes par id croissant (ordre
        # deterministe, donc pas d'interblocage entre deux fusions croisees)
        # et couvre les ecritures qui, elles, ne passent pas par le verrou
        # consultatif — une anonymisation RGPD concurrente, par exemple.
        # `populate_existing` force la relecture des colonnes : sans lui,
        # SQLAlchemy rendrait les instances deja en memoire, c'est-a-dire
        # exactement les valeurs perimees qu'on cherche a ecarter.
        reloaded = {
            row.id: row
            for row in (
                await self.db.execute(
                    select(Client)
                    .where(Client.id.in_([winner.id, source.id]))
                    .order_by(Client.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars().all()
        }
        winner = reloaded.get(winner.id)
        source = reloaded.get(source.id)
        if winner is None or source is None:
            # Une ligne `clients` n'est jamais supprimee (RGPD =
            # anonymisation) : si elle a disparu, on ne fusionne rien.
            raise MergeClientInactive()
        self._check_merge_allowed(winner, source)

        # L'e-mail de la source doit etre capture AVANT qu'elle ne soit
        # videe : c'est lui qu'il faudra retirer de la liste Brevo.
        source_email = source.email

        # (1) Les ventes et les annulations changent de fiche. UPDATE
        # ensembliste (pas de boucle par ligne) : c'est exactement le
        # rattachement de PR3, applique en masse.
        moved_transactions = (
            await self.db.execute(
                update(Transaction)
                .where(Transaction.client_id == source.id)
                .values(client_id=winner.id)
                .execution_options(synchronize_session=False)
            )
        ).rowcount

        # (2) Le registre de consentement et les messages suivent. Le
        # registre reste INTEGRALEMENT conserve : on ne reecrit aucune ligne
        # (le trigger `trg_protect_consent` ne laisse passer que le
        # rattachement, cf. migration 0010), on constate que les deux fiches
        # n'en faisaient qu'une. L'etat courant reste « derniere ligne par
        # finalite », donc la plus recente des deux fiches l'emporte.
        moved_consents = (
            await self.db.execute(
                update(Consent)
                .where(Consent.client_id == source.id)
                .values(client_id=winner.id)
                .execution_options(synchronize_session=False)
            )
        ).rowcount
        moved_communications = (
            await self.db.execute(
                update(Communication)
                .where(Communication.client_id == source.id)
                .values(client_id=winner.id)
                .execution_options(synchronize_session=False)
            )
        ).rowcount

        # (3) Ce que la fiche conservee va recuperer : uniquement ce qui lui
        # MANQUE. Jamais d'ecrasement — la vendeuse a designe cette fiche-la
        # comme la bonne, ses valeurs font foi.
        carried = {
            field: getattr(source, field)
            for field in ("email", "phone", "first_name", "last_name")
            if not getattr(winner, field) and getattr(source, field)
        }
        # Le cache d'opt-in est la reunion des deux : une personne qui s'est
        # abonnee sous l'une de ses deux fiches reste abonnee. Le registre
        # `consents`, lui, garde la trace exacte de chaque decision.
        newsletter_optin = bool(winner.newsletter_optin or source.newsletter_optin)

        # (4) La fiche absorbee est videe puis marquee. L'ecriture est
        # poussee en base AVANT de recopier les coordonnees sur la fiche
        # conservee : les index uniques PARTIELS de `clients` (0007) sont
        # verifies a chaque instruction, l'e-mail doit donc etre libere
        # avant d'etre repris.
        source.email = f"supprime-{uuid.uuid4()}@anonyme.invalid"
        source.phone = None
        source.first_name = None
        source.last_name = None
        source.newsletter_optin = False
        source.brevo_synced_at = None
        source.brevo_last_error = None
        source.anonymized_at = datetime.now(timezone.utc)
        # Une suppression programmee sur la fiche ABSORBEE n'a plus d'objet :
        # ses donnees personnelles viennent d'etre effacees a l'instant.
        source.deletion_requested_at = None
        source.deletion_scheduled_for = None
        source.deletion_requested_by_user_id = None
        source.merged_into_client_id = winner.id
        source.merged_at = datetime.now(timezone.utc)
        await self.db.flush()

        for field, value in carried.items():
            setattr(winner, field, value)
        winner.newsletter_optin = newsletter_optin
        await self.db.flush()

        # (5) Brevo — la fiche source ne doit plus recevoir de campagne. On
        # la retire de la liste dediee (JAMAIS `DELETE /v3/contacts` ni la
        # blocklist globale : le compte Brevo est partage, cf. CLAUDE.md),
        # puis on synchronise la fiche conservee, qui a pu recuperer un
        # e-mail ou un opt-in. Meilleur effort de bout en bout : une panne
        # Brevo ne doit pas faire echouer une fusion deja ecrite en base.
        from app.services import brevo_contacts

        if brevo_contacts.is_configured():
            if source_email:
                try:
                    result = await brevo_contacts.remove_from_list(source_email)
                    if not result.ok:
                        logger.warning(
                            "brevo: retrait du contact absorbe impossible client_id=%s (%s)",
                            source.id,
                            result.detail,
                        )
                except Exception:  # noqa: BLE001 — jamais bloquant
                    logger.warning(
                        "brevo: retrait du contact absorbe en erreur client_id=%s",
                        source.id,
                        exc_info=True,
                    )
            await self.sync_brevo(winner, user_id=user_id)

        # (6) JET — identifiants et compteurs seulement (journal immuable).
        await JournalService(self.db).record(
            EVENT_CLIENT_MERGED,
            user_id=user_id,
            payload={
                "winner_id": str(winner.id),
                "source_id": str(source.id),
                "transactions_moved": moved_transactions,
                "consents_moved": moved_consents,
                "communications_moved": moved_communications,
            },
        )
        await self.db.flush()
        return {
            "client": _serialize_client(winner),
            "moved": {
                "transactions": moved_transactions,
                "consents": moved_consents,
                "communications": moved_communications,
            },
        }

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
                # Export RGPD : le ticket est rendu avec la ligne
                # « Client : … » telle qu'elle est AUJOURD'HUI (PR7/I3), la
                # vente exportee etant par construction rattachee a cette
                # cliente. Le contenu stocke, lui, reste fige (immuable en
                # base) et c'est lui qui part dans l'archive fiscale.
                tickets.append(
                    {
                        "transaction_number": tx.transaction_number,
                        "content": apply_client_line(
                            receipt.content,
                            format_client_label(client.first_name, client.last_name),
                        ),
                    }
                )
        data["tickets"] = tickets
        data["exported_at"] = datetime.now(timezone.utc).isoformat()
        await JournalService(self.db).record(
            EVENT_CLIENT_EXPORTED,
            user_id=user_id,
            payload={"client_id": str(client.id)},
        )
        await self.db.flush()
        return data


def _ordered_items(transaction: Transaction) -> list:
    """Lignes d'un ticket dans l'ordre d'impression (`position`) — la
    relation est chargee en `selectin`, il n'y a donc pas de requete
    supplementaire ici."""
    return sorted(transaction.items, key=lambda i: (i.position, str(i.id)))


def _serialize_item(item) -> dict:
    return {
        "label": item.label,
        "quantity": item.quantity,
        "unit_price": format_amount(item.unit_price),
    }


def _preview_items(transaction: Transaction) -> list[dict]:
    """Apercu des lignes pour l'historique CAISSE : les
    `HISTORY_ITEMS_PREVIEW` premieres lignes du ticket, et rien d'autre.

    Aucune pseudo-ligne « … » n'est ajoutee : la liste ne contient que de
    vrais articles, tous de la meme forme. C'est `items_count` (le nombre
    TOTAL de lignes du ticket) qui permet a la caisse d'afficher
    « … et N autres articles » quand il y en a davantage.
    """
    items = _ordered_items(transaction)
    return [_serialize_item(i) for i in items[:HISTORY_ITEMS_PREVIEW]]


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
        # PR10/L5 — suppression RGPD programmee. Les deux dates sont nulles
        # tant qu'aucune demande n'est en cours ; `deletion_scheduled_for`
        # est la date d'effet affichee au manager (« Suppression programmee
        # le JJ/MM/AAAA ») et celle que le cron quotidien compare a l'heure
        # courante. La fiche reste pleinement utilisable jusque-la.
        "deletion_requested_at": (
            client.deletion_requested_at.isoformat()
            if client.deletion_requested_at
            else None
        ),
        "deletion_scheduled_for": (
            client.deletion_scheduled_for.isoformat()
            if client.deletion_scheduled_for
            else None
        ),
        # PR10/L3 — renseigne quand cette fiche a ete ABSORBEE par une
        # autre : le front ouvre alors la fiche conservee, avec un bandeau.
        # L'URL de l'ancienne fiche continue donc de mener quelque part.
        "merged_into_client_id": (
            str(client.merged_into_client_id) if client.merged_into_client_id else None
        ),
        "merged_at": client.merged_at.isoformat() if client.merged_at else None,
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


# ---------------------------------------------------------------------------
# Export CSV des abonnes a la newsletter (PR11, M4)
# ---------------------------------------------------------------------------

# Colonnes du fichier, dans l'ordre. En-tetes en francais : ce fichier est lu
# par une personne (et importe dans l'outil d'e-mailing declare), pas par un
# programme de l'application.
NEWSLETTER_EXPORT_COLUMNS = [
    "email",
    "prenom",
    "nom",
    "telephone",
    "consentement_le",
    "source",
]


async def newsletter_export_csv(db: AsyncSession) -> tuple[str, str, int]:
    """Abonnes a la newsletter, en CSV (BOM UTF-8, separateur « ; »).

    Ne sortent QUE les fiches actives et abonnees :
      - `newsletter_optin` vrai (l'etat courant du registre) ;
      - ni anonymisee (RGPD deja execute), ni absorbee par une fusion (son
        contenu vit desormais sur la fiche conservee : l'exporter serait
        envoyer deux fois le meme message a la meme personne), ni en attente
        de suppression (elle a demande a partir — l'ajouter a une campagne
        pendant son delai de reflexion serait exactement ce qu'elle a
        refuse) ;
      - avec une adresse e-mail : une ligne sans adresse n'est pas
        importable dans un outil d'e-mailing, et gonflerait le compteur
        d'abonnes d'un contact qu'on ne peut pas joindre.

    `consentement_le` et `source` viennent de la DERNIERE ligne `newsletter`
    ACCORDEE du registre append-only : c'est la preuve a produire en cas de
    controle, pas le cache `newsletter_optin`.

    Retourne ``(filename, csv_text, count)``.
    """
    import csv
    import io
    from zoneinfo import ZoneInfo

    paris = ZoneInfo("Europe/Paris")

    clients = (
        (
            await db.execute(
                select(Client)
                .where(
                    Client.newsletter_optin.is_(True),
                    Client.anonymized_at.is_(None),
                    Client.merged_into_client_id.is_(None),
                    Client.deletion_requested_at.is_(None),
                    Client.email.is_not(None),
                )
                .order_by(Client.email.asc())
            )
        )
        .scalars()
        .all()
    )

    consents: dict[uuid.UUID, Consent] = {}
    if clients:
        # Une seule requete pour tout le fichier : jamais une requete par
        # fiche (une boutique qui dure finit avec des milliers d'abonnes).
        rows = (
            (
                await db.execute(
                    select(Consent)
                    .where(
                        Consent.client_id.in_([c.id for c in clients]),
                        Consent.purpose == ConsentPurpose.newsletter,
                        Consent.granted.is_(True),
                    )
                    .order_by(Consent.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        # Parcours croissant : la derniere ligne vue par client est la plus
        # recente.
        for consent in rows:
            consents[consent.client_id] = consent

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(NEWSLETTER_EXPORT_COLUMNS)
    for client in clients:
        consent = consents.get(client.id)
        granted_at = ""
        if consent is not None and consent.created_at is not None:
            moment = consent.created_at
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            granted_at = moment.astimezone(paris).strftime("%d/%m/%Y %H:%M:%S")
        # Neutralisation tableur (revue Codex #17) : prenom, nom, e-mail,
        # telephone et source sont du texte venu de la saisie en caisse. Un
        # prenom `=1+1` deviendrait une FORMULE a l'ouverture du fichier
        # chez la personne qui l'importe — la victime de cette injection
        # n'est pas notre application, c'est la boutique ou son outil
        # d'e-mailing. La date de consentement, elle, est produite par nous.
        writer.writerow(
            neutralize_csv_row(
                [
                    client.email or "",
                    client.first_name or "",
                    client.last_name or "",
                    client.phone or "",
                    granted_at,
                    consent.source.value if consent is not None else "",
                ]
            )
        )

    today = datetime.now(paris).date().isoformat()
    # BOM : sans lui, Excel lit « Léa » en « LÃ©a » a l'ouverture.
    return f"abonnes_newsletter_{today}.csv", "﻿" + buffer.getvalue(), len(clients)
