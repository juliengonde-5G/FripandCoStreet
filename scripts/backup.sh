#!/usr/bin/env bash
# ===========================================
# Frip & Co Street — sauvegarde quotidienne PostgreSQL (cron hôte)
# ===========================================
# crontab -e :  15 3 * * * /opt/fripco-street/scripts/backup.sh >> /var/log/fripco-backup.log 2>&1
# Dump complet (pg_dump | gzip) du conteneur fripco-db, rétention 60 jours.
# Restauration :  gunzip -c fichier.sql.gz | docker exec -i fripco-db psql -U fripco -d fripco
#
# ATTENTION : les données de caisse sont des données fiscales (conservation
# 6 ans). Ce script est une sauvegarde d'exploitation ; l'archive fiscale
# signée (clôtures) est produite par l'application (PR4) et doit être copiée
# sur un support externe.

set -euo pipefail

_SELF="${BASH_SOURCE[0]:-$0}"
PROJECT_DIR="$(cd "$(dirname "$_SELF")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
BACKUP_DIR="${FRIPCO_BACKUP_DIR:-$PROJECT_DIR/backups}"
RETENTION_DAYS="${FRIPCO_BACKUP_RETENTION_DAYS:-60}"
DATE=$(date +%Y%m%d_%H%M%S)

read_env_value() {
  local line
  line=$(grep -E "^[[:space:]]*$1[[:space:]]*=" "$ENV_FILE" | tail -n 1 || true)
  line="${line#*=}"; line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
  printf '%s' "$line"
}
PG_USER="$(read_env_value POSTGRES_USER)"
PG_DB="$(read_env_value POSTGRES_DB)"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
OUT="$BACKUP_DIR/fripco_${DATE}.sql.gz"

echo "[$(date)] Sauvegarde PostgreSQL Frip & Co Street…"
docker exec fripco-db pg_dump -U "$PG_USER" "$PG_DB" | gzip > "$OUT"
# Vérification minimale : l'archive se relit et contient le journal.
gunzip -t "$OUT"
gunzip -c "$OUT" | grep -q "journal_events" || { echo "Sauvegarde suspecte : table journal_events absente" >&2; exit 1; }
echo "[$(date)] Sauvegarde créée : $(basename "$OUT") ($(du -h "$OUT" | cut -f1))"

find "$BACKUP_DIR" -name "fripco_*.sql.gz" -mtime +"$RETENTION_DAYS" -delete
echo "[$(date)] Rétention : sauvegardes de plus de ${RETENTION_DAYS} jours supprimées."
