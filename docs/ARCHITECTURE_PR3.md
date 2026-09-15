# PR3 — Architecture : client, ticket par e-mail (Brevo), newsletter, consentement, RGPD

**Statut :** contrat d'architecture de l'orchestrateur, opposable aux agents. Empilé sur PR2 (`docs/ARCHITECTURE_PR2.md`).
**Périmètre CDC :** §2.1 Fidélité (collecte de coordonnées), Ticket client (e-mail via Brevo), Newsletter (liste Brevo dédiée) ; §3.3 RGPD ; §4.3 ; critère §5.2 « ticket reçu, contact visible dans Brevo ».
**Hors PR3 :** exports, archive fiscale, clôtures périodiques, attestation (PR4). Aucun point de fidélité, aucune remise client, aucun SMS.

Source d'extraction : Vintiz `apps/api/app/services/email_gateway.py`, `brevo_contacts.py`, `api/brevo/router.py`, `models/client.py` (Client + Consent), `services/rgpd.py` (request/hard delete), `services/client_lookup.py`. Copié puis taillé.

---

## 1. Décisions structurantes

| # | Décision | Conséquence |
|---|---|---|
| E1 | Le compte Brevo est **partagé** avec Vintiz Vernon (Julien). Liste **dédiée** « Frip & Co Street » (`BREVO_LIST_ID`). | Fripco n'écrit jamais sur la blocklist globale d'un contact ni ne supprime un contact Brevo : seulement `listIds`/`unlinkListIds` sur SA liste, et les attributs `PRENOM`/`NOM` s'ils sont vides. Un opt-out ou une suppression RGPD côté fripco = retrait de la liste fripco (+ consentement révoqué en base). |
| E2 | Contact Brevo poussé **uniquement si consentement newsletter** (case non pré-cochée). Le ticket par e-mail est un envoi **transactionnel** qui ne crée pas de contact. | Minimisation RGPD : un client qui refuse la newsletter n'existe pas dans Brevo. |
| E3 | `transactions.client_id` = seule colonne **mutable hors hash** (déclarée dans l'attestation). | Trigger 0003 : exception `client_id` (et `updated_at`) sur `transactions` ; rien d'autre ne change. |
| E4 | Suppression RGPD = **anonymisation** de la fiche (`clients`), jamais suppression de ligne ni modification d'une vente. | `email` → `supprime-<uuid>@anonyme.invalid`, noms vidés, `anonymized_at` posé, consentements révoqués (nouvelle ligne), contact retiré de la liste fripco. Les transactions gardent leur `client_id` (qui ne pointe plus vers aucune donnée personnelle). |
| E5 | Journal des consentements **append-only** (trigger UPDATE/DELETE interdits), horodaté, avec source et version de politique. | L'état courant = dernière ligne par (client, purpose). |
| E6 | E-mail : passerelle **Brevo → SMTP → simulation** extraite de Vintiz, **avec** la garde CNIL « pixel d'ouverture » de Vintiz : envoi Brevo refusé tant que `BREVO_ANONYMOUS_TRACKING=true` n'est pas posé (le compte partagé doit être en suivi anonyme) ; à défaut repli SMTP puis simulation, état visible en admin. | Aucun tracker individuel sans base légale. Question ouverte pour Julien : le compte Brevo partagé est-il en suivi anonyme ? |
| E7 | Chaque envoi d'e-mail est tracé (`communications`), chaque événement client/consentement est au JET. | Preuve « ticket reçu » et traçabilité RGPD. |
| E8 | Mention d'information RGPD **affichée sur l'écran de saisie** (CDC §3.3) : responsable, finalités (ticket / newsletter optionnelle), durée, droits, contact `shop.dpo_email`. | Texte court, en français, dans le composant de saisie ; version de politique `CONSENT_POLICY_VERSION = "2026-09"` enregistrée avec chaque consentement. |
| E9 | Webhook Brevo (`unsubscribed`, `hardBounce`/`hard_bounce`, `contact_deleted` si fourni) authentifié par `BREVO_WEBHOOK_TOKEN` (query `?token=` ou header `X-Brevo-Token`) ; sans token configuré → 403. | Un désabonnement Brevo révoque le consentement local (source `webhook`). |
| E10 | Aucun secret en base : `BREVO_API_KEY`, `BREVO_LIST_ID`, `BREVO_WEBHOOK_TOKEN`, `BREVO_ANONYMOUS_TRACKING`, `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME`, `SMTP_*` en env ; admin en lecture d'état seulement. | |

---

## 2. Modèle de données (migration `0003_clients_email`)

### `clients`
`id`, `email` varchar(255) unique not null (minuscules, trim), `first_name` varchar(100) nullable, `last_name` varchar(100) nullable, `newsletter_optin` bool not null default false (**cache** de l'état courant du consentement `newsletter`), `brevo_synced_at` timestamptz nullable, `brevo_last_error` text nullable, `anonymized_at` timestamptz nullable, `created_by_user_id` FK users nullable, `created_at`, `updated_at`. Index sur `email`.

### `consents` (append-only)
`id`, `client_id` FK not null, `purpose` enum `newsletter` (seule valeur en PR3 ; enum extensible), `granted` bool not null, `source` enum `pos|webhook|admin|rgpd`, `policy_version` varchar(16) not null, `recorded_by_user_id` FK nullable, `note` text nullable, `created_at`. Trigger `trg_protect_consent` : UPDATE/DELETE interdits.

### `communications`
`id`, `client_id` FK nullable, `transaction_id` FK nullable, `kind` enum `receipt`, `channel` enum `email`, `recipient` varchar(255), `subject` varchar(255), `provider` enum `brevo|smtp|simulated`, `status` enum `sent|failed|simulated`, `provider_message_id` varchar(128) nullable, `error` text nullable, `created_at`. Pas de contenu du mail (le ticket est déjà dans `receipts`).

### `transactions`
+ `client_id` UUID FK clients nullable. Trigger `trg_protect_signed_transaction` modifié pour **ignorer** `client_id` dans la comparaison (E3). Migration 0003 réécrit la fonction (`CREATE OR REPLACE FUNCTION`), une commande par `execute`.

`app/version.py` : `APP_VERSION="0.3.0"`, `EXPECTED_DB_REVISION="0003"`, `FISCAL_SIGNATURE_VERSION=3` (inchangé : `client_id` hors payload), `CONSENT_POLICY_VERSION="2026-09"`.

---

## 3. Services

- `services/email_gateway.py` (extrait de Vintiz) : `EmailMessage(to, subject, html, text)`, `send_email(message) -> EmailResult(provider, status, message_id, error)`, `describe_active_provider()`. Brevo `POST https://api.brevo.com/v3/smtp/email` (base `BREVO_API_BASE` configurable, défaut `https://api.brevo.com`, pour les tests de bout en bout), en-tête `api-key`, `sender` = `EMAIL_FROM_*`. Garde E6. SMTP via `smtplib` (comme Vintiz). Simulation = log + `status=simulated`. Retirer : tout ce qui concerne le suivi d'ouverture par consentement individuel (fripco n'envoie aucun e-mail marketing), Twilio, templates éditables.
- `services/receipt_email.py` (nouveau, petit) : construit l'e-mail du ticket : sujet « Votre ticket Frip & Co Street n° X », HTML sobre (en-tête boutique depuis `app_settings.shop`, ticket en `<pre>`, mentions légales : nom, adresse, SIRET, n° TVA, pied `receipt.footer_note`, et un paragraphe RGPD court avec `dpo_email`), texte brut = ticket. Ne contient **jamais** de lien de tracking ni d'image externe.
- `services/brevo_contacts.py` (extrait, réduit E1/E2) : `push_contact(client) -> SyncResult` = `POST /v3/contacts` `{email, attributes:{PRENOM, NOM}, listIds:[BREVO_LIST_ID], updateEnabled:true}` (201/204 = ok) ; `remove_from_list(email)` = `POST /v3/contacts/lists/{id}/contacts/remove` `{emails:[…]}` ; `apply_webhook_event(db, event)` : `unsubscribed`/`hard_bounce`/`contact_deleted` → consentement révoqué (source `webhook`) + `newsletter_optin=false` + JET `consent.revoked`. Base URL `BREVO_API_BASE`. Aucun appel `DELETE /v3/contacts`, aucun `emailBlacklisted`.
- `services/client_service.py` (nouveau, patron Vintiz `client_lookup` + `rgpd`) : `upsert_by_email(db, email, first_name, last_name, user)` (normalise, crée ou met à jour les noms si fournis, JET `client.created`/`client.updated`), `record_consent(db, client, purpose, granted, source, user, note)` (ligne append-only + cache + JET `consent.granted`/`consent.revoked` ; **idempotent** : ne réécrit pas si l'état courant est identique et la source est `pos`), `link_transaction(db, tx, client, user)` (E3, JET `client.linked`), `anonymize(db, client, user, reason)` (E4, JET `client.anonymized`), `search(db, q)`, `get_full(db, client)` (fiche + consentements + communications + tickets liés).
- `services/pos.py` : `create_transaction` inchangé. Nouveau `PosService.attach_client_and_send_receipt(...)` orchestrant : upsert → consentement (si case cochée, sinon rien ; si le client existait avec opt-in et décoche → révocation source `pos`) → lien → e-mail (communication) → push Brevo si opt-in (échec Brevo = `brevo_last_error`, n'empêche pas la vente ni l'e-mail ; JET `brevo.sync_failed`).

Événements JET PR3 : `client.created`, `client.updated`, `client.linked`, `consent.granted`, `consent.revoked`, `receipt.emailed` (payload : number, provider, status), `receipt.email_failed`, `brevo.synced`, `brevo.sync_failed`, `brevo.webhook_received`, `client.anonymized`, `client.exported` (export RGPD JSON).

---

## 4. API (JWT sauf webhook)

| Méthode | Route | Corps / réponse |
|---|---|---|
| POST | `/pos/transactions/{id}/client` | `{email, first_name?, last_name?, newsletter_optin: bool, send_receipt: bool=true}` → `{client: {id, email, first_name, last_name, newsletter_optin}, receipt_email: {status, provider}|null, brevo: {status}|null}` ; 404 si vente inconnue, 422 e-mail invalide, 409 si la vente est déjà liée à un autre client (`client_already_linked`) |
| POST | `/pos/transactions/{id}/receipt/email` | `{email?}` (défaut : e-mail du client lié ; sinon 422) → `{status, provider}` ; incrémente `duplicate_count`, JET `receipt.emailed` + `receipt.duplicate` |
| GET | `/admin/clients?q=&limit=` | `{clients: [{id, email, first_name, last_name, newsletter_optin, created_at, anonymized_at}]}` |
| GET | `/admin/clients/{id}` | fiche complète : client, `consents` (historique), `communications`, `transactions` (n°, date, total) |
| POST | `/admin/clients/{id}/consents` | `{purpose:"newsletter", granted: bool, note?}` (source `admin`) |
| POST | `/admin/clients/{id}/anonymize` | `{reason}` → 200 fiche anonymisée (E4) |
| GET | `/admin/clients/{id}/export` | JSON portable (Art. 20) : fiche, consentements, tickets (texte), communications ; JET `client.exported` |
| GET | `/admin/messaging/status` | `{email: {provider: brevo|smtp|simulated, anonymous_tracking: bool, from}, brevo_contacts: {configured, list_id_set, webhook_token_set}}` — aucun secret |
| POST | `/brevo/webhook?token=` | **sans JWT**, token E9, corps = événement ou liste ; → `{applied, skipped}` |

Erreurs : `PosServiceError` → `{detail, code}` comme PR2.

---

## 5. Front

- **Écran succès de vente** (après paiement) : bloc « Envoyer le ticket par e-mail » : champ e-mail (clavier e-mail), prénom / nom facultatifs, case **non cochée** « Je souhaite recevoir les actualités et événements Frip & Co Street », mention RGPD courte (E8, texte fourni par `GET /admin/settings/shop` : `dpo_email`), boutons « Envoyer le ticket » et « Passer ». Résultat : « Ticket envoyé à … » / « Envoi impossible : … » (avec « Réessayer »). Trois gestes max : e-mail → Envoyer. `send_receipt=true`.
- **Tickets du jour → détail** : « Envoyer par e-mail » (pré-rempli si client lié), et affichage du client lié (e-mail masqué partiellement : `j***@exemple.fr`).
- **Admin → onglet Clients** : recherche par e-mail/nom, liste, fiche (consentements avec date/source, envois, tickets), boutons « Retirer de la newsletter » / « Inscrire (demande orale) », « Exporter (RGPD) » (affiche le JSON), « Supprimer les données (RGPD) » avec motif et double confirmation.
- **Admin → carte « E-mail & newsletter »** : état fournisseur, suivi anonyme, liste Brevo configurée, webhook configuré, aucun secret.
- `app_settings.shop` gagne `dpo_email` (formulaire admin).

---

## 6. Propriété des fichiers

| Agent | Écrit uniquement dans |
|---|---|
| **D — Backend client/e-mail** (Sonnet) | `app/models/{client,consent,communication}.py`, `alembic/versions/0003_clients_email.py`, `app/models/pos.py` (+`client_id`), `app/services/{email_gateway,receipt_email,brevo_contacts,client_service}.py`, `app/services/pos.py` (méthode d'orchestration), `app/api/pos/router.py` (2 routes), `app/api/admin/router.py` (clients, messaging/status, `dpo_email`), `app/api/brevo/router.py`, `app/core/config.py` + `.env.example` (clés E10), `app/services/jet.py` (constantes), `app/version.py`, `main.py` (router brevo), `tests/test_{clients,consents,receipt_email,email_gateway,brevo_contacts,brevo_webhook,rgpd,pos_client_link}.py`, `conftest.py` (TRUNCATE) |
| **E — Front** (Sonnet) | `apps/web/**` |
| Orchestrateur | `docs/`, `README.md`, `CLAUDE.md`, revue, PR |

Tests : PostgreSQL uniquement ; HTTP Brevo/SMTP mockés (`httpx.MockTransport` injecté, `smtplib` monkeypatché). Faux serveur Brevo local pour le testeur (comme le faux SumUp).

---

## 7. Tests exigés

Migration 0003 sur base vide ; `client_id` modifiable sur une vente signée mais **aucune autre colonne** ; `consents` immuables (trigger) ; upsert e-mail normalisé ; consentement idempotent ; révocation via webhook (token absent → 403, mauvais → 403, bon → consentement révoqué + JET) ; anonymisation (E4 : PII effacées, transactions intactes, `verify_chain_integrity` toujours valide, contact retiré de la liste — appel Brevo vérifié par le mock) ; e-mail ticket : Brevo appelé avec `api-key`, `sender`, `to`, sujet, HTML contenant le ticket, refus sans `BREVO_ANONYMOUS_TRACKING` → repli SMTP/simulation et statut cohérent ; communication tracée ; JET pour chaque événement ; export JSON complet ; aucun secret dans `/admin/messaging/status` ; isolation Vintiz.

Personas : vendeuse (saisie e-mail + case en ≤ 3 gestes, « Passer », e-mail invalide, ticket envoyé, renvoi depuis tickets du jour), testeur (faux Brevo : ticket reçu par le faux serveur, contact poussé dans la liste avec les bons `listIds`, webhook de désabonnement rejoué, anonymisation), revue debug & sécurité + RGPD par l'orchestrateur.
