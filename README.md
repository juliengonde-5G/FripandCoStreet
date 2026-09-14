# Frip & Co Street — caisse boutique éphémère

Application de caisse **isolée** (Rouen, sept. 2026 → janv. 2027), extraite
des modules éprouvés de Vintiz : vente en saisie libre, espèces, CB SumUp,
ticket par e-mail, contacts newsletter, exports comptables — sous régime
NF525 par auto-attestation. Cahier des charges : `CDC_Caisse_FripCo_Street.md`.

```
apps/api/    FastAPI (Python 3.11) — auth mono-compte, JET chaîné, migrations Alembic
apps/web/    Next.js 15 — connexion, caisse, administration
docker/      Compose prod/dev, Dockerfiles, init des rôles PostgreSQL, fragment Caddy
scripts/     deploy.sh, backup.sh
docs/        DEPLOIEMENT.md
```

## Démarrage rapide (dev)

```bash
# Base (PostgreSQL 16 avec les deux rôles) — port 5433
docker compose -f docker/docker-compose.yml up -d

# API
cd apps/api && cp .env.example .env   # DATABASE_URL vers localhost:5433
pip install -e ".[dev]"
python -m alembic upgrade head
python scripts/create_manager.py --username admin --email admin@example.org
uvicorn app.main:app --reload --port 8000

# Front — API_PROXY_TARGET fait relayer /api/* par Next vers l'API
# (reproduit le same-origin de la prod ; évite CORS et le piège
# localhost -> IPv6 avec uvicorn qui écoute en 127.0.0.1). Voir
# apps/web/next.config.ts et apps/web/.env.local.example.
cd apps/web && npm install
API_PROXY_TARGET=http://127.0.0.1:8000 npm run dev   # http://localhost:3000
```

## Tests

Les tests de l'API tournent **exclusivement sur PostgreSQL** : les triggers
d'inaltérabilité et le chaînage du journal sont exercés pour de vrai.

```bash
cd apps/api
TEST_DATABASE_URL=postgresql+asyncpg://fripco:fripco@localhost:5432/fripco_test python -m pytest
python -m ruff check .
cd ../web && npm run lint && npx tsc --noEmit && npm run build
```

## Plan de livraison

| PR | Contenu | État |
|---|---|---|
| PR0 | Audit d'extraction + écarts NF525 | livré |
| PR1 | Squelette : auth mono-compte, JET chaîné, migration initiale, Docker, proxy, backup | **cette PR** |
| PR2 | Vente + espèces + tickets + Z + SumUp | à venir |
| PR3 | Client / e-mail Brevo / newsletter | à venir |
| PR4 | Exports, archive fiscale, clôtures, attestation | à venir |
