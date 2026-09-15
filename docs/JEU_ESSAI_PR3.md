# Jeu d'essai PR3 — client, e-mail, newsletter, RGPD (valeurs attendues)

Rejoué par le testeur (API + front, faux serveur Brevo local) et la persona
vendeuse. Pré-requis : caisse ouverte, réglages boutique renseignés
(nom, adresse, SIRET, n° TVA, `dpo_email`), `BREVO_API_KEY` factice,
`BREVO_LIST_ID=42`, `BREVO_WEBHOOK_TOKEN=secret-test`,
`BREVO_ANONYMOUS_TRACKING=true`, `BREVO_API_BASE` → faux serveur.

| # | Action | Attendu |
|---|---|---|
| 1 | Vente A espèces 25,00 ; écran succès : e-mail `Marie.Dupont@Example.org`, case newsletter **cochée**, « Envoyer le ticket » | client créé `marie.dupont@example.org` ; consentement `newsletter` granted, source `pos`, policy `2026-09` ; `transactions.client_id` posé ; e-mail reçu par le faux Brevo (`POST /v3/smtp/email`, sujet « Votre ticket … n° 1 », HTML contenant le ticket, mentions légales, aucune image) ; communication `sent/brevo` ; contact poussé `POST /v3/contacts` avec `listIds: [42]`, `updateEnabled: true`, sans `emailBlacklisted` ; JET `client.created`, `consent.granted`, `client.linked`, `receipt.emailed`, `brevo.synced` |
| 2 | Vente B CB 54,00 ; e-mail `paul@example.org`, case **non cochée** | client créé ; **aucun** consentement enregistré ; e-mail envoyé ; **aucun** appel `/v3/contacts` (E2) |
| 3 | Vente C ; « Passer » | aucune donnée client, aucun e-mail, aucun événement client |
| 4 | Tickets du jour → ticket n° 1 → « Envoyer par e-mail » | e-mail pré-rempli, second envoi, `duplicate_count = 1`, JET `receipt.duplicate` + `receipt.emailed` |
| 5 | Vente D, e-mail `marie.dupont@example.org` (existante), case cochée | pas de doublon client, pas de nouvelle ligne de consentement (idempotent), vente liée |
| 6 | Vente E, même e-mail, case **décochée** | nouvelle ligne `granted=false`, source `pos`, `newsletter_optin=false`, `POST /v3/contacts/lists/42/contacts/remove` reçu |
| 7 | Webhook Brevo `unsubscribed` pour `paul@example.org` sans token | 403, rien ne change |
| 8 | Webhook `unsubscribed` pour `marie.dupont@example.org` avec `?token=secret-test` (après l'avoir réinscrite en admin) | consentement révoqué source `webhook`, JET `brevo.webhook_received` + `consent.revoked` |
| 9 | Admin → fiche Marie → export | JSON contenant fiche, historique des consentements (3 lignes : oui/non/oui puis révocation webhook = 4), 3 tickets liés avec texte, envois ; JET `client.exported` |
| 10 | Admin → Marie → « Supprimer les données » motif « demande client » | `email = supprime-<uuid>@anonyme.invalid`, noms vides, `anonymized_at` posé, ligne de consentement `granted=false` source `rgpd`, appel `contacts/lists/42/contacts/remove`, tickets intacts (`verify_chain_integrity` valide, `client_id` inchangé), JET `client.anonymized` ; nouvelle vente avec `marie.dupont@example.org` → nouveau client distinct |
| 11 | `UPDATE consents SET granted = true` / `DELETE FROM consents` (psql) | refusés (trigger) |
| 12 | `UPDATE transactions SET client_id = NULL WHERE transaction_number = 1` | autorisé ; `SET total_ttc = 0` → refusé « NF525 » |
| 13 | `GET /admin/messaging/status` | `provider: brevo`, `anonymous_tracking: true`, aucune clé dans la réponse |
| 14 | Redémarrer l'API **sans** `BREVO_ANONYMOUS_TRACKING` | envoi → repli SMTP (non configuré) → `simulated` ; front affiche l'avertissement ; aucun appel Brevo |
| 15 | E-mail invalide `marie@` | 422, bouton désactivé côté front |
