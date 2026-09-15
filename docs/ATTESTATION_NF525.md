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
`fiscal_export.py`, `table_export.py`, `z_report_pdf.py`) étaient présents
dans le code à cette date ; les routes d'API qui les exposent en
administration n'y étaient, elles, pas encore toutes raccordées — voir la
réserve précise en §3.3 et §3.4. Toute évolution du mécanisme de signature fiscale
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

**Exports et téléchargements.** Le contrat d'architecture PR4
(`docs/ARCHITECTURE_PR4.md` §1, décision F7) prévoit qu'aucun export ou
téléchargement (CSV comptable, FEC, export brut, archive de clôture, export
fiscal à la demande, PDF du Z) ne puisse avoir lieu sans une écriture JET
`export.downloaded` correspondante. La constante d'événement
(`EVENT_EXPORT_DOWNLOADED`, ainsi que `accounting.export_created`,
`accounting.mismatch`, `closure.created`, `closure.failed`) est déclarée
dans `apps/api/app/services/jet.py` ; son écriture effective par chaque
route de téléchargement dépend des routes d'administration décrites au
§3.3/§3.4, qui n'étaient pas toutes raccordées à la date de rédaction — ce
point précis reste donc à vérifier une fois ce raccordement terminé.

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

**Clôtures mensuelle et annuelle, totaux perpétuels de période.** Le
contrat d'architecture PR4 (`docs/ARCHITECTURE_PR4.md` §1, décisions F5,
et §3) spécifie une clôture mensuelle (le 1er à 00:15 Europe/Paris) et
annuelle (le 1er janvier à 00:30), plus une clôture manuelle à la demande.
`FiscalClosureService.close_period` (`apps/api/app/services/
fiscal_closure.py`) implémente ce mécanisme : refus si une caisse est
ouverte (`DrawerOpenError`, code `drawer_open`) ou si l'une des deux
chaînes (ventes, Z) est rompue (`ChainInvalidError`, code `chain_invalid`),
table `fiscal_closures` (migration `0005_accounting_closures.py`) protégée
par le trigger `trg_protect_fiscal_closure`
(`fripco_protect_fiscal_closure()`, immuabilité totale, sans exception),
grand total de période et cumul chaîné à la clôture précédente via
`previous_hash`. `verify_chain` (`fiscal_closure.py:331-357`) recalcule le
maillage et compare l'empreinte SHA-256 de l'archive stockée à celle
recalculée sur son contenu. Les crons correspondants
(`monthly_fiscal_closure`, `annual_fiscal_closure`) et les routes
d'administration qui exposent ce service (liste des clôtures, clôture
manuelle, téléchargement de l'archive) n'étaient, à la date de rédaction,
**pas encore raccordées dans `apps/api/app/api/admin/router.py`** : le
mécanisme de clôture périodique est donc écrit et partiellement vérifié au
niveau du service, mais **pas encore opérable de bout en bout par un
manager**. Cette attestation doit être relue dès que ce raccordement sera
livré, pour confirmer qu'aucun écart n'a été introduit entre le service et
son exposition en API/administration.

**Horodatage.** Les horodatages proviennent de l'horloge du serveur
applicatif (calculés côté application avant chaque écriture scellée, pour
que la ligne soit insérée déjà signée en une seule opération — voir §3.1).
Aucune détection de recul d'horloge n'est mise en œuvre : voir §4
(synchronisation NTP recommandée).

### 3.4 Archivage

**Ce qui existe et est vérifié à ce jour :**

- Le ticket de caisse embarque, en pied de page, la mention légale
  (décision D14 du contrat PR2, jamais « conforme NF525 ») :
  « Logiciel de caisse Frip & Co Street — auto-attestation art. 286 I-3° bis
  CGI, version fiscale {N} », ainsi que les 16 premiers caractères du hash
  de la transaction (`apps/api/app/services/receipt.py:100-107`).
- La chaîne de vente et le JET sont chacun vérifiables intégralement et à
  tout moment depuis l'administration (`verify_chain_integrity`,
  `verify_z_chain_integrity`, `JournalService.verify_chain`), sans dépendre
  d'un archivage préalable.

**Ce qui est écrit dans le code à la date de rédaction, au niveau du
service, mais pas encore exposé par une route d'administration** (voir la
réserve d'opérabilité de bout en bout au §3.3) :

- **Écritures comptables par Z** (`apps/api/app/services/
  accounting_service.py`) : une écriture équilibrée par Z, ventilant les
  encaissements nets par mode de paiement (comptes espèces `531000` / carte
  `512000`, défauts `apps/api/app/services/settings_service.py`, clé
  `accounting`), les ventes nettes (`707100`) et la TVA collectée nette
  (`44571`), avec ajustement d'arrondi (`658000`/`758000`) le cas échéant ;
  tables `accounting_exports` et `accounting_export_lines`
  (migration `0005_accounting_closures.py`), protégées respectivement par
  les triggers `trg_protect_accounting_export` et
  `trg_protect_accounting_export_line`.
- **Export comptable mensuel (CSV)** et **FEC** (journalier et mensuel,
  18 colonnes réglementaires), générés par `accounting_service.py` à partir
  du même plan de comptes.
- **Exports bruts** (`table_export.py`) : journal des ventes / journal de
  caisse détaillés en CSV, sur liste blanche de tables, sans aucune donnée
  client/PII.
- **Clôtures périodiques archivées** (`fiscal_closure.py`) : snapshot JSON
  canonique compressé gzip, avec l'horodatage interne de l'archive figé à
  `mtime=0` pour la reproductibilité (`gzip.compress(..., mtime=0)`,
  `fiscal_closure.py:213`), empreinte SHA-256 de l'archive
  (`fiscal_closure.py:214`), manifeste chaîné à la clôture précédente via
  `previous_hash` (`fiscal_closure.py:246`). Le contenu couvre transactions,
  lignes, paiements, Z, mouvements de caisse et JET de la période, ainsi
  qu'une notice française auto-descriptive de vérification
  (`fiscal_closure.py:207`).
- **Export fiscal à la demande** (`fiscal_export.py`) : construit un
  instantané JSON/XML dont l'horodatage de génération (`generated_at`) est
  **délibérément exclu du corps signé**, pour que le même appel sur la même
  période produise toujours le même contenu et donc la même empreinte
  (commentaire de tête du fichier, `fiscal_export.py:5-6`). La vérification
  des deux chaînes avant de servir l'export, et l'en-tête HTTP
  `X-Export-SHA256`, relèvent de la route d'administration qui appellera ce
  service — non encore raccordée à la date de rédaction.
- **PDF du rapport Z** (`z_report_pdf.py`) : généré par
  `generate_z_report_pdf`, avec un canevas rendu invariant (pas
  d'horodatage de génération dans le contenu, `z_report_pdf.py:70`) pour
  que deux générations du même Z produisent le même document, donc le même
  SHA-256 ; reprend les totaux, la ventilation par mode de paiement, les
  montants de caisse, les mouvements, les cumuls perpétuels, le hash et le
  `previous_hash`, et la mention D14 (« Auto-attestation art. 286 I-3° bis
  CGI, version fiscale {N} », `z_report_pdf.py:274-285`) — jamais
  « conforme NF525 ».

**Ce qui reste à livrer et à vérifier avant que cette section soit
considérée close** : les routes d'administration qui exposent ces cinq
services (téléchargement du CSV/FEC, des exports bruts, de l'archive de
clôture et du PDF du Z ; déclenchement d'une clôture manuelle ; liste des
clôtures) dans `apps/api/app/api/admin/router.py` et
`apps/api/app/api/pos/router.py`, l'écriture effective de
`export.downloaded` à chaque téléchargement (§3.2), les deux crons
`monthly_fiscal_closure`/`annual_fiscal_closure` dans `app/jobs.py`, et
l'écran d'administration correspondant côté `apps/web`. Cette section devra
être relue dès leur livraison (voir la procédure de mise à jour, §5).

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
mécanisme fiscal (§5) et, au plus tard, lorsque les routes d'administration
et les crons encore décrits aux §3.3 et §3.4 comme non raccordés auront été
livrés et mis en production.

Fait à _______________________, le _______________________

Signature :

_______________________________________
Julien Gondé
Président de Frip & Co
