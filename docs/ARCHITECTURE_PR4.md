# PR4 — Architecture : exports comptables, archive fiscale, clôtures périodiques, attestation

**Statut :** contrat d'architecture de l'orchestrateur, opposable aux agents. Empilé sur PR3b.
**Périmètre CDC :** §2.1 Export comptable (journal des ventes + journal de caisse CSV pour Pennylane / Talenz Alteis), §3.1 conditions 3 (conservation : clôtures journalières, mensuelles, annuelles, grand total perpétuel) et 4 (archivage : archive exportable, datée, signée, lisible hors application), §5.2 PR4, §7 livrables 4 (auto-attestation) et 5 (guide vendeur + procédure de clôture).
**Décision Julien :** le format CSV est **exactement** celui de l'application source (colonnes `_PENNYLANE_CSV_COLUMNS`, FEC 18 colonnes), sans intégration API Pennylane (l'import se fait par fichier).

Source d'extraction : modules équivalents de l'application source `services/accounting_service.py` (lignes d'écriture par Z, CSV mensuel, FEC), `services/fiscal_closure.py`, `services/fiscal_export.py`, `services/z_report_pdf.py`, `api/accounting/router.py`, `api/admin/fiscal_closures.py`, `api/admin/database.py` (export CSV de tables).

---

## 1. Décisions structurantes

| # | Décision | Conséquence |
|---|---|---|
| F1 | Configuration comptable en base (`app_settings.accounting`), **valeurs par défaut identiques à l'application source** : `journal_code="VTE"`, `account_sales="707100"` (« Ventes marchandises »), `account_tva="44571"` (« TVA collectée 20% »), `account_cash="531000"` (« Caisse »), `account_card="512000"` (« CB SumUp »), `account_rounding_expense="658000"`, `account_rounding_income="758000"`. Modifiable en admin, journalisée au JET. | Pas de Pennylane API, pas de clé. |
| F2 | **Une écriture comptable par Z**, générée dans la **même transaction SQL** que la clôture (`close_drawer` et garde 23:59) : débit encaissements par mode (espèces, CB) nets des remboursements ; crédit ventes HT nettes (707) ; crédit TVA collectée nette (44571) ; ligne d'ajustement d'arrondi 658/758 si Σdébit ≠ Σcrédit (écart > 1 € = erreur journalisée mais écriture équilibrée). `PieceRef` = `Z{report_number:04d}`. | Tables `accounting_exports` (1 par Z, unique) + `accounting_export_lines`, **immuables** (trigger UPDATE/DELETE). Régénération = recalcul et comparaison, jamais réécriture ; divergence → JET `accounting.mismatch`. |
| F3 | **CSV mensuel Pennylane** : colonnes, ordre, format des montants (virgule décimale), dates `JJ/MM/AAAA`, `\r\n`, regroupement par n° de pièce Z — **copie fidèle** de `generate_monthly_csv`. **FEC** : 18 colonnes tabulées de l'application source, `EcritureNum = {report_number:04d}-{i:03d}`, FEC journalier et mensuel. | Réutiliser le code source des fonctions `_csv_row/_csv_amount/_csv_date/_fec_row/_fec_date` à l'identique. |
| F4 | **Exports bruts CSV** (journal des ventes / journal de caisse détaillés) : `transactions`, `transaction_items`, `payments`, `z_reports`, `cash_movements`, `cash_drawers`, `journal_events` — mécanisme de l'application source (`export_table_csv`, liste blanche), filtre `?from&to` sur `created_at`, UTF-8 BOM, `;` séparateur, dates Europe/Paris. | Aucune table client/PII exportable ici (RGPD : l'export client existe déjà par fiche). |
| F5 | **Clôtures périodiques** (mensuelle le 1er à 00:15, annuelle le 1er janvier à 00:30, Europe/Paris, + manuelle) : extraites de `fiscal_closure.py` — verrou fiscal, refus si caisse ouverte (409), **vérification des deux chaînes** (transactions, Z) + JET avant scellé (409 si rompue), snapshot JSON canonique **gzip `mtime=0`** contenant transactions/lignes/paiements/Z/**mouvements de caisse**/**JET de la période**/tickets (texte)/réglages boutique, `archive_sha256`, manifeste HMAC chaîné (`previous_hash` = clôture précédente), grand total de période et **total perpétuel**, `software_version` + `FISCAL_VERSION_DATE`, notice française auto-descriptive. | Table `fiscal_closures` (trigger : toute UPDATE/DELETE interdite). Échec d'un cron → JET `system.job_failed` **et** e-mail d'alerte à `shop.email` si la passerelle e-mail est disponible. |
| F6 | **Export fiscal à la demande** (`format=json|xml`, période) : extrait de `fiscal_export.py`, **vérifie les chaînes avant de servir** (409 si invalide), inclut le JET et les mouvements, `generated_at` **hors** du corps signé : le corps est reproductible et son `sha256` renvoyé en en-tête `X-Export-SHA256`. | Corrige A-1/A-5. |
| F7 | **Chaque export ou téléchargement est journalisé** : JET `export.downloaded` `{kind, period_start, period_end, sha256, rows}` (CSV mensuel, FEC, bruts, archive, export fiscal, PDF Z). | Corrige S-3. |
| F8 | **PDF du Z** (reportlab, extrait de `z_report_pdf.py`) : en-tête boutique, totaux, ventilation par mode, caisse (fond/attendu/compté/écart), mouvements, cumuls perpétuels, hash + previous_hash, mention D14 (« auto-attestation art. 286 I-3° bis CGI, version fiscale X du JJ/MM/AAAA »), jamais « conforme NF525 ». Généré à la demande de façon **déterministe** (pas d'horodatage de génération dans le contenu) ; `sha256` dans le JET. | |
| F9 | **Attestation** : `docs/ATTESTATION_NF525.md` (à signer par Julien Gondé, Président de Frip & Co) décrivant les mécanismes réels (chaînage v3, JET, triggers, rôles PostgreSQL, clôtures, archive, exports), la version fiscale, **les limites déclarées** (colonne `client_id` mutable, `printed_count/duplicate_count/printed_at` mutables, HMAC à clé secrète → vérification par un tiers via la clé sous scellé, horloge du VPS, sauvegarde/archives sur le même hôte à copier hors site) et la procédure de conservation 6 ans. | Rédigé par un agent à partir des contrats PR2–PR4, revu par l'orchestrateur. |
| F10 | **Guide vendeur (1 page)** et **procédure de clôture** : `docs/GUIDE_VENDEUR.md`, `docs/PROCEDURE_CLOTURE.md`, en français, sans jargon, à partir des parcours validés par la persona. | |

`app/version.py` : `APP_VERSION="0.5.0"`, `EXPECTED_DB_REVISION="0005"`, `FISCAL_SIGNATURE_VERSION=3`, `FISCAL_VERSION_DATE` inchangée.

---

## 2. Modèle de données (migration `0005_accounting_closures`)

### `accounting_exports`
`id`, `z_report_id` FK unique not null, `export_date` date (jour du Z, Europe/Paris), `total_sales_ht`, `total_tva`, `total_ttc`, `total_refunds_ttc`, `total_cash`, `total_card` numeric(12,2), `total_debit`, `total_credit` numeric(12,2), `rounding_adjustment` numeric(12,2), `fec_content` text (FEC de ce Z, généré à la création), `created_at`. Trigger : UPDATE/DELETE interdits.

### `accounting_export_lines`
`id`, `export_id` FK, `line_number` int, `account_number` varchar(20), `account_label` varchar(100), `label` varchar(255), `debit`, `credit` numeric(12,2), `piece_reference` varchar(32) (= `Z0001`). Trigger : INSERT interdit si l'export parent existe déjà avec des lignes… (simplifié : UPDATE/DELETE interdits).

### `fiscal_closures`
Extrait de l'application source : `sequence_number` int unique, `closure_type` enum `monthly|annual|manual`, `period_start`, `period_end` (timestamptz, bornes Europe/Paris), `software_version`, `fiscal_version_date`, `transaction_count`, `first/last_transaction_number` nullable, `last_transaction_hash`, `first/last_z_number`, `last_z_hash`, `jet_last_seq`, `jet_last_hash`, `grand_total_sales/refunds/net` numeric(12,2), `perpetual_sales/refunds/net` numeric(12,2), `perpetual_transaction_count`, `archive_sha256` varchar(64), `archive_content` bytea (gzip), `archive_size`, `manifest` JSONB, `previous_hash`, `hash`, `signature_version`, `closed_by_user_id` FK nullable, `created_at`. Trigger : UPDATE/DELETE interdits.

---

## 3. Services

- `services/accounting_service.py` (extrait, réduit) : `get_config()` (depuis `app_settings.accounting`, défauts F1), `build_journal_lines(z, cfg)` (F2, logique identique : débit par mode net, crédit 707 net, crédit 44571 net, ajustement 658/758), `create_export_for_z(db, z)` (idempotent : renvoie l'existant), `generate_monthly_csv(year, month)` (F3, **texte identique** à l'application source), `generate_fec(export)` / `generate_daily_fec(date)` / `generate_monthly_fec(year, month)`, `verify_export(z)` (recalcul et comparaison → `accounting.mismatch`).
- `services/table_export.py` (extrait de `database_backup.export_table_csv`) : liste blanche F4, streaming CSV.
- `services/fiscal_closure.py` (extrait, étendu F5), `services/fiscal_export.py` (extrait, étendu F6), `services/z_report_pdf.py` (extrait, F8 ; dépendance `reportlab` ajoutée au `pyproject.toml`).
- `services/pos.py::close_drawer` et `fiscal.py::close_open_drawers` : appellent `create_export_for_z` dans la même transaction (F2).
- `app/jobs.py` : `monthly_fiscal_closure`, `annual_fiscal_closure` (+ garde existante) ; échec → JET `system.job_failed` + e-mail d'alerte best effort.

JET PR4 : `accounting.export_created`, `accounting.mismatch`, `closure.created` `{type, sequence, period, sha256}`, `closure.failed`, `export.downloaded`, `fiscal.integrity_checked` (existant).

---

## 4. API (JWT)

| Méthode | Route | Corps / réponse |
|---|---|---|
| GET/PUT | `/admin/settings/accounting` | F1 (validation : comptes numériques 6–8 chiffres, journal 1–5 caractères) |
| GET | `/admin/accounting/exports?year&month` | liste des écritures du mois (Z, date, totaux, équilibre) |
| GET | `/admin/accounting/exports/{z_id}` | écriture détaillée (lignes) |
| GET | `/admin/accounting/monthly-csv/{year}/{month}` | `text/csv` (Pennylane), `Content-Disposition: ecritures_{YYYY}-{MM}.csv`, JET |
| GET | `/admin/accounting/fec/day/{date}` , `/admin/accounting/fec/month/{year}/{month}` | `text/plain` (FEC tabulé), nom `FEC_{SIREN}_{période}.txt` (SIREN = 9 premiers chiffres du SIRET), JET |
| GET | `/admin/exports/table/{table}?from&to` | CSV brut (F4), JET |
| GET | `/admin/fiscal-closures` | liste (séquence, type, période, totaux, sha256, taille) |
| POST | `/admin/fiscal-closures` | `{closure_type: "monthly"|"annual"|"manual", period_start, period_end}` → 201 / 409 (`drawer_open`, `chain_invalid`, `already_closed`) |
| GET | `/admin/fiscal-closures/{id}` | détail + manifeste |
| GET | `/admin/fiscal-closures/{id}/archive` | `application/gzip`, en-têtes `X-Archive-SHA256`, `X-Closure-Hash`, JET |
| GET | `/admin/fiscal-closures/integrity` | vérification de la chaîne des clôtures |
| GET | `/admin/fiscal-export?from&to&format=json|xml` | F6, `X-Export-SHA256`, 409 si chaîne invalide, JET |
| GET | `/pos/z-reports/{id}/pdf` | `application/pdf` (F8), JET |
| GET | `/admin/fiscal/integrity` | existant, étendu aux clôtures et aux exports comptables |

Erreurs : `PosServiceError` → `{detail, code}`.

---

## 5. Front (admin)

- Onglet **Comptabilité** : réglages des comptes (F1), sélecteur mois → « Télécharger le CSV Pennylane », « Télécharger le FEC du mois », liste des écritures du mois (Z, date, débit = crédit, ✔/⚠), détail d'une écriture, exports bruts (table + période → CSV).
- Onglet **Archives fiscales** : liste des clôtures (type, période, séquence, totaux, total perpétuel, SHA-256 court), « Clôturer maintenant » (manuelle : période libre avec garde-fous, double confirmation), « Télécharger l'archive » (+ affichage du SHA-256 pour vérification hors ligne), « Vérifier l'intégrité ».
- Liste des Z : bouton « PDF ».
- Téléchargements binaires via `api.getBytes` existant ; aucun jargon technique inutile (dire « archive fiscale », « écritures comptables », « fichier FEC », « empreinte SHA-256 »).

---

## 6. Propriété des fichiers

| Agent | Écrit uniquement dans |
|---|---|
| **K — Backend exports/archives** (Sonnet) | `apps/api/**` (modèles, migration 0005, services, routes, jobs, `pyproject.toml` +reportlab, tests) |
| **L — Front admin** (Sonnet) | `apps/web/**` |
| **M — Documents** (Sonnet) | `docs/ATTESTATION_NF525.md`, `docs/GUIDE_VENDEUR.md`, `docs/PROCEDURE_CLOTURE.md` |
| Orchestrateur | `docs/ARCHITECTURE_PR4.md`, `README.md`, `CLAUDE.md`, `DEPLOIEMENT.md`, revue, PR |

---

## 7. Tests exigés (PostgreSQL)

Écriture par Z équilibrée sur le jeu d'essai PR2 (Z1 : débit caisse 47,00 + CB 25,00 = 72,00 ; crédit 707 60,00 + 44571 12,00 ; ajustement 0) ; CSV mensuel **octet pour octet** conforme à un fichier attendu (fixture) ; FEC 18 colonnes, `EcritureNum` séquentiel, équilibre Σdébit = Σcrédit ; exports bruts filtrés, sans PII ; clôture mensuelle : refus caisse ouverte, refus chaîne rompue, archive gzip reproductible (deux générations = même sha256), manifeste vérifiable, total perpétuel cumulé sur deux clôtures, JET et mouvements inclus, triggers ; export fiscal : 409 si chaîne invalide, sha256 stable ; PDF Z déterministe (même sha256 à deux appels), contient les totaux et la mention D14, pas « conforme NF525 » ; chaque téléchargement → JET `export.downloaded` ; crons : échec journalisé ; `verify_export` détecte une divergence simulée.

Personas : comptable Talenz (rejoue le jeu d'essai PR2 sur deux mois, importe mentalement le CSV : équilibre, comptes, pièces, TVA ; relit le FEC ; vérifie l'archive hors application : décompression, SHA-256, totaux = Z), vendeuse (procédure de clôture et guide 1 page compréhensibles), testeur (API + front, téléchargements réels, sha256 comparés).
