#!/usr/bin/env bash
# ===========================================
# Frip & Co Street — tests de fumée post-déploiement (PR12, contrat N6)
# ===========================================
# Usage :
#   ./scripts/smoke_prod.sh                      # https://app.lloomi.fr
#   ./scripts/smoke_prod.sh http://127.0.0.1:8000
#
# Neuf contrôles de LECTURE SEULE, lancés juste après un déploiement, pour
# répondre à la seule question qui compte à ce moment-là : « est-ce que ce
# qui vient d'être déployé est bien ce qui répond, et est-ce qu'il répond ? »
# Aucune écriture, aucun jeton, aucune vente : le script peut tourner autant
# de fois qu'on veut, y compris en pleine journée de caisse.
#
# Une ligne par contrôle, `[OK]` ou `[KO]`. Le script continue après un `[KO]`
# (on veut le tableau complet, pas le premier échec) et sort en 1 s'il y en a
# au moins un.
#
# Dépendances : bash, curl et python3 — tous présents sur le VPS. Pas
# d'openssl, pas de jq : la date d'expiration du certificat et la validité du
# JSON se lisent en python3 (bibliothèque standard).

set -uo pipefail

_SELF="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

BASE_URL="${1:-https://app.lloomi.fr}"
BASE_URL="${BASE_URL%/}"

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
FAILURES=0
ok() { echo -e "${GREEN}[OK]${NC} $*"; }
ko() { echo -e "${RED}[KO]${NC} $*"; FAILURES=$((FAILURES + 1)); }

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# `curl` silencieux : écrit le corps dans $TMP/body et renvoie le code HTTP
# sur stdout (000 si la requête n'est jamais partie).
fetch() {
  local code
  : > "$TMP/body"
  # curl écrit lui-même "000" quand la requête n'est jamais partie ; on ne
  # rajoute donc pas de repli qui doublerait le code.
  code=$(curl -sS -o "$TMP/body" -w '%{http_code}' --max-time 15 "$1" 2>"$TMP/curl.err")
  printf '%s' "${code:-000}"
}

echo ""
echo "============================================"
echo "  Tests de fumée — $BASE_URL"
echo "============================================"
echo ""

# ------------------------------------------------------------------ 1. santé
# La sonde de santé dit trois choses : que c'est bien NOTRE API qui répond
# (et pas un autre vhost du VPS attrapé par erreur), que le schéma de base
# attendu par le code est celui de la tête Alembic du dépôt, et que le build
# déployé est bien le commit courant. C'est le contrôle qui attrape le cas le
# plus vicieux : un déploiement « réussi » qui sert l'image précédente.
HEAD_REVISION=$(ls "$PROJECT_DIR/apps/api/alembic/versions" 2>/dev/null \
  | grep -E '^[0-9]+_.*\.py$' | sort | tail -n 1 | cut -d_ -f1)
DEPLOYED_SHA=$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || true)

code=$(fetch "$BASE_URL/api/health")
if [ "$code" != "200" ]; then
  ko "1/9 /api/health répond $code (attendu 200)"
else
  health_app=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('app',''))" "$TMP/body" 2>/dev/null)
  health_rev=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('expected_db_revision',''))" "$TMP/body" 2>/dev/null)
  health_sha=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('build_sha',''))" "$TMP/body" 2>/dev/null)
  problems=""
  [ "$health_app" = "fripco-street-api" ] || problems="$problems app=$health_app;"
  [ -n "$HEAD_REVISION" ] && [ "$health_rev" != "$HEAD_REVISION" ] \
    && problems="$problems révision=$health_rev au lieu de $HEAD_REVISION;"
  [ -n "$DEPLOYED_SHA" ] && [ "$health_sha" != "$DEPLOYED_SHA" ] \
    && problems="$problems build=$health_sha au lieu de $DEPLOYED_SHA;"
  if [ -n "$problems" ]; then
    ko "1/9 /api/health :$problems"
  else
    ok "1/9 /api/health — $health_app, schéma $health_rev, build $health_sha"
  fi
fi

# ------------------------------------------------------------- 2. page d'accueil
# `&` est échappé en `&amp;` dans le HTML rendu : les deux écritures valent.
code=$(fetch "$BASE_URL/")
if [ "$code" != "200" ]; then
  ko "2/9 / répond $code (attendu 200)"
elif grep -qE 'Frip (&|&amp;) Co Street' "$TMP/body"; then
  ok "2/9 / — page servie, nom de la boutique présent"
else
  ko "2/9 / répond 200 mais ne contient pas le nom de la boutique"
fi

# ------------------------------------------------------------ 3. écran de connexion
code=$(fetch "$BASE_URL/login")
[ "$code" = "200" ] && ok "3/9 /login — écran de connexion servi" \
  || ko "3/9 /login répond $code (attendu 200)"

# ----------------------------------------------------------------- 4. manifeste
# Sans manifeste valide, Chrome ne propose plus d'installer la caisse sur la
# tablette : la vendeuse se retrouve dans un onglet ordinaire.
code=$(fetch "$BASE_URL/manifest.webmanifest")
if [ "$code" != "200" ]; then
  ko "4/9 /manifest.webmanifest répond $code (attendu 200)"
elif python3 -c "import json,sys;json.load(open(sys.argv[1]))" "$TMP/body" 2>/dev/null; then
  ok "4/9 /manifest.webmanifest — JSON valide"
else
  ko "4/9 /manifest.webmanifest répond 200 mais n'est pas du JSON valide"
fi

# ------------------------------------------------------------ 5. service worker
code=$(fetch "$BASE_URL/sw.js")
[ "$code" = "200" ] && ok "5/9 /sw.js — service worker servi" \
  || ko "5/9 /sw.js répond $code (attendu 200)"

# ---------------------------------------------------------------- 6. OpenAPI
code=$(fetch "$BASE_URL/api/openapi.json")
[ "$code" = "200" ] && ok "6/9 /api/openapi.json — description de l'API servie" \
  || ko "6/9 /api/openapi.json répond $code (attendu 200)"

# ------------------------------------------------------------ 7. certificat TLS
# Un certificat expiré, c'est une caisse muette du jour au lendemain : on
# prévient deux semaines avant. En http (faux serveur local, mise au point),
# le contrôle n'a pas d'objet et le dit.
host="${BASE_URL#*://}"; host="${host%%/*}"; host="${host%%:*}"
if [ "${BASE_URL%%://*}" != "https" ]; then
  ok "7/9 Certificat TLS — sans objet (URL en http)"
else
  days=$(python3 - "$host" <<'PY' 2>/dev/null
import socket, ssl, sys, time
host = sys.argv[1]
ctx = ssl.create_default_context()
with socket.create_connection((host, 443), timeout=10) as raw:
    with ctx.wrap_socket(raw, server_hostname=host) as tls:
        cert = tls.getpeercert()
print(int((ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) // 86400))
PY
)
  if [ -z "$days" ]; then
    ko "7/9 Certificat TLS — illisible (handshake impossible)"
  elif [ "$days" -gt 14 ]; then
    ok "7/9 Certificat TLS — valide encore $days jour(s)"
  else
    ko "7/9 Certificat TLS — expire dans $days jour(s) (seuil : 14)"
  fi
fi

# ------------------------------------------------------------ 8. temps de réponse
# 2 s, c'est déjà lent pour une sonde qui ne fait rien : au-delà, quelque
# chose sature (base, proxy, conteneur) et la caisse le paiera en vente.
elapsed=$(curl -sS -o /dev/null -w '%{time_total}' --max-time 15 "$BASE_URL/api/health" 2>/dev/null)
curl_rc=$?
# Une requête qui échoue renvoie quand même une durée (minuscule) : sans le
# code de retour, un serveur éteint passerait pour un serveur rapide.
if [ "$curl_rc" -ne 0 ] || [ -z "$elapsed" ]; then
  ko "8/9 Temps de réponse de /api/health — mesure impossible (curl $curl_rc)"
elif python3 -c "import sys;sys.exit(0 if float(sys.argv[1]) < 2.0 else 1)" "$elapsed"; then
  ok "8/9 Temps de réponse de /api/health — ${elapsed}s (seuil : 2s)"
else
  ko "8/9 Temps de réponse de /api/health — ${elapsed}s (seuil : 2s)"
fi

# -------------------------------------------------- 9. l'administration n'est pas publique
# La supervision technique expose l'état de la base, des sauvegardes et des
# files : sans jeton, elle doit refuser. Un 200 ici serait une fuite.
code=$(fetch "$BASE_URL/api/admin/monitoring")
[ "$code" = "401" ] && ok "9/9 /api/admin/monitoring sans jeton — refusé (401)" \
  || ko "9/9 /api/admin/monitoring sans jeton répond $code (attendu 401)"

echo ""
if [ "$FAILURES" -eq 0 ]; then
  echo -e "${GREEN}Tests de fumée : 9/9 OK${NC}"
  exit 0
fi
echo -e "${RED}Tests de fumée : $FAILURES contrôle(s) en échec${NC}"
exit 1
