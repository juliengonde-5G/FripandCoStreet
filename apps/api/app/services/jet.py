# Nouveau service — Journal des Evenements Techniques (JET).
#
# Modelise sur le chainage HMAC-SHA256 de app/services/fiscal.py de
# l'application source (methodes `_canonical`, `_hmac`, `_iso`,
# `_get_previous_*_hash`, genesis "0") et sur le verrou
# `pg_advisory_xact_lock` utilise pour serialiser l'attribution des numeros
# de sequence (voir app/services/pos.py:103-107 et app/services/fiscal.py:247-248
# dans l'application source). Contrairement a `EventService` (l'application
# source), ce service NE PIEGE JAMAIS les exceptions : un
# echec d'ecriture du JET doit faire echouer la requete plutot que de laisser
# passer un evenement de securite non journalise (pilier "securisation" de
# l'auto-attestation NF525, cf. CDC Frip & Co Street §3.1).
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.jet import JournalEvent
from app.version import JET_SIGNATURE_VERSION

# Cle arbitraire du verrou consultatif Postgres, dediee au JET (distincte de
# toute cle utilisee par un autre sous-systeme). Sa seule fonction est
# d'empecher deux transactions concurrentes de lire le meme MAX(seq) et
# d'ecrire deux evenements avec la meme sequence.
_JET_ADVISORY_LOCK_KEY = 837_120_001

GENESIS_HASH = "0"

# ---------------------------------------------------------------------------
# Constantes de type d'evenement
# ---------------------------------------------------------------------------

EVENT_LOGIN_SUCCESS = "auth.login_success"
EVENT_LOGIN_FAILED = "auth.login_failed"
EVENT_LOGIN_RATE_LIMITED = "auth.login_rate_limited"
EVENT_LOGOUT = "auth.logout"
EVENT_TOKEN_REFRESH = "auth.token_refresh"
EVENT_SYSTEM_STARTUP = "system.startup"

# PR2 — vente, caisse espèces, Z, SumUp, paramétrage (docs/ARCHITECTURE_PR2.md §4.6)
EVENT_SALE_CREATED = "sale.created"
EVENT_SALE_CANCELLED = "sale.cancelled"
EVENT_DRAWER_OPENED = "drawer.opened"
EVENT_DRAWER_CLOSED = "drawer.closed"
EVENT_DRAWER_AUTO_CLOSED = "drawer.auto_closed"
EVENT_CASH_MOVEMENT_CREATED = "cash_movement.created"
EVENT_Z_REGULARIZATION = "z.regularization"
EVENT_PAYMENT_CB_INITIATED = "payment.cb_initiated"
EVENT_PAYMENT_CB_PAID = "payment.cb_paid"
EVENT_PAYMENT_CB_FAILED = "payment.cb_failed"
EVENT_PAYMENT_CB_CANCELLED = "payment.cb_cancelled"
EVENT_RECEIPT_DUPLICATE = "receipt.duplicate"
EVENT_CONFIG_CHANGED = "config.changed"
EVENT_SYSTEM_JOB_FAILED = "system.job_failed"
EVENT_FISCAL_INTEGRITY_CHECKED = "fiscal.integrity_checked"

# PR3 — client, e-mail (Brevo), newsletter, consentement, RGPD
# (docs/ARCHITECTURE_PR3.md §3)
EVENT_CLIENT_CREATED = "client.created"
EVENT_CLIENT_UPDATED = "client.updated"
EVENT_CLIENT_LINKED = "client.linked"
EVENT_CONSENT_GRANTED = "consent.granted"
EVENT_CONSENT_REVOKED = "consent.revoked"
EVENT_RECEIPT_EMAILED = "receipt.emailed"
EVENT_RECEIPT_EMAIL_FAILED = "receipt.email_failed"
EVENT_BREVO_SYNCED = "brevo.synced"
EVENT_BREVO_SYNC_FAILED = "brevo.sync_failed"
EVENT_BREVO_WEBHOOK_RECEIVED = "brevo.webhook_received"
EVENT_CLIENT_ANONYMIZED = "client.anonymized"
EVENT_CLIENT_EXPORTED = "client.exported"

# PR3b — impression physique des tickets (MUNBYN 047P ESC/POS, réseau ou
# WebUSB) et ouverture du tiroir-caisse Safescan SD-4141 (décision Julien :
# même matériel que l'application source). Voir `app/api/pos/router.py`.
EVENT_RECEIPT_PRINTED = "receipt.printed"
EVENT_DRAWER_KICKED = "drawer.kicked"
# Echec TCP vers la MUNBYN (connexion ou envoi) — payload host/port
# seulement, jamais l'exception systeme brute (celle-ci va au log serveur).
# Le `detail` renvoye au client HTTP est toujours un message metier
# generique, sans IP/port/errno (persona vendeuse) — voir
# `app/services/escpos_service.py::PRINTER_UNREACHABLE_MESSAGE`.
EVENT_PRINTER_UNREACHABLE = "printer.unreachable"

# PR4 — exports comptables, archives fiscales, clotures periodiques
# (docs/ARCHITECTURE_PR4.md §3).
EVENT_ACCOUNTING_EXPORT_CREATED = "accounting.export_created"
EVENT_ACCOUNTING_MISMATCH = "accounting.mismatch"
EVENT_CLOSURE_CREATED = "closure.created"
EVENT_CLOSURE_FAILED = "closure.failed"
EVENT_EXPORT_DOWNLOADED = "export.downloaded"

# PR5 — sauvegardes applicatives de la base (docs/ARCHITECTURE_PR5.md §1 G4/G5).
# `export.downloaded` (ci-dessus, kind="database_backup") couvre deja le
# telechargement ; seule la suppression manuelle d'une sauvegarde a besoin
# d'un type dedie.
EVENT_BACKUP_DELETED = "backup.deleted"

# PR7 — client en caisse (docs/ARCHITECTURE_PR7.md, I3). `client.linked`
# existe depuis PR3 (rattachement a posteriori) et sert desormais aussi au
# rattachement A LA CREATION de la vente (`client_id` dans le corps de
# `POST /pos/transactions`) ; `client.unlinked` est son pendant
# (`DELETE /pos/transactions/{id}/client`). Payload : `client_id` et
# `transaction_id` — JAMAIS de nom, d'e-mail ni de telephone (le JET est
# immuable : aucune donnee personnelle ne doit pouvoir s'y retrouver
# piegee, cf. PR3/E4 et art. 17 RGPD).
EVENT_CLIENT_UNLINKED = "client.unlinked"

# PR8 — facture B2B et avoir (docs/ARCHITECTURE_PR8.md, J5). Payload :
# IDENTIFIANTS SEULEMENT (`invoice_id`, `transaction_id`, `invoice_number`).
# Une raison sociale et un SIRET ne sont pas des donnees personnelles, mais
# le JET est immuable : on s'y tient au principe « des identifiants, jamais
# du contenu », comme pour `client.linked`.
EVENT_INVOICE_ISSUED = "invoice.issued"
EVENT_INVOICE_CREDIT_NOTE_ISSUED = "invoice.credit_note_issued"
# Incident d'integrite constate en servant un document scelle (aujourd'hui :
# un PDF de facture dont l'empreinte ne correspond plus a celle posee au
# premier telechargement). L'evenement est journalise ET COMMITE avant que
# la requete n'echoue — un incident de ce type ne doit jamais disparaitre
# avec le rollback.
EVENT_SYSTEM_INTEGRITY_ALERT = "system.integrity_alert"

# PR8 — vendeuses identifiees par code PIN (docs/ARCHITECTURE_PR8.md J2/J3).
# AUCUN de ces evenements ne porte de code PIN ni de hash de PIN : le JET est
# immuable, un secret qui y tomberait ne pourrait plus jamais en sortir. Les
# payloads se limitent a `cashier_id` (identifiant technique) et, pour les
# refus, a la nature du refus.
EVENT_CASHIER_CREATED = "cashier.created"
EVENT_CASHIER_UPDATED = "cashier.updated"
EVENT_CASHIER_PIN_CHANGED = "cashier.pin_changed"
EVENT_CASHIER_IDENTIFIED = "cashier.identified"
EVENT_CASHIER_RELEASED = "cashier.released"
EVENT_CASHIER_PIN_REJECTED = "cashier.pin_rejected"

# PR9 — file des paiements carte echoues (docs/ARCHITECTURE_PR9.md, K3).
# Payloads : IDENTIFIANTS ET MONTANTS SEULEMENT (`failed_payment_id`,
# `attempt_id`, `checkout_id`, `transaction_id`, `amount`, `error_type`,
# `retry_count`) — jamais le message d'erreur brut renvoye par SumUp, qui
# resterait piege dans un journal immuable. `payment.abandoned` porte en
# plus le motif libre saisi par le manager (borne a 200 caracteres) : c'est
# une phrase de gestion (« encaisse en especes »), pas une donnee
# personnelle.
EVENT_PAYMENT_FAILED_QUEUED = "payment.failed_queued"
EVENT_PAYMENT_RETRY_STARTED = "payment.retry_started"
EVENT_PAYMENT_RETRY_SUCCEEDED = "payment.retry_succeeded"
EVENT_PAYMENT_RETRIES_EXHAUSTED = "payment.retries_exhausted"
EVENT_PAYMENT_ABANDONED = "payment.abandoned"

# PR9 — journal des echanges SumUp (docs/ARCHITECTURE_PR9.md, K2). Seule la
# PURGE est journalisee : l'ecriture d'un echange, elle, ne l'est pas (on
# n'inscrit pas dans un journal immuable qu'on a ecrit dans un journal
# purgeable). Le payload se limite au nombre de lignes supprimees — savoir
# QUE le journal de debogage a ete vide, et de combien, suffit a expliquer
# un trou dans les traces.
EVENT_SUMUP_EXCHANGES_PURGED = "sumup_exchanges.purged"

# PR10 — fusion de deux fiches clientes en double (docs/ARCHITECTURE_PR10.md,
# L3). Payload : IDENTIFIANTS ET COMPTEURS SEULEMENT (`winner_id`,
# `source_id`, `transactions_moved`, `consents_moved`,
# `communications_moved`) — jamais un nom, un e-mail ni un numero. Le JET est
# immuable : une donnee personnelle qui y tomberait ne pourrait plus jamais
# en sortir, alors meme que la fiche absorbee vient, elle, d'etre videe
# (art. 17 RGPD).
EVENT_CLIENT_MERGED = "client.merged"

# PR10 — suppression RGPD DIFFEREE (docs/ARCHITECTURE_PR10.md, L5). La
# demande et son annulation sont des actes de gestion des donnees
# personnelles : ils doivent laisser une trace inalterable de QUI a demande
# QUOI et POUR QUAND (preuve en cas de controle CNIL). Payload :
# `client_id` et `scheduled_for` — la date d'effet n'est pas une donnee
# personnelle, le nom et l'adresse n'y figurent jamais. L'effacement
# lui-meme, a echeance, reste journalise par `client.anonymized` (PR3).
EVENT_CLIENT_DELETION_REQUESTED = "client.deletion_requested"
EVENT_CLIENT_DELETION_CANCELLED = "client.deletion_cancelled"

# PR11 — cahier du jour (docs/ARCHITECTURE_PR11.md, M2). Payloads :
# LA JOURNEE ET LA NATURE DU GESTE, jamais le contenu. `cahier.text_updated`
# porte le jour et les NOMS des champs touches (`message`, `operation`) ;
# `cahier.signed` le jour et le role (`manager` / `team`). Les textes libres
# du cahier et le nom saisi pour l'equipe n'y figurent pas : le JET est
# immuable, et une ardoise qu'on corrige tous les jours n'a rien a y faire.
EVENT_CAHIER_TEXT_UPDATED = "cahier.text_updated"
EVENT_CAHIER_SIGNED = "cahier.signed"

# PR12 — remise a zero pre-ouverture (docs/ARCHITECTURE_PR12.md, N3).
# PREMIER evenement de la nouvelle chaine : le journal vient d'etre vide, cet
# evenement est donc chaine sur le genesis "0". Payload : horodatage, auteur
# ("script" — l'operation se lance en ligne de commande, sans session HTTP,
# donc sans user_id), COMPTAGES des tables videes et identifiant de la
# sauvegarde faite juste avant. Des nombres et des identifiants, jamais un
# nom ni une adresse : le JET est immuable (meme regle que `client.merged`).
EVENT_SYSTEM_GO_LIVE_RESET = "system.go_live_reset"


class JournalService:
    """Ecrit et verifie la chaine d'evenements techniques (JET)."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Ecriture
    # ------------------------------------------------------------------

    async def record(
        self,
        event_type: str,
        *,
        user_id=None,
        username: str | None = None,
        ip: str | None = None,
        request_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> JournalEvent:
        """Enregistre un evenement immuable et retourne la ligne creee.

        Aucune exception n'est interceptee : un appelant qui a besoin que
        l'evenement soit ecrit (login, logout, demarrage...) doit laisser
        cette methode echouer la requete en cas de probleme d'ecriture.
        """
        await self.db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _JET_ADVISORY_LOCK_KEY})

        next_seq = (
            await self.db.execute(select(func.coalesce(func.max(JournalEvent.seq), 0)))
        ).scalar_one() + 1
        previous_hash = await self._previous_hash()

        # created_at est calcule cote application (et fourni explicitement a
        # l'INSERT, ce qui court-circuite le server_default herite de Base)
        # plutot que relu apres un premier flush : le trigger d'immuabilite
        # de la migration 0001 interdit tout UPDATE sur journal_events, donc
        # la ligne doit etre inseree scellee (hash calcule) en UNE seule
        # ecriture — un flush-puis-UPDATE-du-hash serait rejete par la base.
        created_at = datetime.now(timezone.utc)

        event = JournalEvent(
            seq=next_seq,
            event_type=event_type,
            user_id=user_id,
            username=username,
            ip=ip,
            request_id=request_id,
            payload=payload or {},
            created_at=created_at,
            previous_hash=previous_hash,
            hash="",
            signature_version=JET_SIGNATURE_VERSION,
        )
        event.hash = self._hmac(self._canonical_payload(event))

        self.db.add(event)
        await self.db.flush()
        return event

    async def _previous_hash(self) -> str:
        row = (
            await self.db.execute(
                select(JournalEvent.hash).order_by(JournalEvent.seq.desc()).limit(1)
            )
        ).scalar_one_or_none()
        return row if row else GENESIS_HASH

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def verify_chain(self) -> dict:
        """Recalcule chaque hash et verifie le chainage previous_hash.

        Retourne ``{valid, count, first_invalid_seq, errors}``.
        """
        events = (
            await self.db.execute(select(JournalEvent).order_by(JournalEvent.seq.asc()))
        ).scalars().all()

        previous_hash = GENESIS_HASH
        errors: list[str] = []
        first_invalid_seq: int | None = None

        for event in events:
            if event.previous_hash != previous_hash:
                errors.append(
                    f"seq={event.seq}: previous_hash mismatch "
                    f"(attendu {previous_hash!r}, trouve {event.previous_hash!r})"
                )
                if first_invalid_seq is None:
                    first_invalid_seq = event.seq
            expected = self._hmac(self._canonical_payload(event))
            if not hmac.compare_digest(event.hash, expected):
                errors.append(f"seq={event.seq}: signature invalide")
                if first_invalid_seq is None:
                    first_invalid_seq = event.seq
            previous_hash = event.hash

        return {
            "valid": not errors,
            "count": len(events),
            "first_invalid_seq": first_invalid_seq,
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # Canonicalisation / HMAC
    # ------------------------------------------------------------------

    @staticmethod
    def _iso(value: datetime | None) -> str:
        if value is None:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _canonical(payload: dict) -> bytes:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def _hmac(cls, payload: dict) -> str:
        return hmac.new(
            settings.FISCAL_SIGNING_KEY.encode("utf-8"),
            cls._canonical(payload),
            hashlib.sha256,
        ).hexdigest()

    @classmethod
    def _canonical_payload(cls, event: JournalEvent) -> dict:
        return {
            "signature_version": event.signature_version,
            "seq": event.seq,
            "event_type": event.event_type,
            "created_at": cls._iso(event.created_at),
            "user_id": str(event.user_id) if event.user_id else None,
            "username": event.username,
            "ip": event.ip,
            "request_id": event.request_id,
            "payload": event.payload or {},
            "previous_hash": event.previous_hash,
        }
