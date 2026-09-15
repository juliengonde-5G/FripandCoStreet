#!/usr/bin/env bash
# ===========================================
# Frip & Co Street — génération des secrets (.env)
# ===========================================
# Usage : ./scripts/setup_env.sh
#
# Crée .env depuis .env.example et génère automatiquement les secrets
# techniques (mots de passe PostgreSQL, SECRET_KEY, FISCAL_SIGNING_KEY).
# Les identifiants SumUp (SUMUP_API_KEY, SUMUP_MERCHANT_CODE, SUMUP_READER_ID)
# restent à saisir à la main — voir docs/DEPLOIEMENT.md §6.
#
# Ne s'exécute qu'une seule fois : si .env existe déjà, le script refuse de
# le toucher. FISCAL_SIGNING_KEY est définitive (docs/DEPLOIEMENT.md) — la
# régénérer invaliderait la vérification de tout l'historique signé.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_EXAMPLE="$PROJECT_DIR/.env.example"
ENV_FILE="$PROJECT_DIR/.env"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

[ -f "$ENV_EXAMPLE" ] || { err ".env.example introuvable"; exit 1; }
if [ -f "$ENV_FILE" ]; then
  err ".env existe déjà — le script ne le régénère pas (FISCAL_SIGNING_KEY est définitive)."
  err "Pour changer un secret ponctuel (ex. rotation SUMUP_API_KEY), éditez .env à la main."
  exit 1
fi
command -v openssl >/dev/null || { err "openssl requis"; exit 1; }

log "Génération des secrets…"
POSTGRES_PASSWORD=$(openssl rand -hex 32)
FRIPCO_APP_PASSWORD=$(openssl rand -hex 32)
SECRET_KEY=$(openssl rand -hex 32)
FISCAL_SIGNING_KEY=$(openssl rand -hex 32)

cp "$ENV_EXAMPLE" "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Valeurs générées par openssl (hex) : pas de caractère spécial à échapper pour sed.
# Les motifs les plus longs sont substitués en premier (l'un est préfixe de l'autre).
sed -i \
  -e "s#CHANGER_MOI_generer_avec_openssl_rand_hex_32_independant#${FISCAL_SIGNING_KEY}#g" \
  -e "s#CHANGER_MOI_generer_avec_openssl_rand_hex_32#${SECRET_KEY}#g" \
  -e "s#CHANGER_MOI_autre_mot_de_passe_fort_64chars#${FRIPCO_APP_PASSWORD}#g" \
  -e "s#CHANGER_MOI_mot_de_passe_fort_64chars#${POSTGRES_PASSWORD}#g" \
  "$ENV_FILE"

if grep -vE '^[[:space:]]*#' "$ENV_FILE" | grep -q "CHANGER_MOI"; then
  err "Des valeurs CHANGER_MOI subsistent dans .env — vérifiez .env.example (motif non reconnu)."
  grep -vE '^[[:space:]]*#' "$ENV_FILE" | grep -n "CHANGER_MOI" | sed 's/=.*/=…/' | sed 's/^/  ligne /'
  exit 1
fi

log ".env créé (permissions 600) : mots de passe DB, SECRET_KEY et FISCAL_SIGNING_KEY générés."
warn "Encore à saisir à la main dans .env : SUMUP_API_KEY, SUMUP_MERCHANT_CODE, SUMUP_READER_ID"
warn "(docs/DEPLOIEMENT.md §6 — sans ces trois variables ou TPE hors ligne, espèces uniquement)."
echo ""
warn "FISCAL_SIGNING_KEY est DÉFINITIVE. Sauvegardez-la MAINTENANT hors ligne"
warn "(gestionnaire de mots de passe, accès restreint). Une perte ou une rotation"
warn "invalide la vérification de tout l'historique signé :"
echo ""
echo "    FISCAL_SIGNING_KEY=${FISCAL_SIGNING_KEY}"
echo ""
warn "Cette valeur reste visible dans l'historique de ce terminal — pensez à le purger si besoin."
