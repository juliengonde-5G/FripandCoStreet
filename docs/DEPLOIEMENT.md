# Déploiement — Frip & Co Street

Caisse isolée hébergée sur le VPS existant, derrière le reverse-proxy Caddy
déjà en place. Domaine public : **https://lloomi.fr** (DEUX « o »), DNS
chez **OVH**. `www.lloomi.fr` redirige vers l'apex (redirection HTTPS
gérée par Caddy — voir §2.1, pas par OVH).

## 1. Ce qui est partagé, ce qui ne l'est pas

| Composant | Statut |
|---|---|
| Reverse-proxy Caddy (ports 80/443) | **Partagé** — seul point de contact. Il est rattaché au réseau externe `fripco-network` et proxifie `lloomi.fr` vers `fripco-web` (/) et `fripco-api` (/api/*) ; `www.lloomi.fr` est redirigé vers l'apex. |
| Réseau Docker | Dédié : `fripco-network` (externe, créé une fois). Aucun service de la caisse ne rejoint un autre réseau. |
| Base PostgreSQL | Dédiée : conteneur `fripco-db`, volume `postgres_data` de la stack, non publié sur l'hôte. |
| Secrets | Dédiés : `/opt/fripco-street/.env`. |
| Sauvegardes | Dédiées : `scripts/backup.sh` (cron hôte) → `/opt/fripco-street/backups`. |
| Code | Dédié : clone `/opt/fripco-street` (aucun import, aucun appel réseau vers une autre application). |

Voir aussi §2.4 « Non-interférence avec l'autre application du VPS » pour le
détail de cette isolation et la procédure de vérification post-déploiement.

## 2. Pré-requis (une fois)

### 2.1 DNS (zone `lloomi.fr` chez OVH)

Dans le Manager OVH, zone DNS `lloomi.fr` :

1. Enregistrement **`A`** de l'**apex** (`lloomi.fr`, cible `@`) → IPv4 du VPS.
2. Enregistrement **`A`** de **`www`** → même IPv4 du VPS (Caddy se charge de
   la redirection `www.lloomi.fr` → `https://lloomi.fr`, voir le bloc
   `docker/Caddyfile.fragment`).
3. Si le VPS a une IPv6 : enregistrement **`AAAA`** correspondant sur l'apex
   et sur `www`, en plus des `A`.
4. **TTL court** (300 s / 5 min) sur ces enregistrements pendant la mise en
   service, pour pouvoir corriger rapidement en cas d'erreur ; remonter à un
   TTL normal (1 h+) une fois le déploiement validé.
5. **Supprimer toute redirection web OVH** existante sur `lloomi.fr` et
   `www.lloomi.fr` (fonctionnalité « Redirection » de la zone DNS OVH, à ne
   pas confondre avec un enregistrement `A`/`AAAA`) : une redirection OVH
   répond elle-même aux requêtes HTTP, ce qui empêche Caddy d'obtenir son
   certificat ACME (challenge HTTP-01 intercepté) et entre en conflit avec
   la redirection `www` gérée côté Caddy (§2.1 ci-dessus, bloc
   `www.lloomi.fr { redir … }` dans `docker/Caddyfile.fragment`).

Faire les changements DNS **avant** d'activer le bloc Caddy (sinon Caddy
retente l'obtention du certificat en boucle, sans bloquer les autres sites
du VPS).

### 2.2 Réseau + reverse-proxy

1. **Réseau externe** : `docker network create fripco-network` (idempotent ;
   `scripts/deploy.sh` le fait aussi).
2. **Reverse-proxy** : ajouter le bloc de `docker/Caddyfile.fragment`
   (`lloomi.fr` + `www.lloomi.fr`) au Caddyfile **déjà présent sur le VPS**
   (PR dédiée côté proxy, hors de ce dépôt) et rattacher le conteneur Caddy
   au réseau `fripco-network` (dans sa stack : `networks: [ …,
   fripco-network ]` + `networks: fripco-network: external: true`), puis
   recharger Caddy (`caddy reload` — voir §2.4, aucun impact sur les autres
   vhosts).

### 2.3 Code et secrets

1. **Clone** : `git clone <dépôt fripco-street> /opt/fripco-street` (tant
   que le code vit dans le dépôt Vintiz : `/opt/vintiz/fripco-street`
   fonctionne aussi, `deploy.sh` est relatif à lui-même et ne touche pas à
   git sans `--pull`).
2. **Secrets** : `cp .env.example .env` puis remplir toutes les valeurs
   `CHANGER_MOI` (`openssl rand -hex 32` pour `SECRET_KEY` et
   `FISCAL_SIGNING_KEY`, deux valeurs différentes). `FISCAL_SIGNING_KEY` est
   **définitive** : la conserver hors ligne sous accès restreint.

### 2.4 Non-interférence avec l'autre application du VPS

Le VPS héberge déjà une autre application (Vintiz). Frip & Co Street ne
partage **que** le reverse-proxy avec elle :

| Élément | Frip & Co Street | Partagé ? |
|---|---|---|
| Réseau Docker | `fripco-network` (dédié, externe) | Non — aucun service ne rejoint le réseau de l'autre application |
| Ports publiés sur l'hôte | Aucun (`fripco-api`, `fripco-web`, `fripco-db` ne publient rien ; seul Caddy écoute 80/443) | Non |
| Conteneurs / volumes | Préfixés `fripco-` (`fripco-api`, `fripco-web`, `fripco-db`, volume `postgres_data` de la stack) | Non |
| `.env` | `/opt/fripco-street/.env`, propre à cette stack | Non |
| Base de données | Conteneur `fripco-db` dédié, jamais partagé | Non |
| Cron de sauvegarde | `scripts/backup.sh` dédié (§5), séparé de celui de l'autre application | Non |
| Reverse-proxy Caddy (ports 80/443) | Un seul Caddy pour tout le VPS | **Oui — seul point partagé** |

Le reverse-proxy est donc le seul point de contact entre les deux
applications, et ce contact se limite à un fichier de config (Caddyfile) et
un rechargement de process. `caddy reload` recharge la configuration sans
couper les connexions existantes ni redémarrer le process : ajouter ou
modifier le bloc `lloomi.fr` n'interrompt **pas** les vhosts de l'autre
application.

**Procédure de vérification après déploiement** (à exécuter systématiquement,
depuis une machine externe au VPS) :

```bash
# 1. La caisse répond sur son propre domaine
curl -I https://lloomi.fr
curl https://lloomi.fr/api/health

# 2. www redirige bien vers l'apex
curl -I https://www.lloomi.fr

# 3. L'autre application du VPS répond toujours sur ses propres domaines
#    (remplacer par le domaine réel de l'autre application, ex. Vintiz)
curl -I https://<domaine-de-l-autre-application>
```

Si l'étape 3 échoue après un rechargement Caddy lié à cette PR, c'est un
signal d'interférence à traiter immédiatement (voir rollback ci-dessous)
avant de considérer le déploiement Frip & Co Street comme terminé.

### 2.5 Rollback du bloc Caddy

En cas de problème (certificat, conflit de vhost, régression sur l'autre
application) : retirer le bloc `lloomi.fr` / `www.lloomi.fr` ajouté en §2.2
du Caddyfile du VPS, puis recharger Caddy — sans toucher au reste de la
configuration :

```bash
caddy validate --config /chemin/vers/Caddyfile   # optionnel, avant reload
caddy reload --config /chemin/vers/Caddyfile
```

Ceci ne coupe que `lloomi.fr` / `www.lloomi.fr` ; les autres vhosts (dont
l'autre application du VPS) continuent de répondre normalement pendant et
après l'opération.

### 2.6 Checklist pré-ouverture

- [ ] DNS OVH : `A` (+ `AAAA` si IPv6) sur l'apex et sur `www`, TTL court,
      aucune redirection web OVH restante sur ces deux noms (§2.1).
- [ ] Bloc `docker/Caddyfile.fragment` (`lloomi.fr` + `www.lloomi.fr`)
      ajouté au Caddyfile du VPS, Caddy rattaché à `fripco-network`, reload
      effectué (§2.2).
- [ ] Certificat HTTPS obtenu pour `lloomi.fr` et `www.lloomi.fr` (`curl -I
      https://lloomi.fr` et `curl -I https://www.lloomi.fr` répondent sans
      erreur TLS).
- [ ] `curl https://lloomi.fr/api/health` OK.
- [ ] `www.lloomi.fr` redirige bien vers `https://lloomi.fr` (301/308).
- [ ] L'autre application du VPS répond toujours sur ses propres domaines
      (§2.4, étape 3).
- [ ] `.env` sans valeur `CHANGER_MOI` restante (`deploy.sh` bloque sinon).
- [ ] Compte manager unique créé (`scripts/create_manager.py`).
- [ ] Cron de sauvegarde installé (§5) et une restauration testée une fois
      (§5, critère d'acceptation §6 du CDC).
- [ ] TTL DNS remonté à une valeur normale une fois le service stabilisé.

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
curl -s https://lloomi.fr/api/health
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

## 7. E-mail des tickets et newsletter (PR3)

Compte Brevo **partagé** avec Vintiz Vernon : la caisse n'utilise que sa
liste dédiée « Frip & Co Street ». Variables `.env` : `BREVO_API_KEY`,
`BREVO_LIST_ID` (numéro de la liste dédiée, créée dans Brevo), `BREVO_WEBHOOK_TOKEN`
(secret partagé ; côté Brevo, URL du webhook `https://lloomi.fr/api/brevo/webhook?token=<secret>`
sur l'événement « désinscription »), `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME`,
et **`BREVO_ANONYMOUS_TRACKING=true`** uniquement si le compte Brevo est réglé en
suivi d'ouverture anonyme (CNIL) ; sinon les tickets partent par SMTP
(`SMTP_*`) ou ne partent pas (mode simulation, signalé en administration).
La caisse ne touche jamais à la blocklist globale d'un contact ni ne supprime
un contact Brevo : un désabonnement ou une suppression RGPD retire le contact
de la liste dédiée et anonymise la fiche locale.

## 8. Déploiement automatique

`.github/workflows/deploy.yml` (actif dans le dépôt dédié) : après une CI verte
sur `main`, connexion SSH et `./scripts/deploy.sh --pull`. Secrets à créer dans
le dépôt : `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_PORT` (optionnel).
