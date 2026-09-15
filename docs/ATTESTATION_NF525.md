# Attestation individuelle de conformité — Logiciel de caisse « Frip & Co Street »

Attestation établie en application de l'article 286 I-3° bis du Code général
des impôts et de la doctrine BOI-TVA-DECLA-30-10-30, relative aux logiciels
et systèmes de caisse qui enregistrent les paiements de clients non
assujettis à la TVA. Le régime retenu est l'**auto-attestation par
l'éditeur-utilisateur** : Frip & Co développe et exploite ce logiciel pour
son usage exclusif, il n'est ni vendu ni mis à disposition d'un tiers. Cette
attestation n'emploie à aucun endroit la mention « logiciel certifié » ou
« conforme NF525 » : le code applicatif applique la même règle (voir §3.4).

**Éditeur et utilisateur :** Frip & Co (groupe Solidarité Textiles).
**Signataire :** Julien Gondé, Président de Frip & Co.
**Date d'établissement :** voir bloc de signature, §6.

Cette attestation est établie à partir de l'examen direct du code source du
logiciel, arrêté à la date de rédaction sur la branche
`claude/pr4-exports-archive-attestation`. Chaque affirmation technique
renvoie au fichier (et, autant que possible, aux lignes) qui la porte, afin
qu'elle reste vérifiable et qu'elle soit revue à chaque évolution du
mécanisme fiscal.

---

## 1. Identification du logiciel

| Élément | Valeur | Référence |
|---|---|---|
| Nom commercial | Logiciel de caisse Frip & Co Street | — |
| Version applicative | `APP_VERSION` (voir la valeur courante dans le fichier cité) | `apps/api/app/version.py` |
| Version fiscale | `FISCAL_SIGNATURE_VERSION` | `apps/api/app/version.py` |
| Date de la version fiscale | `FISCAL_VERSION_DATE` | `apps/api/app/version.py` |
| Version de signature du journal des événements techniques (JET) | `JET_SIGNATURE_VERSION` | `apps/api/app/version.py` |
| Révision de schéma de base de données attendue | `EXPECTED_DB_REVISION` | `apps/api/app/version.py` ; contrôlée au démarrage de l'API, qui refuse de démarrer si la révision réellement appliquée (`alembic_version`) diffère |
| Dépôt et déploiement | Dépôt Git dédié, déploiement sur VPS isolé (réseau, base et secrets propres) | `docs/DEPLOIEMENT.md` |

À la date de rédaction de la présente attestation, les migrations Alembic
appliquées vont de `0001` à `0005` (`apps/api/alembic/versions/`), la
migration `0005_accounting_closures.py` ayant introduit les écritures
comptables et les clôtures périodiques (voir §3.3 et §3.4). Les services
correspondants (`accounting_service.py`, `fiscal_closure.py`,
`fiscal_export.py`, `table_export.py`, `z_report_pdf.py`), les crons de
clôture périodique et les routes d'administration et de caisse qui les
exposent sont, à cette date, tous écrits et raccordés (voir §3.3 et §3.4
pour le détail vérifié). Toute évolution du mécanisme de signature fiscale
(payload signé, algorithme, colonnes couvertes) est une évolution fiscale
majeure : elle impose un incrément de `FISCAL_SIGNATURE_VERSION`, une
nouvelle `FISCAL_VERSION_DATE`, et une mise à jour de la présente
attestation avant mise en production (`CLAUDE.md`, règle « Chaîne
fiscale »).

## 2. Périmètre couvert

Le logiciel couvre l'intégralité du cycle d'encaissement d'une boutique
mono-poste, mono-utilisateur :

- **Vente** : panier de lignes libres (libellé + prix TTC, sans gestion de
  stock ni de catalogue produit), remise globale en euros ou en pourcentage,
  taux de TVA paramétrable, encaissement espèces, carte bancaire (terminal
  SumUp Solo) ou mixte.
- **Annulation** : annulation totale d'une vente, jamais de suppression ni
  de modification directe.
- **Caisse espèces** : ouverture avec fond de caisse, mouvements d'entrée et
  de sortie en cours de journée, clôture avec comptage et rapport Z.
- **Ticket client** : génération, impression physique (imprimante ESC/POS
  réseau ou USB tablette), envoi par e-mail, réimpression et renvoi tracés.
- **Paramétrage boutique** : coordonnées, taux de TVA, mentions du ticket,
  matériel — chaque modification journalisée.
- **Clôtures périodiques, archivage et exports comptables** : voir §3.3 et
  §3.4.

Sont explicitement **hors périmètre** : gestion de stock/catalogue produit,
programme de fidélité, facturation B2B, avoir, coupons, personal shopper.
Aucune de ces fonctions ne participe donc à la chaîne de preuve décrite
ci-dessous.

## 3. Démonstration des quatre conditions (art. 286 I-3° bis du CGI)

### 3.1 Inaltérabilité

**Mécanisme.** Chaque vente et chaque annulation est scellée par une
signature HMAC-SHA256 chaînée (« signature v3 »), calculée et vérifiée par
`FiscalService` (`apps/api/app/services/fiscal.py`). La clé de signature
(`FISCAL_SIGNING_KEY`) est un secret d'environnement, distinct par
déploiement (`apps/api/app/core/config.py`).

**Champs couverts par la signature d'une transaction** (méthode
`_transaction_payload`, `fiscal.py:158-245`) :

- Transaction : identifiant, numéro, type (vente/annulation), horodatage de
  création, opérateur, identifiant d'idempotence du panier, transaction
  d'origine et motif (pour une annulation), type et valeur de la remise
  globale, montant de remise effectivement appliqué, taux de TVA, totaux
  HT/TVA/TTC.
- Chaque ligne : position, libellé, quantité, prix unitaire, part de remise
  ventilée, total de ligne, taux de TVA, HT et TVA de la ligne, ligne
  d'origine (pour une annulation).
- Chaque paiement : méthode, montant, montant remis et rendu (espèces),
  6 références SumUp (identifiant de checkout, identifiant et code de
  transaction, code d'autorisation, marque et 4 derniers chiffres de la
  carte).
- Le hash de la transaction précédente (`previous_hash`) et la version de
  signature.

**Chaînage et genesis.** Chaque transaction porte le hash de la précédente ;
la toute première transaction référence le genesis conventionnel `"0"`
(`GENESIS_HASH`, `fiscal.py:31`). `sign_transaction` (`fiscal.py:130-147`)
calcule le hash après que les lignes et paiements ont été ajoutés et
flushés (donc avec leurs identifiants et horodatages définitifs), et écrit
la ligne déjà scellée en une seule opération — aucune transaction n'existe
en base sans être signée dans la foulée. `verify_chain_integrity`
(`fiscal.py:247-279`) recalcule chaque hash et vérifie le maillage
`previous_hash` sur l'ensemble de la chaîne ; comparaison à temps constant
(`hmac.compare_digest`).

**Annulation, jamais modification.** Une vente ne peut être ni corrigée ni
supprimée. Son annulation crée une **nouvelle transaction** de type
`refund`, référençant l'originale (`original_transaction_id`), avec un
motif obligatoire (au moins 3 caractères) et ses propres lignes et
paiements « miroir » (`RefundService.cancel_transaction`,
`apps/api/app/services/refund.py:80-237`). Aucun endpoint de suppression ou
de modification d'une vente signée n'existe. Quand un paiement d'origine
est en carte, le remboursement est d'abord demandé à SumUp ; ce n'est qu'en
cas de succès que l'écriture inverse locale est produite (`refund.py:133-149`)
— un refus SumUp ne laisse aucune trace locale.

**Triggers PostgreSQL d'immuabilité** (un trigger `BEFORE UPDATE OR DELETE`,
ou `BEFORE INSERT OR UPDATE OR DELETE` selon la table, par table protégée) :

| Table | Trigger | Fonction | Règle |
|---|---|---|---|
| `transactions` | `trg_protect_signed_transaction` | `fripco_protect_signed_transaction()` | Dès que `hash_chain <> ''`, toute colonne du payload signé est figée ; seules `client_id` et `updated_at` restent mutables (voir §4) ; suppression toujours interdite | `apps/api/alembic/versions/0002_pos_fiscal.py:349-389`, réécrite (`CREATE OR REPLACE`) par `0003_clients_email.py:196-231` pour exempter `client_id` |
| `transaction_items` | `trg_protect_transaction_items` | `fripco_protect_fiscal_child()` | INSERT/UPDATE/DELETE interdits dès que la transaction parente est signée | `0002_pos_fiscal.py:395-413` |
| `payments` | `trg_protect_payments` | `fripco_protect_fiscal_child()` | Idem | `0002_pos_fiscal.py:414-419` |
| `z_reports` | `trg_protect_z_report` | `fripco_protect_z_report()` | UPDATE/DELETE toujours interdits, sans exception : un Z est scellé dès sa création | `0002_pos_fiscal.py:420-434` |
| `cash_drawers` | `trg_protect_cash_drawer` | `fripco_protect_cash_drawer()` | Une fois `closed_at` et `z_report_id` renseignés, plus aucune modification ; suppression toujours interdite | `0002_pos_fiscal.py:435-455` |
| `cash_movements` | `trg_protect_cash_movement` | `fripco_protect_cash_movement()` | UPDATE/DELETE toujours interdits (append-only) | `0002_pos_fiscal.py:456-470` |
| `receipts` | `trg_protect_receipt` | `fripco_protect_receipt()` | Suppression interdite ; contenu et transaction figés — seuls `duplicate_count`, `printed_count` et `printed_at` restent mutables (compteurs de relecture/réimpression, voir §4) | `0002_pos_fiscal.py:471-493`, étendue par `0004_receipts_printed.py:28-66` |
| `consents` | `trg_protect_consent` | `fripco_protect_consent()` | UPDATE/DELETE toujours interdits (registre de consentement append-only) | `0003_clients_email.py:174-189` |
| `journal_events` | `trg_protect_journal_event` | `fripco_protect_journal_event()` | UPDATE/DELETE toujours interdits (voir §3.2) | `0001_initial.py` |

Chaque migration exécute chaque instruction SQL (création de fonction,
suppression puis création de trigger) séparément, contrainte du pilote
`asyncpg` (une seule commande top-level par appel) documentée dans chacune
d'elles. Les fonctions `downgrade()` des quatre migrations lèvent
`NotImplementedError` : une régression sur ces triggers ne peut se faire par
un downgrade Alembic, seulement par restauration d'une sauvegarde
antérieure — décision délibérée pour qu'aucune commande d'exploitation
courante ne puisse désactiver la chaîne de preuve.

**Rôle applicatif non propriétaire.** Au premier démarrage du conteneur de
base de données, `docker/db-init/01_roles.sh` crée un rôle applicatif dédié
(`FRIPCO_APP_USER`, par défaut `fripco_app`) avec uniquement les droits
`SELECT/INSERT/UPDATE/DELETE` sur les tables et `USAGE/SELECT` sur les
séquences, sans droit `CREATE` sur le schéma. C'est ce rôle, et non le rôle
propriétaire des migrations (`POSTGRES_USER`), que l'API utilise en
production (`DATABASE_URL`). Un rôle applicatif ordinaire ne peut ni
`ALTER TABLE ... DISABLE TRIGGER`, ni supprimer une fonction ou un trigger :
les triggers d'immuabilité lui sont donc opposables — l'application ne peut
pas, même par erreur de code, contourner ses propres protections. Les
migrations elles-mêmes (et donc la création des triggers) sont exécutées
sous le rôle propriétaire (`MIGRATION_DATABASE_URL`), jamais sous le rôle
applicatif (`docs/DEPLOIEMENT.md` §4).

**Verrouillage de la numérotation.** Le numéro de transaction (`MAX+1`) et
la génération du rapport Z sont protégés par un **verrou consultatif
PostgreSQL unique**, partagé entre les ventes, les annulations et la
clôture de caisse (`FISCAL_WRITE_LOCK_KEY = 5_252_026`,
`fiscal.py:33-37, 83-84` ; acquis par `PosService.create_transaction`,
`RefundService.cancel_transaction` et `PosService.close_drawer`,
`apps/api/app/services/pos.py:136, 552`, `refund.py:102`). Aucune vente ne
peut donc s'intercaler entre le calcul d'un Z et sa signature.

### 3.2 Sécurisation

**Journal des événements techniques (JET).** Table `journal_events`
(migration `0001_initial.py`), chaînée par HMAC-SHA256 sur le même modèle
que la chaîne de vente (genesis `"0"`, `previous_hash`), écrite par
`JournalService` (`apps/api/app/services/jet.py`). Un verrou consultatif
dédié (`_JET_ADVISORY_LOCK_KEY = 837_120_001`, `jet.py:32`) sérialise
l'attribution des numéros de séquence. La ligne est insérée déjà scellée
(`record`, `jet.py:102-150`) ; `verify_chain` (`jet.py:164-197`) recalcule
chaque hash et le maillage. **Aucune exception n'est interceptée** : un
échec d'écriture du JET fait échouer la requête plutôt que de laisser
passer un événement de sécurité non journalisé (`jet.py:9-12, 112-117` ;
règle également énoncée dans `CLAUDE.md`).

**Ce qui est journalisé** (constantes `EVENT_*`, `jet.py:40-89`) :

- **Connexions** : succès, échec, blocage par limite de tentatives et
  déconnexion (`auth.login_success`, `auth.login_failed`,
  `auth.login_rate_limited`, `auth.logout`, `auth.token_refresh`),
  effectivement appelées depuis `apps/api/app/api/auth/router.py:79-204`.
- **Ventes et caisse** : création et annulation de vente, ouverture,
  clôture et clôture automatique d'un tiroir, mouvement de caisse,
  régularisation, cycle du paiement carte (initié/payé/refusé/annulé),
  duplicata de ticket.
- **Configuration** : chaque écriture d'un paramètre boutique
  (`config.changed`), journalisée dans la même transaction SQL que
  l'écriture, avec le diff avant/après (`SettingsService.set`,
  `apps/api/app/services/settings_service.py:78-123`).
- **Impression et tiroir** : impression physique d'un ticket, ouverture du
  tiroir, imprimante injoignable (`receipt.printed`, `drawer.kicked`,
  `printer.unreachable`).
- **Client et RGPD** (périmètre e-mail/newsletter) : création, mise à jour
  et rattachement d'un client, consentement accordé/révoqué, envoi ou échec
  d'envoi du ticket par e-mail, synchronisation Brevo, anonymisation,
  export RGPD — sans jamais porter l'e-mail ni le nom en clair dans le
  payload JET (`apps/api/app/services/client_service.py:190,
  246-254` : seuls l'identifiant client et un hash de corrélation sont
  journalisés).
- **Défaillances techniques** : échec d'un job planifié
  (`system.job_failed`), utilisé par la garde fiscale 23:59
  (`apps/api/app/jobs.py`, voir §3.3) — jamais avalé en silence.
- **Vérifications d'intégrité** : chaque contrôle de la chaîne de vente
  lancé depuis l'administration (`fiscal.integrity_checked`).

**Exports et téléchargements.** Aucun export ou téléchargement (CSV
comptable, FEC, export brut, archive de clôture, export fiscal à la
demande, PDF du Z) n'a lieu sans une écriture JET `export.downloaded`
correspondante. Le helper commun `_record_export_downloaded`
(`apps/api/app/api/admin/router.py:344-364`) journalise le type d'export,
la période, l'empreinte SHA-256 du contenu servi et le nombre de lignes ; il
est appelé par les six routes de téléchargement de l'administration (CSV
mensuel, FEC journalier, FEC mensuel, export de table brute, archive de
clôture, export fiscal JSON/XML — `admin/router.py:478, 505, 531, 579, 663,
735`), et le téléchargement du PDF d'un Z l'appelle indépendamment depuis
`apps/api/app/api/pos/router.py:937-976`. Les autres constantes
d'événement PR4 (`accounting.export_created`, `accounting.mismatch`,
`closure.created`, `closure.failed`) sont déclarées dans
`apps/api/app/services/jet.py` et écrites respectivement par
`AccountingService.create_export_for_z`/`verify_export`
(`apps/api/app/services/accounting_service.py:247-360`) et
`FiscalClosureService.close_period`
(`apps/api/app/services/fiscal_closure.py`).

**Accès.** Authentification JWT obligatoire sur l'ensemble des routes
métier ; les routes de paramétrage, d'export et de clôture sont réservées
au compte manager. Le premier (et unique) compte manager est créé par
`scripts/create_manager.py`, qui refuse la création d'un second compte —
le logiciel est mono-utilisateur par construction. Aucun identifiant n'est
livré par défaut.

### 3.3 Conservation

**Rapport Z journalier scellé.** `FiscalService.generate_z_report`
(`fiscal.py:285-538`) est appelé sous le verrou fiscal unique décrit en
§3.1, immédiatement à la clôture d'un tiroir
(`PosService.close_drawer`, `pos.py:544-569`). Le Z est **scellé à la
création**, sans étape de verrouillage séparée : la signature couvre non
seulement les totaux de vente (ventes, annulations, net, HT, TVA, nombre de
transactions, premier et dernier numéro, dernier hash de vente couvert),
mais aussi les **montants de caisse** (fond d'ouverture, montant compté,
montant attendu, écart), les **mouvements de caisse** de la période
(liste complète, avec direction, montant, motif et horodatage) et les
**cumuls perpétuels** (ventes, remboursements, net et nombre de
transactions, cumulés depuis le Z précédent — `fiscal.py:457-489`). Le Z
est lui-même chaîné au Z précédent (`previous_hash`) **et** au dernier hash
de vente qu'il couvre (`last_transaction_hash`), et son trigger PostgreSQL
n'admet **aucune** exception de modification, contrairement à
`transactions` (§3.1). `verify_z_chain_integrity` (`fiscal.py:540-647`)
recalcule chaque hash, vérifie le maillage, **et** contrôle la complétude :
pour chaque Z, le nombre de transactions réellement présentes dans sa
fenêtre temporelle doit égaler `transaction_count` scellé.

**Fond de caisse attendu.** Calculé et scellé comme
« ouverture + ventes espèces − remboursements espèces + entrées − sorties »
(`fiscal.py:354-402`), remboursements espèces bien déduits (et non
neutralisés).

**Garde de fin de journée (23:59).** Un job planifié
(`daily_fiscal_close_guard`, `apps/api/app/jobs.py`, déclenché à 23:59
Europe/Paris) clôture toute caisse restée ouverte via
`FiscalService.close_open_drawers` (`fiscal.py:653-682`), sous le même
verrou fiscal. Le Z produit porte `counted=false` (comptage non effectué),
`closing_amount = expected_amount`, et le tiroir est marqué
`closed_by_guard=true` avec une note horodatée. Un échec de ce job est
systématiquement journalisé au JET (`system.job_failed`) — jamais avalé en
silence (`jobs.py:16-45`).

**Régularisation a posteriori.** Si une journée de caisse a été
entièrement oubliée (aucun tiroir ouvert), `preview_regularization` et
`create_regularization_z` (`fiscal.py:714-795`) permettent de générer, avec
un motif obligatoire, un Z couvrant les transactions orphelines de la
période — jamais antidaté, jamais recouvrant une session de caisse déjà
couverte (`covered` rejeté en 409).

**Écriture comptable par Z, dans la même transaction que la clôture.** À
chaque clôture d'un tiroir (`PosService.close_drawer`,
`apps/api/app/services/pos.py:544-578`), à chaque clôture par la garde
23:59 (`FiscalService.close_open_drawers`, `fiscal.py:653-686`) et à chaque
régularisation a posteriori (`FiscalService.create_regularization_z`,
`fiscal.py:748-812`), le Z tout juste scellé déclenche, **dans la même
transaction SQL**, `AccountingService.create_export_for_z`
(`apps/api/app/services/accounting_service.py:247-325`) : celui-ci ventile
les encaissements nets par mode de paiement, les ventes nettes et la TVA
collectée nette (plan de comptes §3.1/§4 ci-avant), avec un ajustement
d'arrondi si nécessaire, et écrit les lignes dans `accounting_exports`/
`accounting_export_lines` (migration `0005_accounting_closures.py`),
protégées respectivement par les triggers `trg_protect_accounting_export`
et `trg_protect_accounting_export_line`. Comme pour le Z lui-même, un
Z sans écriture comptable associée ne peut donc pas exister : soit les deux
sont écrits ensemble, soit la transaction SQL échoue et aucun des deux ne
l'est. `AccountingService.verify_export` (`accounting_service.py:326-367`)
recalcule une écriture et la compare à celle persistée ; une divergence
journalise `accounting.mismatch` (jamais une réécriture silencieuse).

**Clôtures mensuelle et annuelle, totaux perpétuels de période.** Deux
crons planifiés, enregistrés dans `apps/api/app/jobs.py:176-185`,
déclenchent une clôture mensuelle le 1er de chaque mois à 00:15
(`run_monthly_fiscal_closure`, `jobs.py:119-140`, `CronTrigger(day=1,
hour=0, minute=15)`) et une clôture annuelle le 1er janvier à 00:30
(`run_annual_fiscal_closure`, `jobs.py:143-163`, `CronTrigger(month=1,
day=1, hour=0, minute=30)`), toutes deux en heure de Paris ; une clôture
manuelle est disponible à la demande. Les trois passent par
`FiscalClosureService.close_period` (`apps/api/app/services/
fiscal_closure.py`) : refus si une caisse est ouverte (`DrawerOpenError`,
code `drawer_open`, `fiscal_closure.py:47-49`) ou si l'une des deux chaînes
(ventes, Z) est rompue (`ChainInvalidError`, code `chain_invalid`,
`fiscal_closure.py:55-57`) ; table `fiscal_closures` protégée par le
trigger `trg_protect_fiscal_closure`
(`fripco_protect_fiscal_closure()`, immuabilité totale, sans exception,
migration `0005_accounting_closures.py:192-201`) ; grand total de période
et cumul chaîné à la clôture précédente via `previous_hash`
(`fiscal_closure.py:246`). L'échec d'un cron de clôture est journalisé
(`EVENT_SYSTEM_JOB_FAILED`) et déclenche une alerte e-mail best-effort
(`jobs.py`, mêmes garanties que la garde 23:59). `verify_chain`
(`fiscal_closure.py:331-357`) recalcule le maillage et compare l'empreinte
SHA-256 de chaque archive stockée à celle recalculée sur son contenu.

Côté administration, `apps/api/app/api/admin/router.py` expose
`POST /admin/fiscal-closures` (clôture manuelle, 201, refus 409 sur
`drawer_open`/`chain_invalid`, `:602-615`), `GET /admin/fiscal-closures`
(liste, `:618-625`), `GET /admin/fiscal-closures/integrity` (`:629-634`),
`GET /admin/fiscal-closures/{id}` (`:637-645`) et
`GET /admin/fiscal-closures/{id}/archive` (téléchargement gzip, en-têtes
`X-Archive-SHA256` et `X-Closure-Hash`, `:651-682`) ; côté front,
l'onglet **Archives fiscales** (`apps/web/src/components/admin/
FiscalArchivesTab.tsx`) affiche la liste des clôtures (colonnes N°, Type,
Période, Total période, Total perpétuel, Empreinte avec bouton
**Copier**), le bouton **Clôturer maintenant** (avec double confirmation)
et le bouton **Vérifier l'intégrité**. Le mécanisme de clôture périodique
est donc, à la date de rédaction, écrit, raccordé de bout en bout
(crons, routes, écran) et vérifiable par un manager sans intervention
technique.

**Horodatage.** Les horodatages proviennent de l'horloge du serveur
applicatif (calculés côté application avant chaque écriture scellée, pour
que la ligne soit insérée déjà signée en une seule opération — voir §3.1).
Aucune détection de recul d'horloge n'est mise en œuvre : voir §4
(synchronisation NTP recommandée).

### 3.4 Archivage

L'archivage repose sur quatre pièces qui se recoupent : la mention légale
imprimée sur chaque ticket et chaque Z, les écritures comptables générées
à chaque Z, les exports (comptables, bruts, fiscal à la demande) et
l'archive gzip signée de chaque clôture périodique — décrits un à un
ci-dessous, avec leurs références de code.

- Le ticket de caisse embarque, en pied de page, la mention légale
  (décision D14 du contrat PR2, jamais « conforme NF525 ») :
  « Logiciel de caisse Frip & Co Street — auto-attestation art. 286 I-3° bis
  CGI, version fiscale {N} », ainsi que les 16 premiers caractères du hash
  de la transaction (`apps/api/app/services/receipt.py:100-107`).
- La chaîne de vente, la chaîne des Z, le JET et la chaîne des clôtures sont
  chacun vérifiables intégralement et à tout moment depuis
  l'administration (`verify_chain_integrity`, `verify_z_chain_integrity`,
  `JournalService.verify_chain`, `FiscalClosureService.verify_chain`),
  sans dépendre d'un archivage préalable — bouton **Vérifier l'intégrité**
  de l'onglet Archives fiscales.

**Écritures comptables par Z** (`apps/api/app/services/
accounting_service.py`) : une écriture équilibrée par Z (créée dans la
même transaction SQL que la clôture, voir §3.3), ventilant les
encaissements nets par mode de paiement (comptes espèces `531000` / carte
`512000`, défauts dans `apps/api/app/services/settings_service.py`, clé
`accounting`), les ventes nettes (`707100`) et la TVA collectée nette
(`44571`), avec ajustement d'arrondi (`658000`/`758000`) le cas échéant ;
tables `accounting_exports` et `accounting_export_lines`
(migration `0005_accounting_closures.py`), protégées respectivement par
les triggers `trg_protect_accounting_export` et
`trg_protect_accounting_export_line`.

**Export comptable mensuel (CSV Pennylane) et FEC.**
`AccountingService.generate_monthly_csv` (`accounting_service.py:444-493`,
colonnes `_PENNYLANE_CSV_COLUMNS` définies en `accounting_service.py:82`)
et `generate_daily_fec`/`generate_monthly_fec`
(`accounting_service.py:404-443`, FEC 18 colonnes réglementaires) sont
exposés par
`GET /admin/accounting/monthly-csv/{year}/{month}`,
`GET /admin/accounting/fec/day/{date}` et
`GET /admin/accounting/fec/month/{year}/{month}`
(`apps/api/app/api/admin/router.py:464-546`) ; l'onglet **Comptabilité**
(`apps/web/src/components/admin/AccountingTab.tsx`) porte les boutons
**Télécharger le CSV Pennylane**, **Télécharger le fichier FEC du mois** et
**Fichier FEC du jour**.

**Exports bruts** (`apps/api/app/services/table_export.py`) : liste
blanche stricte de sept tables — `transactions`, `transaction_items`,
`payments`, `z_reports`, `cash_movements`, `cash_drawers`,
`journal_events` (`table_export.py:22-32`) — **aucune table
client/consentement/communication** n'y figure, délibérément, pour ne
jamais exposer de donnée personnelle par ce canal (l'export client existe
déjà par fiche, `GET /admin/clients/{id}/export`). Exposé par
`GET /admin/exports/table/{table}?from&to`
(`admin/router.py:548-600`).

**Clôtures périodiques archivées** (`apps/api/app/services/
fiscal_closure.py`) : snapshot JSON canonique compressé gzip, avec
l'horodatage interne de l'archive figé à `mtime=0` pour la reproductibilité
(`gzip.compress(..., mtime=0)`, `fiscal_closure.py:213`), empreinte
SHA-256 de l'archive (`fiscal_closure.py:214`), manifeste chaîné à la
clôture précédente via `previous_hash` (`fiscal_closure.py:246`). Le
contenu couvre transactions, lignes, paiements, Z, mouvements de caisse et
JET de la période, ainsi qu'une notice française auto-descriptive de
vérification (`fiscal_closure.py:207`). Téléchargeable depuis
`GET /admin/fiscal-closures/{id}/archive`
(`admin/router.py:651-682` — en-têtes `X-Archive-SHA256` et
`X-Closure-Hash`) et depuis l'onglet **Archives fiscales**, bouton
**Télécharger l'archive** ; l'empreinte affichée en colonne « Empreinte »
se copie avec le bouton **Copier**.

**Export fiscal à la demande** (`apps/api/app/services/fiscal_export.py`) :
construit un instantané JSON/XML dont l'horodatage de génération
(`generated_at`) est **délibérément exclu du corps signé**, pour que le
même appel sur la même période produise toujours le même contenu et donc
la même empreinte (commentaire de tête du fichier, `fiscal_export.py:5-6`).
Le JET embarqué dans cet instantané **exclut lui-même l'événement
`export.downloaded`** (`fiscal_export.py:68-77`) : chaque appel à cette
route écrit un tel événement (§3.2), qui ne figurerait pas encore dans le
corps qu'il vient de produire mais apparaîtrait au prochain appel — ce qui
casserait la reproductibilité de l'empreinte d'un appel à l'autre ; c'est
un événement opérationnel sur l'export lui-même, pas une donnée fiscale, et
il reste consultable comme tout autre événement JET
(`GET /admin/jet`). La route `GET /admin/fiscal-export`
(`admin/router.py:685-737`) vérifie les deux chaînes avant de servir
l'export (409 `chain_invalid` si l'une est rompue,
`admin/router.py:706-713`) et renvoie l'empreinte SHA-256 du corps dans
l'en-tête `X-Export-SHA256` (`admin/router.py:737`).

**PDF du rapport Z** (`apps/api/app/services/z_report_pdf.py`) : généré par
`generate_z_report_pdf`, avec un canevas rendu invariant (pas
d'horodatage de génération dans le contenu, `z_report_pdf.py:70`) pour
que deux générations du même Z produisent le même document, donc le même
SHA-256 ; reprend les totaux, la ventilation par mode de paiement, les
montants de caisse, les mouvements, les cumuls perpétuels, le hash et le
`previous_hash`, et la mention D14, dont le texte exact est :
« Auto-attestation art. 286 I-3° bis CGI, version fiscale 3 du
2026-09-15. Document conservé 6 ans (art. L.102B LPF). »
(`z_report_pdf.py:290-295`, valeurs interpolées depuis
`FISCAL_SIGNATURE_VERSION` et `FISCAL_VERSION_DATE`, §1) — jamais
« conforme NF525 ». Exposé par `GET /pos/z-reports/{id}/pdf`
(`apps/api/app/api/pos/router.py:937-980`), qui journalise
`export.downloaded` (kind `z_report_pdf`) avant de servir le fichier.

À la date de rédaction, l'ensemble de cette section (services, migration
`0005`, routes d'administration et de caisse, écran **Comptabilité** et
écran **Archives fiscales**) est écrit, raccordé de bout en bout et
utilisable par un manager sans intervention technique. Cette attestation
n'a pas fait l'objet, à ce stade, d'un test manuel complet du parcours
(téléchargement réel d'un CSV/FEC/archive, comparaison d'empreinte hors
application) : cette vérification opérationnelle reste recommandée avant
la première clôture mensuelle réelle (voir `docs/PROCEDURE_CLOTURE.md`
§3).

## 4. Limites et choix déclarés

Cette section liste, sans les euphémiser, les points sur lesquels le
logiciel s'écarte d'une preuve absolue, et pourquoi ces écarts sont jugés
acceptables pour une caisse mono-poste auto-attestée.

- **Colonnes mutables hors signature.** Sur une transaction déjà signée,
  seules `client_id` et `updated_at` peuvent encore changer
  (`fripco_protect_signed_transaction`, exception documentée dans le code —
  `apps/api/alembic/versions/0003_clients_email.py:190-234`). `client_id`
  permet de rattacher a posteriori une vente à une fiche client (envoi du
  ticket par e-mail après paiement) sans que ce rattachement, qui ne modifie
  ni les montants ni les moyens de paiement de la vente, ne rentre dans le
  périmètre de la preuve fiscale — un choix délibéré plutôt qu'un oubli.
  Sur un ticket (`receipts`) déjà émis, `duplicate_count` (nombre de
  relectures du texte), `printed_count` et `printed_at` (compteur et date
  de la dernière impression physique) restent mutables ; le contenu du
  ticket, lui, est figé dès sa création
  (`apps/api/alembic/versions/0004_receipts_printed.py:28-66`). Ces
  compteurs opérationnels ne portent aucune information fiscale et ne
  contredisent pas l'intégrité du ticket.
- **Signature à clé secrète, pas à clé publique.** La chaîne de preuve
  (ventes, Z, JET) est scellée par HMAC-SHA256, un algorithme à **clé
  secrète** (`FISCAL_SIGNING_KEY`). Cela signifie qu'un tiers extérieur
  (expert-comptable, administration fiscale) ne peut pas recalculer et
  vérifier lui-même les empreintes sans que cette clé lui soit communiquée
  — ce qui n'est raisonnablement envisageable que dans un cadre de contrôle
  formel (mise sous scellé de la clé, procédure encadrée), pas en
  consultation libre. En revanche, l'**empreinte SHA-256 de chaque archive
  de clôture** (voir §3.4) est vérifiable par quiconque, sans aucun secret
  : c'est elle qui permet un contrôle d'intégrité de premier niveau hors
  application (comparaison de l'empreinte affichée avec celle recalculée
  sur le fichier téléchargé). Une signature asymétrique donnerait une
  garantie plus forte ; ce choix n'a pas été retenu à ce jour.
- **Horloge du serveur.** Tous les horodatages scellés proviennent de
  l'horloge du serveur hébergeant l'application. Aucun mécanisme du
  logiciel ne détecte un recul de cette horloge ni ne prouve sa
  synchronisation. Il est **recommandé** que le serveur soit synchronisé en
  permanence par NTP (pratique standard des hébergeurs Linux modernes,
  généralement activée par défaut) ; ce point relève de l'exploitation du
  serveur, pas du code applicatif.
- **Archives et sauvegardes sur le même hôte que les données protégées.**
  À la date de rédaction, les archives de clôture (une fois livrées, voir
  §3.4) sont stockées en base de données, et les sauvegardes complètes
  (`docs/DEPLOIEMENT.md` §5) sur le disque du même VPS que la base
  applicative. Un incident matériel unique (disque, VPS) menacerait donc à
  la fois les données vivantes et leurs copies. La procédure de copie hors
  site (§5) est le palliatif à appliquer strictement : elle n'est pas
  automatisée par le logiciel lui-même à ce jour.
- **Pas de certification externe.** Cette attestation est une
  auto-attestation par l'éditeur-utilisateur, pas une certification délivrée
  par un organisme accrédité (COFRAC) ni une attestation individuelle
  d'éditeur commercial. Elle documente ce que le logiciel fait réellement,
  vérifié dans son code, et engage la responsabilité de son signataire dans
  les conditions rappelées au §6 — elle ne remplace pas un avis juridique
  sur l'éligibilité de Frip & Co à ce régime déclaratif, question que
  l'entreprise doit trancher avec son conseil habituel si elle ne l'a pas
  déjà fait.

## 5. Procédures d'exploitation

- **Durée de conservation.** Les données de la chaîne fiscale (ventes,
  annulations, Z, JET, et à terme les clôtures périodiques et leurs
  archives) doivent être conservées **6 ans** au minimum, conformément à
  l'obligation générale de conservation des documents comptables (art.
  L102 B du Livre des procédures fiscales). Le logiciel ne purge aucune
  donnée fiscale de lui-même ; la seule opération de suppression prévue
  (anonymisation RGPD d'une fiche client) préserve intégralement les ventes
  et n'efface qu'une colonne hors signature (`client_id`, remise à `NULL`).
- **Sauvegarde et test de restauration.** Une sauvegarde complète de la
  base de données (`pg_dump | gzip`) est programmée quotidiennement
  (`docs/DEPLOIEMENT.md` §5), avec une rétention de 60 jours. **Une
  restauration complète doit être testée avant l'ouverture de la boutique**,
  et son procès-verbal conservé — critère d'acceptation explicite du cahier
  des charges. Cette sauvegarde protège la disponibilité des données ; elle
  ne remplace pas la copie hors site des archives fiscales évoquée au §4.
- **Conservation de `FISCAL_SIGNING_KEY`.** Cette clé scelle irrévocablement
  toute la chaîne de preuve créée depuis son entrée en service : sa perte
  rend impossible toute nouvelle vérification de la chaîne existante (les
  transactions déjà écrites restent lisibles, mais non re-signables), et sa
  divulgation compromettrait la garantie d'inaltérabilité. Elle doit être
  générée une fois pour toutes au déploiement (`openssl rand -hex 32` ou
  équivalent, au moins 32 caractères — imposé par le code,
  `apps/api/app/core/config.py`), **jamais réutilisée** entre
  environnements, et conservée hors du dépôt de code, hors des sauvegardes
  accessibles en ligne, sous accès restreint (gestionnaire de secrets ou
  coffre physique). Sa perte ou son changement en production doit être
  traité comme un incident majeur (voir ci-dessous), jamais comme une
  opération de routine.
- **Procédure de changement de version fiscale.** Toute modification du
  payload signé (ajout, retrait ou redéfinition d'un champ couvert par la
  signature d'une vente ou d'un Z) est une évolution fiscale majeure. Elle
  impose, avant mise en production : (1) l'incrément de
  `FISCAL_SIGNATURE_VERSION` et la mise à jour de `FISCAL_VERSION_DATE`
  dans `apps/api/app/version.py` ; (2) qu'aucune branche de compatibilité
  avec l'ancienne version ne soit acceptée silencieusement par les
  vérificateurs (`verify_chain_integrity`, `verify_z_chain_integrity`) —
  toute transaction antérieure reste vérifiable avec sa propre version de
  signature, mais aucune transaction nouvelle ne doit pouvoir être écrite
  avec une version obsolète ; (3) la mise à jour de la présente attestation
  (§1, §3.1) avant que la nouvelle version ne serve une vente réelle.
- **Procédure en cas d'incident.** Un incident touchant la chaîne fiscale
  (échec du contrôle d'intégrité, écart de caisse inexpliqué et persistant,
  panne prolongée du job de clôture, doute sur l'intégrité de la clé de
  signature) doit être traité dans cet ordre : (1) ne pas tenter de corriger
  la base par une écriture SQL directe — les triggers la refuseront de
  toute façon, mais la tentative doit être évitée pour ne pas masquer le
  diagnostic ; (2) lancer le contrôle d'intégrité complet depuis
  l'administration (chaîne de vente, chaîne des Z, JET) et conserver son
  résultat ; (3) si l'incident touche des transactions déjà écrites,
  documenter les faits et solliciter, selon la gravité, le conseil
  juridique/comptable habituel de Frip & Co avant toute communication
  externe ; (4) si l'incident touche la disponibilité (panne du serveur, de
  la base), restaurer depuis la dernière sauvegarde testée (voir
  ci-dessus) ; (5) dans tous les cas, consigner l'incident et sa résolution
  par écrit, en dehors du logiciel lui-même (le JET ne journalise que les
  événements techniques normaux du logiciel en fonctionnement, pas
  l'incident lui-même).

## 6. Engagement et signature

Je soussigné, Julien Gondé, agissant en qualité de Président de Frip & Co,
atteste que les mécanismes décrits dans la présente attestation ont été
vérifiés dans le code source du logiciel de caisse Frip & Co Street à la
date ci-dessous, dans les conditions et avec les limites exposées aux §3 et
§4, et m'engage à faire réviser cette attestation à chaque évolution du
mécanisme fiscal (§5) et, au plus tard, après le premier test opérationnel
complet du parcours de clôture périodique (téléchargement réel d'un
CSV/FEC/archive et comparaison d'empreinte hors application, voir la fin
du §3.4 et `docs/PROCEDURE_CLOTURE.md` §3), avant la première clôture
mensuelle réelle de la boutique.

Fait à _______________________, le _______________________

Signature :

_______________________________________
Julien Gondé
Président de Frip & Co
