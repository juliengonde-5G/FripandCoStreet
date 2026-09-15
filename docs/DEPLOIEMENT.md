# Déploiement — Frip & Co Street

Caisse isolée hébergée sur le VPS existant, derrière le reverse-proxy Caddy
déjà en place. Sous-domaine : **https://street.fripco.fr**.

## 1. Ce qui est partagé, ce qui ne l'est pas

| Composant | Statut |
|---|---|
| Reverse-proxy Caddy (ports 80/443) | **Partagé** — seul point de contact. Il est rattaché au réseau externe `fripco-network` et proxifie `street.fripco.fr` vers `fripco-web` (/) et `fripco-api` (/api/*). |
| Réseau Docker | Dédié : `fripco-network` (externe, créé une fois). Aucun service de la caisse ne rejoint un autre réseau. |
| Base PostgreSQL | Dédiée : conteneur `fripco-db`, volume `postgres_data` de la stack, non publié sur l'hôte. |
| Secrets | Dédiés : `/opt/fripco-street/.env`. |
| Sauvegardes | Dédiées : `scripts/backup.sh` (cron hôte) → `/opt/fripco-street/backups`. |
| Code | Dédié : clone `/opt/fripco-street` (aucun import, aucun appel réseau vers une autre application). |

## 2. Pré-requis (une fois)

1. **DNS** : enregistrement `A` (et `AAAA` si IPv6) `street.fripco.fr` → IP du VPS, **avant** d'activer le bloc Caddy (sinon Caddy retente l'obtention du certificat en boucle, sans bloquer les autres sites).
2. **Réseau externe** : `docker network create fripco-network` (idempotent ; `scripts/deploy.sh` le fait aussi).
3. **Reverse-proxy** : ajouter le bloc de `docker/Caddyfile.fragment` au Caddyfile du VPS et rattacher le conteneur Caddy au réseau `fripco-network` (dans sa stack : `networks: [ …, fripco-network ]` + `networks: fripco-network: external: true`), puis recharger Caddy.
4. **Clone** : `git clone <dépôt fripco-street> /opt/fripco-street` (tant que le code vit dans le dépôt Vintiz : `/opt/vintiz/fripco-street` fonctionne aussi, `deploy.sh` est relatif à lui-même et ne touche pas à git sans `--pull`).
5. **Secrets** : `cp .env.example .env` puis remplir toutes les valeurs `CHANGER_MOI` (`openssl rand -hex 32` pour `SECRET_KEY` et `FISCAL_SIGNING_KEY`, deux valeurs différentes). `FISCAL_SIGNING_KEY` est **définitive** : la conserver hors ligne sous accès restreint.

## 3. Déploiement

```bash
cd /opt/fripco-street
./scripts/deploy.sh          # build → base → migrations (rôle propriétaire) → démarrage → health-check
./scripts/deploy.sh --pull   # idem après mise à jour depuis origin/main
./scripts/deploy.sh --rollback
```

Premier démarrage : créer l'unique compte manager.

```bash
docker exec -it fripco-api python scripts/create_manager.py --username <nom> --email <email>
```

Vérifications :

```bash
curl -s https://street.fripco.fr/api/health
docker compose -f docker/docker-compose.prod.yml --env-file .env ps
```

## 4. Rôles PostgreSQL et inaltérabilité

Deux rôles, créés au premier démarrage du conteneur `fripco-db` par
`docker/db-init/01_roles.sh` :

- `POSTGRES_USER` (propriétaire) : exécute les migrations Alembic
  (`MIGRATION_DATABASE_URL`). C'est lui qui installe les triggers d'inaltérabilité.
- `FRIPCO_APP_USER` (applicatif, `DATABASE_URL`) : `SELECT/INSERT/UPDATE/DELETE`
  seulement. Il ne peut ni `ALTER TABLE`, ni `DISABLE TRIGGER`, ni créer d'objet.
  Les triggers lui sont donc opposables : l'application ne peut pas contourner
  ses propres protections (écart I-3 du rapport d'audit PR0).

L'API refuse de démarrer si la révision Alembic de la base n'est pas celle
attendue par le code (`app/version.py`).

## 5. Sauvegarde

```bash
crontab -e
15 3 * * * /opt/fripco-street/scripts/backup.sh >> /var/log/fripco-backup.log 2>&1
```

Dump complet gzippé, vérifié (`gunzip -t` + présence de `journal_events`),
rétention 60 jours. Restauration :

```bash
gunzip -c backups/fripco_YYYYMMDD_HHMMSS.sql.gz | docker exec -i fripco-db psql -U fripco -d fripco
```

Une restauration complète doit être **testée une fois avant l'ouverture**
(critère d'acceptation §6 du CDC) et son procès-verbal conservé.

## 6. TPE SumUp Solo (PR2)

Variables `.env` : `SUMUP_API_KEY` (clé **de production** `sup_sk_…`, une clé de
test est refusée en production), `SUMUP_MERCHANT_CODE`, `SUMUP_READER_ID`
(identifiant du Solo enrôlé : `GET https://api.sumup.com/v0.1/merchants/{code}/readers`
avec la clé en Bearer). Le TPE se connecte au Wi-Fi de la boutique et à SumUp ;
l'API pousse le montant sur le TPE via l'API SumUp, aucune saisie sur le TPE.
Sans ces trois variables ou TPE hors ligne, la caisse n'accepte que les espèces.
État visible dans l'écran Administration.

## 7. Déploiement automatique

`.github/workflows/deploy.yml` (actif dans le dépôt dédié) : après une CI verte
sur `main`, connexion SSH et `./scripts/deploy.sh --pull`. Secrets à créer dans
le dépôt : `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_PORT` (optionnel).
