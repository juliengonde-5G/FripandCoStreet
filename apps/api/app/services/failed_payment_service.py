# Nouveau service (PR9, docs/ARCHITECTURE_PR9.md, contrat K3) — file des
# paiements carte echoues RECUPERABLES.
#
# Pourquoi une file : quand le terminal ne repond pas (Wi-Fi coupe, TPE
# occupe, 5xx SumUp), le panier est encore a l'ecran et la cliente est
# encore devant la caisse. Sans trace, la vendeuse ne sait pas si elle peut
# relancer sans risque de double debit. Une ligne `pending` dit « ce panier
# n'est pas perdu, on peut relancer le terminal », et le compteur de
# reessais dit quand il faut arreter d'insister et encaisser autrement.
#
# Pourquoi un refus de carte n'y entre JAMAIS : il n'y a rien a rejouer —
# la banque a dit non, la cliente change de carte ou de moyen de paiement.
# Mettre un declin en file ferait clignoter un ecran d'incident pour une
# situation parfaitement normale en boutique.
#
# Table d'EXPLOITATION, hors perimetre fiscal : aucune vente n'y nait. La
# regle PR2 reste entiere — la Transaction n'est ecrite qu'une fois le
# paiement constate `paid` par le serveur (`sumup_verify.verify_card_tender`),
# jamais sur la foi d'une ligne de cette table.
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.failed_payment import FailedPayment, FailedPaymentStatus
from app.models.payment_attempt import PaymentAttempt, PaymentAttemptStatus
from app.services.fiscal import PosServiceError
from app.services.jet import (
    EVENT_PAYMENT_ABANDONED,
    EVENT_PAYMENT_FAILED_QUEUED,
    EVENT_PAYMENT_RETRIES_EXHAUSTED,
    EVENT_PAYMENT_RETRY_STARTED,
    EVENT_PAYMENT_RETRY_SUCCEEDED,
    JournalService,
)
from app.services.sumup_service import redact_sumup_error

# Longueur retenue pour `last_error` — meme ordre de grandeur que
# `sumup_exchanges.error_message` (K1) : de quoi identifier la panne, pas de
# quoi recopier une reponse HTTP entiere en base.
LAST_ERROR_MAX_LEN = 500

# Motif d'abandon : champ libre de l'ecran admin, borne cote serveur (le
# front borne deja la saisie, on ne lui fait pas confiance pour autant).
ABANDON_REASON_MAX_LEN = 200

# Nombre de relances offertes a la vendeuse avant de lui dire d'encaisser
# autrement. Trois suffisent : au-dela, c'est le terminal ou le reseau qui
# est en cause, pas un alea.
DEFAULT_MAX_RETRIES = 3

# Causes AMBIGUES : le POST de push a pu atteindre SumUp sans que la reponse
# nous parvienne (coupure, delai depasse). Impossible de savoir, de notre
# cote, si un montant s'affiche sur le terminal — voire s'il a deja ete
# encaisse. Repousser sans verifier, c'est risquer un DOUBLE DEBIT.
#
# Les autres causes ne sont pas ambigues : un `http_5xx` / `http_4xx` est une
# REPONSE de SumUp (la requete a ete traitee et rejetee, aucun paiement n'a
# demarre), et un terminal indisponible est refuse par le pre-vol avant tout
# envoi. Dans ces cas on repousse directement, sans appel supplementaire.
AMBIGUOUS_ERROR_TYPES = ("transport", "timeout")

# Verdicts de la reconciliation.
RECONCILE_PUSH = "push"
RECONCILE_PAID = "paid"

# Statuts « ouverts » : une ligne encore susceptible d'etre resolue par un
# paiement qui finit par passer. `exhausted` en fait partie — la vendeuse
# peut avoir encaisse via le reessai historique (`/{checkout_id}/retry`)
# apres avoir epuise les relances de la file ; la ligne doit alors se
# refermer proprement plutot que rester un faux incident.
OPEN_STATUSES = (FailedPaymentStatus.pending, FailedPaymentStatus.exhausted)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Classification d'un echec de push TPE
# ---------------------------------------------------------------------------


def classify_push_result(result: dict) -> tuple[bool, str]:
    """Traduit un retour de ``SumUpService._push_to_reader`` en (recuperable, error_type).

    Le service SumUp sait deja dire si l'erreur est recuperable
    (``_reader_error_recoverability`` : TPE occupe/hors ligne, 429, 5xx, plus
    les erreurs de transport). On se contente ici de nommer la cause avec le
    meme vocabulaire que `sumup_exchanges.error_type` (K1), auquel s'ajoute
    `declined` pour un refus definitif.
    """
    recoverable = bool(result.get("recoverable"))
    http_status = result.get("http_status")

    if http_status is None:
        # Aucune reponse HTTP : le TPE n'a meme pas ete joint. Le service a
        # range le nom de l'exception httpx dans `error_type`.
        exception_name = str(result.get("error_type") or "").lower()
        return recoverable, "timeout" if "timeout" in exception_name else "transport"

    status = int(http_status)
    if 500 <= status < 600:
        return recoverable, "http_5xx"
    if 400 <= status < 500:
        # 429 (trop d'appels) est recuperable et reste un 4xx ; un 401/403
        # (cle invalide) ne l'est pas — mais on ne met en file que du
        # recuperable, donc le libelle ne sera pas persiste dans ce cas.
        return recoverable, "http_4xx" if recoverable else "declined"
    return recoverable, "declined"


# ---------------------------------------------------------------------------
# Reconciliation avant un nouveau push (anti double debit)
# ---------------------------------------------------------------------------


def apply_paid_fields(attempt: PaymentAttempt, poll: dict) -> None:
    """Passe un essai a `paid` avec les identifiants SumUp du releve.

    Memes champs que le constat par le polling de la caisse : un essai
    reconcilie ne doit pas etre plus pauvre qu'un essai suivi normalement.
    """
    attempt.status = PaymentAttemptStatus.paid
    attempt.sumup_transaction_id = poll.get("sumup_transaction_id")
    attempt.sumup_transaction_code = poll.get("sumup_transaction_code")
    attempt.sumup_auth_code = poll.get("sumup_auth_code")
    attempt.sumup_card_brand = poll.get("sumup_card_brand")
    attempt.sumup_card_last4 = poll.get("sumup_card_last4")


async def queued_error_type(db: AsyncSession, client_uuid: uuid.UUID) -> str | None:
    """Cause du dernier echec de cette vente, telle que mise en file.

    C'est la seule trace qui distingue « SumUp a repondu non » de « on n'a
    jamais su ce que SumUp a fait » : `payment_attempts` ne garde qu'un
    message. Pas de ligne en file = echec non recuperable = SumUp a repondu.
    """
    failed_payment = await find_open_for_client_uuid(db, client_uuid)
    return failed_payment.error_type if failed_payment is not None else None


async def reconcile_before_push(
    db: AsyncSession,
    svc,
    attempt: PaymentAttempt,
    *,
    error_type: str | None,
) -> tuple[str, dict | None]:
    """Verifie le sort du checkout d'origine avant d'en pousser un nouveau.

    Retourne ``(RECONCILE_PAID | RECONCILE_PUSH, releve SumUp | None)`` :

    - cause non ambigue → on repousse sans rien demander (aucun paiement
      n'a pu demarrer cote terminal) ;
    - le checkout d'origine est PAID → **on ne repousse pas** : la cliente a
      deja ete debitee, l'essai passe `paid` et la caisse enchaine sur la
      vente avec ce checkout-la ;
    - sinon le terminal peut encore afficher le montant (la Transactions
      API repond « en attente » aussi bien pour un paiement en cours que
      pour un checkout jamais cree) : on coupe l'ecran du TPE
      (`terminate_reader_checkout`) avant de repousser, pour qu'il n'y ait
      jamais deux montants en circulation ;
    - un checkout d'origine explicitement echoue ou annule ne necessite
      aucune coupure : on repousse directement.

    Aucun commit ici : l'appelant reste maitre de sa transaction.
    """
    if error_type not in AMBIGUOUS_ERROR_TYPES:
        return RECONCILE_PUSH, None

    poll = await svc.get_checkout_status(
        attempt.checkout_id, client_transaction_id=attempt.client_transaction_id
    )
    status = str(poll.get("status") or "").upper()

    if status == "PAID":
        apply_paid_fields(attempt, poll)
        await db.flush()
        return RECONCILE_PAID, poll

    if status == "PENDING":
        await svc.terminate_reader_checkout()

    return RECONCILE_PUSH, poll


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------


async def get(db: AsyncSession, failed_payment_id: uuid.UUID) -> FailedPayment | None:
    return (
        await db.execute(select(FailedPayment).where(FailedPayment.id == failed_payment_id))
    ).scalar_one_or_none()


async def get_or_404(db: AsyncSession, failed_payment_id: uuid.UUID) -> FailedPayment:
    failed_payment = await get(db, failed_payment_id)
    if failed_payment is None:
        raise PosServiceError(
            "Paiement en attente de réessai introuvable.", code="not_found", status_code=404
        )
    return failed_payment


async def find_open_for_client_uuid(
    db: AsyncSession, client_uuid: uuid.UUID
) -> FailedPayment | None:
    """Ligne encore ouverte pour cette vente a venir, s'il y en a une.

    C'est `client_uuid` qui fait le lien — et pas `attempt_id` — parce qu'un
    reessai cree un NOUVEL essai de paiement : la ligne en file reste
    rattachee au premier essai, mais elle suit la vente.
    """
    return (
        await db.execute(
            select(FailedPayment)
            .where(
                FailedPayment.client_uuid == client_uuid,
                FailedPayment.status.in_(OPEN_STATUSES),
            )
            .order_by(FailedPayment.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_queue(
    db: AsyncSession,
    status: str | None = None,
    *,
    limit: int = 100,
) -> tuple[list[FailedPayment], int]:
    """File, du plus recent au plus ancien. `status` vide = tous les statuts."""
    query = select(FailedPayment).order_by(FailedPayment.created_at.desc())
    if status:
        try:
            wanted = FailedPaymentStatus(status)
        except ValueError:
            raise PosServiceError(
                f"Statut inconnu : {status}", code="invalid_status", status_code=422
            ) from None
        query = query.where(FailedPayment.status == wanted)
    rows = (await db.execute(query.limit(limit))).scalars().all()
    return [row for row in rows], len(rows)


# Nom du contrat (K3) conserve comme alias : `list_queue` est le nom reel
# pour ne pas masquer le builtin `list` a l'interieur de ce module.
list = list_queue  # noqa: A001


async def checkout_id_of(db: AsyncSession, failed_payment: FailedPayment) -> str | None:
    """Dernier `checkout_id` connu pour cette vente (celui a repolling)."""
    return (
        await db.execute(
            select(PaymentAttempt.checkout_id)
            .where(PaymentAttempt.client_uuid == failed_payment.client_uuid)
            .order_by(PaymentAttempt.attempt_count.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def snapshot(db: AsyncSession, failed_payment: FailedPayment) -> dict:
    """Serialise une ligne en la RECHARGEANT d'abord.

    Un commit intermediaire — celui du SAVEPOINT du journal des echanges
    SumUp, par exemple — expire les objets de la session : un acces
    paresseux a un attribut expire, en contexte async, leve
    `MissingGreenlet`. On recharge donc explicitement plutot que de compter
    sur l'ordre des appels dans la route.
    """
    await db.refresh(failed_payment)
    checkout_id = await checkout_id_of(db, failed_payment)
    return serialize(failed_payment, checkout_id=checkout_id)


def serialize(failed_payment: FailedPayment, *, checkout_id: str | None = None) -> dict:
    return {
        "id": str(failed_payment.id),
        "attempt_id": str(failed_payment.attempt_id),
        "client_uuid": str(failed_payment.client_uuid),
        "checkout_id": checkout_id,
        "amount": float(failed_payment.amount),
        "status": failed_payment.status.value,
        "error_type": failed_payment.error_type,
        "last_error": failed_payment.last_error,
        "retry_count": failed_payment.retry_count,
        "max_retries": failed_payment.max_retries,
        "next_retry_at": (
            failed_payment.next_retry_at.isoformat() if failed_payment.next_retry_at else None
        ),
        "resolved_at": (
            failed_payment.resolved_at.isoformat() if failed_payment.resolved_at else None
        ),
        "transaction_id": (
            str(failed_payment.transaction_id) if failed_payment.transaction_id else None
        ),
        "cashier_id": str(failed_payment.cashier_id) if failed_payment.cashier_id else None,
        "created_at": (
            failed_payment.created_at.isoformat() if failed_payment.created_at else None
        ),
        "updated_at": (
            failed_payment.updated_at.isoformat() if failed_payment.updated_at else None
        ),
    }


# ---------------------------------------------------------------------------
# Mise en file
# ---------------------------------------------------------------------------


async def enqueue(
    db: AsyncSession,
    attempt: PaymentAttempt,
    *,
    error_type: str,
    last_error: str | None,
    cashier_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    username: str | None = None,
    ip: str | None = None,
    request_id: str | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> FailedPayment:
    """Met un essai echoue en file — a n'appeler que pour une cause RECUPERABLE.

    Idempotent par vente : si une ligne est deja ouverte pour ce
    `client_uuid`, on la reactualise (derniere cause constatee) au lieu d'en
    creer une seconde. Une vente n'a qu'un incident a la fois du point de vue
    de la vendeuse ; empiler des lignes transformerait l'ecran admin en
    doublons illisibles.
    """
    redacted = redact_sumup_error(last_error, max_len=LAST_ERROR_MAX_LEN) or None

    existing = await find_open_for_client_uuid(db, attempt.client_uuid)
    if existing is not None:
        existing.error_type = error_type
        existing.last_error = redacted
        await db.flush()
        return existing

    failed_payment = FailedPayment(
        attempt_id=attempt.id,
        client_uuid=attempt.client_uuid,
        amount=attempt.amount,
        status=FailedPaymentStatus.pending,
        error_type=error_type,
        last_error=redacted,
        retry_count=0,
        max_retries=max_retries,
        cashier_id=cashier_id,
    )
    db.add(failed_payment)
    await db.flush()

    # JET : IDENTIFIANTS ET MONTANT SEULEMENT (contrat K3) — jamais le
    # message d'erreur brut, qui pourrait charrier du contenu de reponse
    # SumUp dans un journal immuable.
    await JournalService(db).record(
        EVENT_PAYMENT_FAILED_QUEUED,
        user_id=user_id,
        username=username,
        ip=ip,
        request_id=request_id,
        payload={
            "failed_payment_id": str(failed_payment.id),
            "attempt_id": str(attempt.id),
            "checkout_id": attempt.checkout_id,
            "amount": str(attempt.amount),
            "error_type": error_type,
        },
    )
    return failed_payment


# ---------------------------------------------------------------------------
# Reessai manuel
# ---------------------------------------------------------------------------


class RetryOutcome:
    """Resultat d'un reessai : l'essai concerne, la reponse SumUp, l'echec eventuel.

    `reconciled_paid` signale le cas ou AUCUN nouveau paiement n'a ete
    pousse : le checkout d'origine etait deja paye. `attempt` est alors
    l'essai d'ORIGINE (passe `paid`), pas un nouvel essai.
    """

    def __init__(
        self,
        attempt: PaymentAttempt,
        result: dict,
        failed: bool,
        *,
        reconciled_paid: bool = False,
    ) -> None:
        self.attempt = attempt
        self.result = result
        self.failed = failed
        self.reconciled_paid = reconciled_paid


class RetriesExhausted(PosServiceError):
    """Plus de relance possible — porte de quoi renseigner l'écran de caisse.

    Les trois champs voyagent A LA RACINE du corps de la 409 (comme
    `retry_after` sur le 429 du code PIN) : la caisse doit pouvoir écrire
    « réessais épuisés (3 sur 3) » sans redemander la file au serveur.
    """

    status_code = 409
    code = "retries_exhausted"

    def __init__(self, failed_payment: FailedPayment) -> None:
        super().__init__("Réessais épuisés — choisissez un autre moyen de paiement.")
        self.failed_payment_id = str(failed_payment.id)
        self.retry_count = failed_payment.retry_count
        self.max_retries = failed_payment.max_retries

    def as_body(self) -> dict:
        return {
            "detail": str(self),
            "code": self.code,
            "failed_payment_id": self.failed_payment_id,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
        }


def error_fields(failed_payment: FailedPayment | None) -> dict:
    """Champs de file à poser à la RACINE d'une réponse d'erreur CB.

    `None` partout quand rien n'a été mis en file (refus de carte) : le
    contrat de la réponse ne change pas de forme selon le cas, seule la
    valeur change — un front n'a pas à tester la présence des clés.
    """
    if failed_payment is None:
        return {"failed_payment_id": None, "retry_count": None, "max_retries": None}
    return {
        "failed_payment_id": str(failed_payment.id),
        "retry_count": failed_payment.retry_count,
        "max_retries": failed_payment.max_retries,
    }


def _guard_retryable(failed_payment: FailedPayment) -> None:
    if failed_payment.status == FailedPaymentStatus.succeeded:
        raise PosServiceError(
            "Ce paiement a déjà été encaissé.", code="already_paid", status_code=409
        )
    if failed_payment.status == FailedPaymentStatus.abandoned:
        raise PosServiceError(
            "Ce paiement a été abandonné — reprenez la vente depuis la caisse.",
            code="not_retryable",
            status_code=409,
        )
    if (
        failed_payment.status == FailedPaymentStatus.exhausted
        or failed_payment.retry_count >= failed_payment.max_retries
    ):
        raise RetriesExhausted(failed_payment)


async def retry(
    db: AsyncSession,
    failed_payment: FailedPayment,
    *,
    svc,
    description: str,
    reader_id: str | None = None,
    user_id: uuid.UUID | None = None,
    username: str | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> RetryOutcome:
    """Repousse le paiement sur le TPE — meme mecanique que le reessai historique.

    Un reessai = un NOUVEL essai de paiement (`attempt_count + 1`, nouveau
    `client_transaction_id`), jamais une reecriture de l'essai precedent :
    cote SumUp comme en base, chaque presentation au terminal reste tracee
    separement. La ligne en file, elle, ne bouge pas — seul son
    `retry_count` avance.

    N'ecrit AUCUNE vente : un push accepte rend un essai `pending`, et c'est
    le polling (`GET /{checkout_id}/status`) puis la creation de vente qui
    constateront `paid`.

    Avant tout nouveau push, le sort du checkout d'origine est reconcilie
    quand la cause de l'echec est ambigue (cf. `reconcile_before_push`) :
    c'est ce qui evite de presenter deux fois le meme montant a la carte.
    """
    source = (
        await db.execute(
            select(PaymentAttempt)
            .where(PaymentAttempt.client_uuid == failed_payment.client_uuid)
            .order_by(PaymentAttempt.attempt_count.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if source is None:
        raise PosServiceError(
            "Aucun essai de paiement à réessayer pour cette vente.",
            code="not_found",
            status_code=404,
        )
    if source.status == PaymentAttemptStatus.pending:
        # Un essai est encore en cours cote TPE : relancer maintenant
        # risquerait un double debit si la cliente tape sa carte entre les
        # deux. La caisse doit d'abord annuler ou laisser le polling
        # conclure.
        raise PosServiceError(
            "Un paiement est encore en attente sur le terminal — annulez-le avant de réessayer.",
            code="attempt_pending",
            status_code=409,
        )
    if source.status == PaymentAttemptStatus.paid:
        raise PosServiceError(
            "Ce paiement a déjà été encaissé.", code="already_paid", status_code=409
        )

    # Reconciliation AVANT le garde-fou des relances : si la cliente a deja
    # ete debitee, il faut le constater meme quand les reessais sont
    # epuises — refuser la lecture laisserait un encaissement orphelin.
    if failed_payment.status in OPEN_STATUSES:
        verdict, poll = await reconcile_before_push(
            db, svc, source, error_type=failed_payment.error_type
        )
        if verdict == RECONCILE_PAID:
            await mark_succeeded(
                db,
                failed_payment,
                user_id=user_id,
                username=username,
                ip=ip,
                request_id=request_id,
            )
            # Aucun push : le compteur de relances ne bouge pas, et il n'y a
            # pas de `retry_started` a journaliser — rien n'a ete lance.
            return RetryOutcome(source, poll or {}, False, reconciled_paid=True)

    _guard_retryable(failed_payment)

    new_count = source.attempt_count + 1
    new_checkout_id = f"{failed_payment.client_uuid}:r{new_count}"
    result = await svc._push_to_reader(  # noqa: SLF001 — service interne, meme paquet
        amount=failed_payment.amount,
        checkout_id=new_checkout_id,
        description=description,
    )
    failed = str(result.get("status", "")).upper() == "FAILED"

    new_attempt = PaymentAttempt(
        client_uuid=failed_payment.client_uuid,
        amount=failed_payment.amount,
        status=PaymentAttemptStatus.failed if failed else PaymentAttemptStatus.pending,
        checkout_id=result.get("checkout_id") or new_checkout_id,
        client_transaction_id=(
            result.get("client_transaction_id") or new_checkout_id
        ),
        reader_id=reader_id if reader_id is not None else getattr(svc, "reader_id", None),
        error_message=(
            redact_sumup_error(
                result.get("error_detail") or result.get("error_friendly") or "Refusé par SumUp"
            )
            if failed
            else None
        ),
        attempt_count=new_count,
    )
    db.add(new_attempt)

    failed_payment.retry_count += 1
    failed_payment.next_retry_at = None
    await db.flush()

    journal = JournalService(db)
    await journal.record(
        EVENT_PAYMENT_RETRY_STARTED,
        user_id=user_id,
        username=username,
        ip=ip,
        request_id=request_id,
        payload={
            "failed_payment_id": str(failed_payment.id),
            "checkout_id": new_attempt.checkout_id,
            "retry_count": failed_payment.retry_count,
        },
    )

    if failed:
        recoverable, error_type = classify_push_result(result)
        failed_payment.error_type = error_type
        failed_payment.last_error = (
            redact_sumup_error(
                result.get("error_detail") or result.get("error_friendly") or "Refusé par SumUp",
                max_len=LAST_ERROR_MAX_LEN,
            )
            or None
        )
        # Une cause non recuperable (refus de carte) ferme la file au meme
        # titre qu'un epuisement : il n'y a plus rien a relancer.
        if not recoverable or failed_payment.retry_count >= failed_payment.max_retries:
            failed_payment.status = FailedPaymentStatus.exhausted
            failed_payment.resolved_at = _now()
            await db.flush()
            await journal.record(
                EVENT_PAYMENT_RETRIES_EXHAUSTED,
                user_id=user_id,
                username=username,
                ip=ip,
                request_id=request_id,
                payload={
                    "failed_payment_id": str(failed_payment.id),
                    "checkout_id": new_attempt.checkout_id,
                    "retry_count": failed_payment.retry_count,
                },
            )
        await db.flush()

    return RetryOutcome(new_attempt, result, failed)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def mark_succeeded(
    db: AsyncSession,
    failed_payment: FailedPayment,
    transaction_id: uuid.UUID | None = None,
    *,
    user_id: uuid.UUID | None = None,
    username: str | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> FailedPayment:
    """Referme une ligne : le paiement est finalement passe.

    Appelee aux DEUX endroits ou un essai devient `paid` : le polling de la
    caisse (pas encore de vente — `transaction_id` reste vide) et la
    verification serveur qui precede l'ecriture de la vente (la vente existe,
    on la rattache). Idempotente : le second appel ne fait que completer le
    `transaction_id` manquant, sans reecrire le JET.
    """
    if failed_payment.status == FailedPaymentStatus.succeeded:
        if transaction_id is not None and failed_payment.transaction_id is None:
            failed_payment.transaction_id = transaction_id
            await db.flush()
        return failed_payment

    failed_payment.status = FailedPaymentStatus.succeeded
    failed_payment.resolved_at = _now()
    failed_payment.next_retry_at = None
    if transaction_id is not None:
        failed_payment.transaction_id = transaction_id
    await db.flush()

    await JournalService(db).record(
        EVENT_PAYMENT_RETRY_SUCCEEDED,
        user_id=user_id,
        username=username,
        ip=ip,
        request_id=request_id,
        payload={
            "failed_payment_id": str(failed_payment.id),
            "amount": str(failed_payment.amount),
            "retry_count": failed_payment.retry_count,
            "transaction_id": str(transaction_id) if transaction_id is not None else None,
        },
    )
    return failed_payment


async def _find_to_resolve(db: AsyncSession, client_uuid: uuid.UUID) -> FailedPayment | None:
    """Ligne que le constat d'un `paid` doit refermer OU completer.

    Plus large que `find_open_for_client_uuid` : une ligne deja `succeeded`
    par le polling de la caisse (la vente n'existait pas encore) reste a
    completer de son `transaction_id` a l'ecriture de la vente. Une ligne
    abandonnee, elle, ne se rouvre jamais.
    """
    return (
        await db.execute(
            select(FailedPayment)
            .where(
                FailedPayment.client_uuid == client_uuid,
                FailedPayment.status != FailedPaymentStatus.abandoned,
                or_(
                    FailedPayment.status != FailedPaymentStatus.succeeded,
                    FailedPayment.transaction_id.is_(None),
                ),
            )
            .order_by(FailedPayment.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def resolve_if_queued(
    db: AsyncSession,
    client_uuid: uuid.UUID,
    transaction_id: uuid.UUID | None = None,
    *,
    user_id: uuid.UUID | None = None,
    username: str | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> FailedPayment | None:
    """Referme la ligne en file de cette vente, s'il y en a une. Sinon, rien.

    Raccourci des points de constat `paid`, qui n'ont pas a savoir si la
    vente a connu un incident : la file est interrogee, pas supposee.
    """
    failed_payment = await _find_to_resolve(db, client_uuid)
    if failed_payment is None:
        return None
    return await mark_succeeded(
        db,
        failed_payment,
        transaction_id,
        user_id=user_id,
        username=username,
        ip=ip,
        request_id=request_id,
    )


async def abandon(
    db: AsyncSession,
    failed_payment: FailedPayment,
    reason: str,
    *,
    user_id: uuid.UUID | None = None,
    username: str | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> FailedPayment:
    """Ferme une ligne a la main (motif libre) — geste d'admin, pas de caisse."""
    if failed_payment.status == FailedPaymentStatus.succeeded:
        raise PosServiceError(
            "Ce paiement a été encaissé — il n'y a rien à abandonner.",
            code="already_paid",
            status_code=409,
        )
    if failed_payment.status == FailedPaymentStatus.abandoned:
        raise PosServiceError(
            "Ce paiement est déjà abandonné.", code="already_abandoned", status_code=409
        )

    cleaned = (reason or "").strip()
    if not cleaned:
        raise PosServiceError(
            "Indiquez un motif d'abandon.", code="reason_required", status_code=422
        )
    cleaned = cleaned[:ABANDON_REASON_MAX_LEN]

    failed_payment.status = FailedPaymentStatus.abandoned
    failed_payment.resolved_at = _now()
    failed_payment.next_retry_at = None
    await db.flush()

    await JournalService(db).record(
        EVENT_PAYMENT_ABANDONED,
        user_id=user_id,
        username=username,
        ip=ip,
        request_id=request_id,
        payload={
            "failed_payment_id": str(failed_payment.id),
            "reason": cleaned,
        },
    )
    return failed_payment
