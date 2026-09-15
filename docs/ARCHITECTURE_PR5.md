# PR5 — Sauvegardes de la base et libellé SumUp

**Demande (15/09/2026) :** « mise en place des outils d'exportation / de
sauvegarde » et « s'assurer que le libellé des transactions SumUp est bien
celui de la boutique ». Les exports comptables et fiscaux existent depuis
PR4 ; cette PR ajoute la **sauvegarde applicative planifiée de la base**
(dump PostgreSQL complet, journalisé, téléchargeable, alerte en cas
d'échec) et fait dériver le **libellé envoyé au TPE** des réglages boutique.
Aucune restauration depuis l'interface (procédure documentée, à la main).

**Périmètre CDC :** §3.3 « sauvegarde quotidienne » (dans le périmètre) ;
l'écran d'administration et l'alerte e-mail vont au-delà de la lettre du
CDC, dans l'esprit du §6 (sauvegarde restaurable testée).

## 1. Contrats

| # | Fonction | Contrat |
|---|---|---|
| G1 | **Service `database_backup.py`** | `run_backup(db, *, trigger: "nightly"\|"manual", user_id=None) -> DatabaseBackup`. Exécute `pg_dump` (binaire `postgresql-client`, déjà dans `Dockerfile.api`) sur l'URL `MIGRATION_DATABASE_URL` si définie, sinon `DATABASE_URL` (le rôle applicatif `fripco_app` a `SELECT` sur toutes les tables et séquences : suffisant), en **sous-processus asynchrone** (`asyncio.create_subprocess_exec`, jamais `shell=True`, mot de passe passé par `PGPASSWORD` dans l'environnement du sous-processus uniquement, jamais en argument). Sortie gzippée en flux vers `BACKUP_DIR/fripco_<YYYYMMDD_HHMMSS>.sql.gz` (écriture dans un fichier temporaire puis `rename` atomique). Vérifications : `gunzip -t` équivalent (relecture gzip complète) et présence de la chaîne `CREATE TABLE public.journal_events` dans le dump. Calcule `sha256` et `size_bytes`. Une ligne `DatabaseBackup` est écrite **dans tous les cas** (succès ou échec, avec `error` tronqué à 500 caractères, sans mot de passe). Puis purge : fichiers et lignes plus vieux que `retention_days` (les fichiers manquants sur disque sont marqués `missing`, jamais recréés). Toute erreur de `pg_dump` (code de retour ≠ 0, binaire absent, disque plein) → ligne `failed` + exception `BackupError` remontée à l'appelant. |
| G2 | **Table `database_backups`** (migration `0006_database_backups`) | `id` uuid, `created_at`, `finished_at`, `trigger` (`nightly`/`manual`), `status` (`success`/`failed`/`missing`), `filename` (unique), `size_bytes`, `sha256` varchar(64) nullable, `duration_ms`, `error` text nullable, `triggered_by_user_id` FK nullable. **Pas de trigger d'immutabilité** (table d'exploitation, purgeable). `EXPECTED_DB_REVISION` → `"0006"`, `APP_VERSION` → `"0.6.0"`. |
| G3 | **Réglages `backup`** (`settings_service.DEFAULT_VALUES["backup"]`) | `retention_days` (int, défaut 60, bornes 7–3650), `nightly_enabled` (bool, défaut true), `alert_email` (str, défaut "" → repli `shop.email`). `BACKUP_DIR` : variable d'environnement (défaut `data/backups`, sous le volume `fripco_data` en prod ; à ajouter à `.env.example` et `docker-compose.prod.yml`). |
| G4 | **Cron** (`jobs.py`) | `run_nightly_database_backup` à **03:00 Europe/Paris** (les clôtures fiscales sont à 00:15 / 00:30 / 23:59, aucun chevauchement). Ne fait rien si `nightly_enabled` est faux (JET `system.job_skipped` non nécessaire : simple log). En cas d'échec : JET `system.job_failed` **et** `_alert_job_failure` (réutilisé tel quel) vers `backup.alert_email` ou, à défaut, `shop.email`. |
| G5 | **Routes admin** (JWT, `api/admin/router.py`, préfixe `/api/admin/database`) | `GET /state` → `{engine_version, database_size_bytes, tables: [{name, rows_estimate}], last_backup: {…}\|null, backup_dir_free_bytes}` (comptages via `pg_stat_user_tables.n_live_tup`, jamais de `COUNT(*)` sur toutes les tables). `GET /config` / `PUT /config` (validation des bornes ; JET `settings.updated` comme les autres réglages). `GET /backups?limit=50` (liste triée récente d'abord). `POST /backups/run` → 201 avec la ligne ; 409 `backup_running` si une sauvegarde est déjà en cours (verrou `asyncio.Lock` de processus, suffisant : une seule instance d'API). `GET /backups/{id}/download` → `application/gzip`, `Content-Disposition` avec le nom du fichier, `X-Backup-SHA256`, **JET `export.downloaded`** (même événement que les autres téléchargements, `kind="database_backup"`) ; 404 si la ligne est `missing`. `DELETE /backups/{id}` → supprime fichier + ligne ; JET `backup.deleted` (nouveau type d'événement, payload `{backup_id, filename}`). Toute route en `PosServiceError(message, code=, status_code=)` → `{detail, code}`. |
| G6 | **Front admin** | Nouvel onglet **« Sauvegardes »** dans `admin/page.tsx` (6ᵉ `TabButton`), composant `components/admin/BackupsTab.tsx` : carte **État de la base** (taille, moteur, tableau volumes par table, dernière sauvegarde avec statut coloré), carte **Réglages** (rétention, activation nocturne, e-mail d'alerte), carte **Sauvegardes** (bouton « Lancer une sauvegarde maintenant » avec état occupé, liste : date, déclencheur, taille, statut, empreinte courte + bouton copier, Télécharger via `lib/download.ts` existant, Supprimer avec confirmation). Vocabulaire sans jargon (« sauvegarde », « empreinte »), pas de « dump ». Mock (`mockApi.ts`) aligné pour le mode démo. |
| G7 | **Libellé SumUp** | `cb_router.py` (paiement initial **et** réessai) passe `description=f"Vente {shop.name}"` lu via `SettingsService.get("shop")` au moment de l'appel ; repli `"Vente Frip & Co Street"` si le nom est vide. La valeur envoyée est journalisée dans le `sumup_exchanges`/log existant (payload déjà rédigé). Test : nom boutique modifié → description modifiée ; nom vide → repli. |
| G8 | **Docs** | `docs/DEPLOIEMENT.md` §5 : la sauvegarde nocturne applicative devient la voie principale ; `scripts/backup.sh` reste la sauvegarde **hôte** optionnelle (double ceinture), la crontab hôte devient facultative. Procédure de restauration (à la main, `gunzip -c … \| docker exec -i fripco-db psql …`) et rappel du test de restauration avant ouverture. `README.md` tableau des PR : ligne PR5. `docs/ATTESTATION_NF525.md` : une phrase au §3.4 ou §4 disant que la sauvegarde applicative est une mesure d'exploitation, distincte de l'archive fiscale signée. |

## 2. Hors périmètre PR5
Restauration depuis l'interface, copie hors-site automatique, chiffrement
des dumps. À décider séparément.

## 3. Tests attendus
- Service : dump réel sur la base de test PostgreSQL (`pg_dump` disponible en CI via `postgresql-client`), fichier gzip valide, `sha256` exact, ligne `success` ; `pg_dump` introuvable (PATH vidé) → ligne `failed` + `BackupError` ; purge par rétention (fichier et ligne) ; fichier supprimé à la main → `missing` au listing.
- Routes : 401 sans JWT sur toutes ; `POST /run` 201 puis `GET /backups` le liste ; `download` renvoie les octets exacts + `X-Backup-SHA256` + JET `export.downloaded` ; 409 si sauvegarde concurrente ; `PUT /config` refuse `retention_days=3` (422) ; `DELETE` supprime fichier + ligne + JET.
- Cron : `run_nightly_database_backup` avec `nightly_enabled=false` → aucune ligne ; échec simulé → JET `system.job_failed` + e-mail (passerelle simulée).
- SumUp : G7.
- Isolation : `tests/test_isolation.py` reste vert ; aucun nom de l'autre boutique.
