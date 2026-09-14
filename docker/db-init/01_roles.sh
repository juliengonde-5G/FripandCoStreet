#!/bin/bash
# Exécuté UNE fois par l'image postgres au premier démarrage (data dir vide).
# Crée le rôle applicatif non propriétaire et lui donne les droits DML sur
# toutes les tables présentes et futures du schéma public (les tables sont
# créées par les migrations Alembic sous le rôle propriétaire POSTGRES_USER).
#
# Pourquoi : un rôle propriétaire peut `ALTER TABLE ... DISABLE TRIGGER` et
# donc contourner les triggers d'inaltérabilité NF525. Le rôle applicatif ne
# le peut pas — l'inaltérabilité devient opposable à l'application elle-même
# (écart I-3 du rapport d'audit PR0).
set -euo pipefail

: "${FRIPCO_APP_USER:=fripco_app}"
: "${FRIPCO_APP_PASSWORD:?FRIPCO_APP_PASSWORD est obligatoire}"

psql -v ON_ERROR_STOP=1 \
     -v app_user="$FRIPCO_APP_USER" -v app_password="$FRIPCO_APP_PASSWORD" \
     -v owner="$POSTGRES_USER" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
              :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec

GRANT CONNECT ON DATABASE :"DBNAME" TO :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";
-- Le rôle applicatif ne crée jamais d'objet : pas de CREATE sur le schéma.
REVOKE CREATE ON SCHEMA public FROM :"app_user";

-- Objets existants (aucun au premier démarrage, mais idempotent).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_user";

-- Objets futurs créés par le propriétaire (migrations Alembic).
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
SQL

echo "[db-init] rôle applicatif '$FRIPCO_APP_USER' prêt (non propriétaire, DML seulement)"
