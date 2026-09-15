#!/usr/bin/env bash
# ===========================================
# Frip & Co Street — configuration TPE SumUp
# ===========================================
# Usage : ./scripts/setup_sumup.sh
#
# Renseigne interactivement dans .env les 3 identifiants SumUp
# (SUMUP_API_KEY, SUMUP_MERCHANT_CODE, SUMUP_READER_ID) — voir
# docs/DEPLOIEMENT.md §6. Sans ces trois variables ou TPE hors ligne,
# la caisse n'accepte que les espèces.
#
# Ne touche à rien d'autre dans .env. Les valeurs ne sont jamais
# affichées en clair à l'écran ni journalisées ; seule leur longueur
# est montrée pour confirmer qu'une valeur existante n'est pas vide.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

[ -f "$ENV_FILE" ] || { err ".env introuvable (cp .env.example .env, ou ./scripts/setup_env.sh)"; exit 1; }
chmod 600 "$ENV_FILE"

current_value() {
  grep -E "^${1}=" "$ENV_FILE" | tail -n1 | cut -d= -f2-
}

set_value() {
  local key="$1" value="$2" escaped
  escaped=$(printf '%s' "$value" | sed -e 's/[&#\]/\\&/g')
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s#^${key}=.*#${key}=${escaped}#" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

prompt_field() {
  local key="$1" label="$2" hidden="$3" existing new confirm
  existing=$(current_value "$key")
  if [ -n "$existing" ]; then
    warn "${label} : une valeur est déjà présente (${#existing} caractères)."
    read -r -p "  Remplacer ? [y/N] " confirm
    case "$confirm" in [yY]*) ;; *) log "${label} inchangé."; return 0 ;; esac
  fi
  if [ "$hidden" = "hidden" ]; then
    read -r -s -p "${label} : " new; echo
  else
    read -r -p "${label} : " new
  fi
  [ -n "$new" ] || { warn "${label} laissé vide."; return 0; }
  set_value "$key" "$new"
  log "${label} enregistré."
}

echo "=== Configuration TPE SumUp (docs/DEPLOIEMENT.md §6) ==="
echo "Clé de PRODUCTION uniquement (sup_sk_…) : une clé sup_sk_test_… est"
echo "refusée par SumUp en production."
echo ""

prompt_field SUMUP_API_KEY       "SUMUP_API_KEY (sup_sk_…)" hidden
KEY_NOW=$(current_value SUMUP_API_KEY)
if [[ "$KEY_NOW" == sup_sk_test_* ]]; then
  warn "SUMUP_API_KEY ressemble à une clé de TEST (sup_sk_test_…) — à corriger avant le déploiement."
fi

prompt_field SUMUP_MERCHANT_CODE "SUMUP_MERCHANT_CODE" plain
prompt_field SUMUP_READER_ID     "SUMUP_READER_ID (GET /v0.1/merchants/{code}/readers)" plain

echo ""
log "Terminé."
warn "SUMUP_READER_ID introuvable ? Récupère-le avec :"
echo "    curl -H \"Authorization: Bearer \$SUMUP_API_KEY\" https://api.sumup.com/v0.1/merchants/\$SUMUP_MERCHANT_CODE/readers"
warn "Sans les 3 variables remplies (ou TPE hors ligne), la caisse reste en espèces uniquement."
