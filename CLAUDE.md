# Frip & Co Street — guide de développement

Caisse isolée, mono-boutique, mono-utilisateur. Régime NF525 par
auto-attestation : tout ce qui touche ventes, paiements, tiroirs, Z, clôtures
et journal des événements est **immuable** (chaînage HMAC + triggers
PostgreSQL) et toute modification de ces mécanismes est une évolution fiscale.

## Règles non négociables

- **Aucune dépendance runtime vers Vintiz** : le code est copié, jamais appelé.
  Pas d'hôte `*.vintiz.fr`, pas de conteneur `vintiz-*` (test `tests/test_isolation.py`).
- **Schéma piloté par Alembic** : jamais `create_all`. Les triggers
  d'inaltérabilité vivent dans les migrations. L'API refuse de démarrer si
  `alembic_version` ≠ `app/version.py::EXPECTED_DB_REVISION`.
- **Tests sur PostgreSQL uniquement** (`TEST_DATABASE_URL`), jamais SQLite.
- **Deux rôles PostgreSQL** : propriétaire pour les migrations
  (`MIGRATION_DATABASE_URL`), applicatif non propriétaire pour l'API
  (`DATABASE_URL`).
- **JET** (`journal_events`) : chaque événement technique est chaîné
  (HMAC-SHA256, `FISCAL_SIGNING_KEY`). Un échec d'écriture du JET fait échouer
  la requête ; on n'avale jamais l'exception.
- Aucun secret dans le code : `.env` uniquement.
- Pas de SMS, pas de matériel autre que le TPE SumUp, pas de stock, pas d'IA.

## Stack

FastAPI + SQLAlchemy async + asyncpg + Alembic · Next.js 15 (App Router) +
Tailwind · PostgreSQL 16 · Docker Compose · Caddy (reverse-proxy partagé du VPS).

## Commandes

```bash
cd apps/api && python -m ruff check . && TEST_DATABASE_URL=postgresql+asyncpg://fripco:fripco@localhost:5432/fripco_test python -m pytest -q
cd apps/web && npm run lint && npx tsc --noEmit && npm run build
./scripts/deploy.sh   # sur le VPS
```

Voir `docs/DEPLOIEMENT.md`.
