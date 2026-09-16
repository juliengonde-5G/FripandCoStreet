# Déploiement — Frip & Co Street

Caisse isolée hébergée sur le VPS existant, derrière le reverse-proxy Caddy
déjà en place. Domaine public : **https://app.lloomi.fr** (sous-domaine
`app`, DNS chez **OVH**).

## 1. Ce qui est partagé, ce qui ne l'est pas

| Composant | Statut |
|---|---|
| Reverse-proxy Caddy (ports 80/443) | **Partagé** — seul point de contact. Il est rattaché au réseau externe `fripco-network` et proxifie `app.lloomi.fr` vers `fripco-web` (/) et `fripco-api` (/api/*). |
| Réseau Docker | Dédié : `fripco-network` (externe, créé une fois). Aucun service de la caisse ne rejoint un autre réseau. |
| Base PostgreSQL | Dédiée : conteneur `fripco-db`, volume `postgres_data` de la stack, non publié sur l'hôte. |
| Secrets | Dédiés : `/opt/fripco-street/.env`. |
| Sauvegardes | Dédiées : `scripts/backup.sh` (cron hôte) → `/opt/fripco-street/backups`. |
| Code | Dédié : clone `/opt/fripco-street` (aucun import, aucun appel réseau vers une autre application). |

Voir aussi §2.4 « Non-interférence avec l'autre application du VPS » pour le
détail de cette isolation et la procédure de vérification post-déploiement.

## 2. Pré-requis (une fois)

### 2.1 DNS (`app.lloomi.fr` chez OVH)

Dans le Manager OVH, sur la zone DNS du domaine :

1. Enregistrement **`A`** du sous-domaine **`app`** (`app.lloomi.fr`) → IPv4
   du VPS.
2. Si le VPS a une IPv6 : enregistrement **`AAAA`** correspondant sur `app`,
   en plus du `A`.
3. **TTL court** (300 s / 5 min) sur cet enregistrement pendant la mise en
   service, pour pouvoir corriger rapidement en cas d'erreur ; remonter à un
   TTL normal (1 h+) une fois le déploiement validé.
4. **Supprimer toute redirection web OVH** existante sur `app.lloomi.fr`
   (fonctionnalité « Redirection » de la zone DNS OVH, à ne pas confondre
   avec un enregistrement `A`/`AAAA`) : une redirection OVH répond
   elle-même aux requêtes HTTP, ce qui empêche Caddy d'obtenir son
   certificat ACME (challenge HTTP-01 intercepté).

Faire les changements DNS **avant** d'activer le bloc Caddy (sinon Caddy
retente l'obtention du certificat en boucle, sans bloquer les autres sites
du VPS).

### 2.2 Réseau + reverse-proxy

1. **Réseau externe** : `docker network create fripco-network` (idempotent ;
   `scripts/deploy.sh` le fait aussi).
2. **Reverse-proxy** : ajouter le bloc de `docker/Caddyfile.fragment`
   (`app.lloomi.fr`) au Caddyfile **déjà présent sur le VPS** (PR dédiée
   côté proxy, hors de ce dépôt) et rattacher le conteneur Caddy au réseau
   `fripco-network` (dans sa stack : `networks: [ …,
   fripco-network ]` + `networks: fripco-network: external: true`), puis
   recharger Caddy (`caddy reload` — voir §2.4, aucun impact sur les autres
   vhosts).

### 2.3 Code et secrets

1. **Clone** : `git clone <dépôt fripco-street> /opt/fripco-street` (tant
   que le code vit imbriqué dans le dépôt de l'application source, le
   chemin équivalent sous ce dépôt-là fonctionne aussi — ex.
   `/opt/<dépôt-de-l-autre-application>/fripco-street` — `deploy.sh` est
   relatif à lui-même et ne touche pas à git sans `--pull`).
2. **Secrets** : `cp .env.example .env` puis remplir toutes les valeurs
   `CHANGER_MOI` (`openssl rand -hex 32` pour `SECRET_KEY` et
   `FISCAL_SIGNING_KEY`, deux valeurs différentes). `FISCAL_SIGNING_KEY` est
   **définitive** : la conserver hors ligne sous accès restreint.

### 2.4 Non-interférence avec l'autre application du VPS

Le VPS héberge déjà une autre application (l'application source, boutique
de Vernon). Frip & Co Street ne
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
modifier le bloc `app.lloomi.fr` n'interrompt **pas** les vhosts de l'autre
application.

**Procédure de vérification après déploiement** (à exécuter systématiquement,
depuis une machine externe au VPS) :

```bash
# 1. La caisse répond sur son propre domaine
curl -I https://app.lloomi.fr
curl https://app.lloomi.fr/api/health

# 2. L'autre application du VPS répond toujours sur ses propres domaines
#    (remplacer par le domaine réel de l'autre application, ex. celui de
#    la boutique de Vernon)
curl -I https://<domaine-de-l-autre-application>
```

Si l'étape 2 échoue après un rechargement Caddy lié à cette PR, c'est un
signal d'interférence à traiter immédiatement (voir rollback ci-dessous)
avant de considérer le déploiement Frip & Co Street comme terminé.

### 2.5 Rollback du bloc Caddy

En cas de problème (certificat, conflit de vhost, régression sur l'autre
application) : retirer le bloc `app.lloomi.fr` ajouté en §2.2 du Caddyfile
du VPS, puis recharger Caddy — sans toucher au reste de la configuration :

```bash
caddy validate --config /chemin/vers/Caddyfile   # optionnel, avant reload
caddy reload --config /chemin/vers/Caddyfile
```

Ceci ne coupe que `app.lloomi.fr` ; les autres vhosts (dont l'autre
application du VPS) continuent de répondre normalement pendant et après
l'opération.

### 2.6 Checklist pré-ouverture

- [ ] DNS OVH : `A` (+ `AAAA` si IPv6) sur `app.lloomi.fr`, TTL court,
      aucune redirection web OVH restante sur ce nom (§2.1).
- [ ] Bloc `docker/Caddyfile.fragment` (`app.lloomi.fr`) ajouté au
      Caddyfile du VPS, Caddy rattaché à `fripco-network`, reload
      effectué (§2.2).
- [ ] Certificat HTTPS obtenu pour `app.lloomi.fr` (`curl -I
      https://app.lloomi.fr` répond sans erreur TLS).
- [ ] `curl https://app.lloomi.fr/api/health` OK.
- [ ] L'autre application du VPS répond toujours sur ses propres domaines
      (§2.4, étape 2).
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
curl -s https://app.lloomi.fr/api/health
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

**Voie principale (PR5) : sauvegarde applicative planifiée**, gérée par
l'API elle-même — aucune crontab hôte à poser. Cron interne (APScheduler,
`app/jobs.py::run_nightly_database_backup`) à **03:00 Europe/Paris**
(aucun chevauchement avec les clôtures fiscales 00:15/00:30/23:59) : dump
`pg_dump | gzip` en sous-processus asynchrone, vérifié (relecture gzip
complète + présence de `CREATE TABLE public.journal_events`), empreinte
SHA-256, écrit dans `BACKUP_DIR` (`/app/data/backups`, sous le volume
`fripco_data` — persiste les redéploiements). Une ligne est journalisée
dans tous les cas (succès ou échec) ; un échec écrit aussi le JET
`system.job_failed` et alerte par e-mail (`backup.alert_email`, repli
`shop.email`). Écran **Administration → Sauvegardes** : état de la base,
réglages (rétention 7–3650 jours, activation du cron, e-mail d'alerte),
liste des sauvegardes (déclenchement manuel, téléchargement, suppression).
Purge automatique des fichiers et lignes plus vieux que la rétention
configurée.

**Sauvegarde hôte (`scripts/backup.sh`) : optionnelle, double ceinture.**
Reste disponible pour une copie indépendante du conteneur API (utile en
cas d'incident applicatif empêchant le cron interne de tourner) :

```bash
crontab -e
15 3 * * * /opt/fripco-street/scripts/backup.sh >> /var/log/fripco-backup.log 2>&1
```

Dump complet gzippé, vérifié (`gunzip -t` + présence de `journal_events`),
rétention 60 jours — même format de fichier (`fripco_YYYYMMDD_HHMMSS.sql.gz`)
que la sauvegarde applicative, pour une restauration identique.

**Restauration (à la main — aucune restauration depuis l'interface,
hors périmètre PR5)**, que le dump vienne de la voie applicative
(téléchargé depuis Administration → Sauvegardes) ou de `scripts/backup.sh` :

```bash
gunzip -c fripco_YYYYMMDD_HHMMSS.sql.gz | docker exec -i fripco-db psql -U fripco -d fripco
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

### 6.1 Lire le journal des échanges avec le terminal (PR9)

Quand un encaissement carte se passe mal, l'écran **Administration → Paiements
CB** répond à la question « qu'est-ce qui s'est réellement passé entre la caisse
et SumUp ». Trois blocs :

- **Échecs en attente** : les paniers dont le paiement n'a pas abouti pour une
  cause rattrapable (terminal muet, réseau coupé, panne côté SumUp). Une carte
  refusée n'y figure pas : il n'y a rien à relancer, la cliente change de moyen
  de paiement.
- **Analyse des échecs** (7 ou 30 jours) : les causes les plus fréquentes, la
  répartition par type (`transport` = la requête n'est jamais partie,
  `timeout` = pas de réponse à temps, `http_4xx` = SumUp a refusé la demande,
  `http_5xx` = panne chez SumUp, `decode` = réponse illisible) et le nombre de
  relances tentées.
- **Journal des échanges** : un appel HTTP par ligne, du plus récent au plus
  ancien, filtrable par erreurs seules, opération ou identifiant de paiement.
  Déplier une ligne montre la durée, le nombre d'essais et le détail envoyé et
  reçu.

Ce journal ne contient **ni clé API, ni en-tête d'authentification, ni numéro de
carte, ni donnée personnelle** : tout est expurgé avant écriture et la réponse
est tronquée à 4 Ko. C'est une table d'exploitation, hors périmètre fiscal :
elle se purge sans conséquence. La purge automatique tourne chaque nuit à 3h,
après la sauvegarde (le dump de la nuit contient donc encore ce qui va être
supprimé) ; la durée de conservation se règle dans `payments.exchange_retention_days`
(90 jours par défaut, entre 7 et 730). Le bouton **Vider le journal** purge tout
immédiatement ; ce geste est lui-même inscrit au journal des événements
(`sumup_exchanges.purged`), pour qu'un trou dans les traces ne passe jamais pour
un terminal silencieux.

En ligne de commande, pour une lecture rapide sur le VPS :

```bash
curl -s -H "Authorization: Bearer <jeton>" \
  "https://app.lloomi.fr/api/admin/sumup-exchanges?only_failed=true&limit=20" | jq
curl -s -H "Authorization: Bearer <jeton>" \
  "https://app.lloomi.fr/api/admin/payment-failures?days=7" | jq
```

## 7. E-mail des tickets et newsletter (PR3)

Compte Brevo **partagé** avec la boutique de Vernon : la caisse n'utilise que sa
liste dédiée « Frip & Co Street ». Variables `.env` : `BREVO_API_KEY`,
`BREVO_LIST_ID` (numéro de la liste dédiée, créée dans Brevo), `BREVO_WEBHOOK_TOKEN`
(secret partagé ; côté Brevo, URL du webhook `https://app.lloomi.fr/api/brevo/webhook?token=<secret>`
sur l'événement « désinscription »), `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME`,
et **`BREVO_ANONYMOUS_TRACKING=true`** uniquement si le compte Brevo est réglé en
suivi d'ouverture anonyme (CNIL) ; sinon les tickets partent par SMTP
(`SMTP_*`) ou ne partent pas (mode simulation, signalé en administration).
La caisse ne touche jamais à la blocklist globale d'un contact ni ne supprime
un contact Brevo : un désabonnement ou une suppression RGPD retire le contact
de la liste dédiée et anonymise la fiche locale.

### 7.1 Suppression RGPD différée (PR10)

Une demande de suppression n'efface plus la fiche le jour même : elle pose une
**date d'effet 30 jours plus tard**, pendant lesquels tout reste réversible.
La cliente reçoit un accusé de réception par e-mail (date d'effet, possibilité
d'annuler en boutique, contact du DPO) et la fiche continue de fonctionner
normalement en caisse — la personne est toujours cliente jusqu'à l'échéance.
La demande s'annule depuis la fiche (Administration → Clients), et « Supprimer
immédiatement » reste disponible pour les cas où la cliente insiste.

- **Délai réglable** : `rgpd.deletion_delay_days`, de 1 à 90 jours,
  `PUT /api/admin/settings/rgpd` (défaut 30). Le raccourcir n'affecte que les
  demandes suivantes : une demande déjà posée garde sa date d'effet.
- **Cron interne** (APScheduler, `app/jobs.py::run_daily_client_deletions`) à
  **04:00 Europe/Paris** — après la sauvegarde de 03:00, de sorte que le dump
  de la nuit contient encore les fiches qui vont être vidées : une erreur reste
  rattrapable pendant toute la durée de rétention. À l'échéance, la fiche est
  anonymisée (jamais supprimée : les ventes gardent leur historique comptable)
  et le contact est retiré de la liste Brevo dédiée. Un échec est journalisé au
  JET (`system.job_failed`) et signalé par e-mail à l'adresse de la boutique.

## 8. Imprimante ticket et tiroir-caisse (PR3b)

Matériel : imprimante ticket ESC/POS 80 mm **MUNBYN 047P** et tiroir-caisse
**Safescan SD-4141** branché sur l'imprimante (RJ-12, impulsion). Deux modes,
réglés dans Administration → Matériel (aucune variable d'environnement) :

- **Réseau (Wi-Fi)** : l'imprimante est sur le réseau de la boutique avec une IP
  fixe (réservation DHCP) et écoute en TCP 9100. L'API du VPS doit pouvoir
  joindre cette IP : ce n'est possible que si la boutique est accessible depuis
  le VPS (VPN, IP publique + NAT) — sinon utiliser le mode USB.
- **USB (tablette)** : imprimante branchée en USB-OTG sur la tablette Android ;
  Chrome envoie les octets ESC/POS directement (WebUSB, HTTPS obligatoire).
  Association une fois depuis Administration → Matériel → « Associer
  l'imprimante USB ».

Impression automatique à chaque vente et ouverture automatique du tiroir sur
les ventes en espèces sont des options du même écran. Chaque impression et
chaque ouverture de tiroir sont journalisées (JET `receipt.printed`,
`drawer.kicked`) ; les réimpressions sont comptées sur le ticket.

## 9. Clôtures, archives et exports (PR4)

- Clôtures **mensuelle** (1er du mois à 00:15) et **annuelle** (1er janvier à 00:30,
  heure de Paris) automatiques ; refusées si une caisse est ouverte ou si une
  chaîne est rompue (échec journalisé et signalé par e-mail à l'adresse de la
  boutique). Contrôle et clôture manuelle : Administration → Archives fiscales.
- **Copie hors site obligatoire** après chaque clôture : télécharger l'archive
  (`.json.gz`) et noter son empreinte SHA-256 ; la conserver sur deux supports
  distincts du VPS (conservation 6 ans). L'archive se lit sans l'application
  (`gunzip`, JSON) ; son empreinte se vérifie avec `sha256sum`.
- Exports comptables (Administration → Comptabilité) : CSV mensuel au format
  Pennylane et FEC, à transmettre au cabinet ; exports bruts par table pour
  contrôle.
- La clé `FISCAL_SIGNING_KEY` est indispensable pour vérifier les signatures :
  la conserver hors ligne, sous accès restreint, avec les archives.

## 10. Météo locale (PR11)

La carte météo de l'accueil et du cahier du jour interroge OpenWeather.

- **Clé** : `OPENWEATHER_API_KEY` dans `.env` (compte gratuit sur
  api.openweathermap.org). C'est un secret : il n'est jamais écrit en base,
  jamais renvoyé par l'API (l'écran Réglages → Météo affiche seulement
  « clé configurée » ou « clé absente ») et jamais journalisé. Le conteneur
  API la reçoit par `env_file: ../.env`
  (`docker/docker-compose.prod.yml`) — pas par une interpolation `${…}`,
  qui irait chercher le `.env` du dossier `docker/` et écraserait la vraie
  valeur par une chaîne vide.
- **Lieu** : Réglages → Météo (ville, et facultativement latitude et
  longitude, qui priment quand elles sont renseignées — deux communes
  homonymes ne se départagent pas autrement). Ville vide : repli sur la
  ville de la fiche boutique. Aucun géocodage n'est fait.
- **Sans clé, ou si OpenWeather ne répond pas** : la carte affiche « Météo
  indisponible » et la raison courte. Rien d'autre ne change — aucune page
  ne tombe, aucune vente n'est affectée. Un appel sortant au plus par quart
  d'heure (cache en mémoire de l'API), donc pas de quota à surveiller.

## 11. Déploiement automatique

`.github/workflows/deploy.yml` (actif dans le dépôt dédié) : après une CI verte
sur `main`, connexion SSH et `./scripts/deploy.sh --pull`. Secrets à créer dans
le dépôt : `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_PORT` (optionnel).
