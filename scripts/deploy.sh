#!/usr/bin/env bash
# ===========================================
# Frip & Co Street — déploiement production
# ===========================================
# Usage :
#   ./scripts/deploy.sh            Build + migrations + redémarrage (code local tel quel)
#   ./scripts/deploy.sh --pull     Idem, après `git fetch` + reset sur origin/$DEPLOY_BRANCH
#   ./scripts/deploy.sh --rollback Revenir au commit du déploiement précédent (implique --pull)
#
# Pré-requis serveur : Docker Engine 24+ avec Compose v2, un fichier .env
# rempli (cp .env.example .env), le reverse-proxy du VPS rattaché au réseau
# `fripco-network` avec le bloc `app.lloomi.fr` (docker/Caddyfile.fragment).
#
# Le script fonctionne aussi bien depuis un clone dédié (/opt/fripco-street)
# que depuis le sous-dossier fripco-street/ d'un autre dépôt : tous les
# chemins sont relatifs à lui-même, et il ne touche à git qu'avec --pull.

set -eo pipefail

_SELF="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker/docker-compose.prod.yml"
ENV_FILE="$PROJECT_DIR/.env"
ROLLBACK_FILE="$PROJECT_DIR/.deploy_rollback"
NETWORK_NAME="fripco-network"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }
step() { echo -e "\n${BLUE}[$1]${NC} $2"; }

PULL=false
ROLLBACK=false
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
for arg in "$@"; do
  case "$arg" in
    --pull)      PULL=true ;;
    --rollback)  ROLLBACK=true; PULL=true ;;
    --branch=*)  DEPLOY_BRANCH="${arg#--branch=}"; PULL=true ;;
    --help|-h)
      sed -n '2,16p' "$_SELF"; exit 0 ;;
    *) err "Option inconnue : $arg"; exit 2 ;;
  esac
done

PROJECT_NAME="fripco-street"
compose() { docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

read_env_value() {
  local key="$1" line
  line=$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE" | tail -n 1 || true)
  line="${line#*=}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  if [[ "$line" == \"*\" ]] || [[ "$line" == \'*\' ]]; then line="${line:1:${#line}-2}"; fi
  printf '%s' "$line"
}

echo ""
echo "============================================"
echo "  Frip & Co Street — Déploiement production"
echo "============================================"

# ------------------------------------------------------------ 0. vérifications
step "0/5" "Vérification de l'environnement…"
[ -f "$ENV_FILE" ] || { err "Fichier .env manquant (cp .env.example .env)"; exit 1; }
if grep -vE '^[[:space:]]*#' "$ENV_FILE" | grep -q "CHANGER_MOI"; then
  err "Le fichier .env contient encore des valeurs CHANGER_MOI :"
  grep -vE '^[[:space:]]*#' "$ENV_FILE" | grep -n "CHANGER_MOI" | sed 's/=.*/=…/' | sed 's/^/  ligne /'
  exit 1
fi
[ "$(read_env_value ENVIRONMENT)" = "production" ] || { err "ENVIRONMENT=production est obligatoire dans .env"; exit 1; }
FISCAL_KEY=$(read_env_value FISCAL_SIGNING_KEY)
[ "${#FISCAL_KEY}" -ge 32 ] || { err "FISCAL_SIGNING_KEY absente ou < 32 caractères (openssl rand -hex 32)"; exit 1; }
unset FISCAL_KEY
[ -n "$(read_env_value FRIPCO_APP_PASSWORD)" ] || { err "FRIPCO_APP_PASSWORD manquant"; exit 1; }
command -v docker >/dev/null || { err "Docker non installé"; exit 1; }
docker compose version >/dev/null 2>&1 || { err "Docker Compose v2 requis"; exit 1; }
log "Environnement OK"

# ------------------------------------------------------------ 1. code
cd "$PROJECT_DIR"
CURRENT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
if $ROLLBACK; then
  [ -f "$ROLLBACK_FILE" ] || { err "Aucun déploiement précédent enregistré"; exit 1; }
  PREV=$(cat "$ROLLBACK_FILE"); warn "Rollback vers $PREV"; git checkout "$PREV"
elif $PULL; then
  step "1/5" "Récupération du code (branche $DEPLOY_BRANCH)…"
  echo "$CURRENT_COMMIT" > "$ROLLBACK_FILE"
  git fetch origin "$DEPLOY_BRANCH" && git reset --hard "origin/$DEPLOY_BRANCH"
else
  step "1/5" "Code local utilisé tel quel ($CURRENT_COMMIT) — pas de git pull (--pull pour l'activer)"
  echo "$CURRENT_COMMIT" > "$ROLLBACK_FILE"
fi
NEW_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
log "Commit déployé : $NEW_COMMIT"

# ------------------------------------------------------------ 2. réseau + build
step "2/5" "Réseau $NETWORK_NAME + build des images…"
if ! docker network inspect "$NETWORK_NAME" >/dev/null 2>&1; then
  docker network create "$NETWORK_NAME" >/dev/null
  log "Réseau $NETWORK_NAME créé (le reverse-proxy doit s'y rattacher, voir docs/DEPLOIEMENT.md)"
fi
export FRIPCO_BUILD_SHA="$NEW_COMMIT"
export FRIPCO_BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
compose build
log "Images construites (sha=$FRIPCO_BUILD_SHA)"

# ------------------------------------------------------------ 3. base + migrations
step "3/5" "Base de données et migrations Alembic…"
compose up -d db
DB_OK=false
for _ in $(seq 1 20); do
  if docker exec fripco-db pg_isready -U "$(read_env_value POSTGRES_USER)" -q 2>/dev/null; then DB_OK=true; break; fi
  sleep 3
done
$DB_OK || { err "PostgreSQL : timeout"; docker logs fripco-db --tail 20; exit 1; }
log "PostgreSQL prêt"
# Migrations sous le rôle PROPRIÉTAIRE (MIGRATION_DATABASE_URL) ; l'API refuse
# de démarrer si la révision de la base n'est pas celle attendue.
if compose run --rm --no-deps api python -m alembic upgrade head; then
  log "Migrations : OK"
else
  err "Migrations échouées — API non démarrée"; exit 1
fi

# ------------------------------------------------------------ 4. démarrage
step "4/5" "Démarrage des conteneurs…"
compose up -d --remove-orphans
API_OK=false
for _ in $(seq 1 40); do
  if docker exec fripco-api python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=3)" >/dev/null 2>&1; then API_OK=true; break; fi
  sleep 3
done
$API_OK || { err "API : timeout"; docker logs fripco-api --tail 30; warn "Rollback : ./scripts/deploy.sh --rollback"; exit 1; }
log "API : OK"

# ------------------------------------------------------------ 5. état final
step "5/5" "État final"
compose ps
echo ""
if [ -z "$(docker exec fripco-db psql -U "$(read_env_value POSTGRES_USER)" -d "$(read_env_value POSTGRES_DB)" -Atc 'SELECT 1 FROM users LIMIT 1' 2>/dev/null)" ]; then
  warn "Aucun compte : créer l'unique manager avec"
  echo "    docker exec -it fripco-api python scripts/create_manager.py --username <nom> --email <email>"
fi
log "Déploiement terminé — https://app.lloomi.fr"
