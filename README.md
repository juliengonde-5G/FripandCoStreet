# Frip & Co Street — caisse boutique éphémère

Application de caisse **isolée** (Rouen, sept. 2026 → janv. 2027), extraite
des modules éprouvés de l'application source : vente en saisie libre, espèces, CB SumUp,
ticket par e-mail, contacts newsletter, exports comptables — sous régime
NF525 par auto-attestation. Cahier des charges : `CDC_Caisse_FripCo_Street.md`.

```
apps/api/    FastAPI (Python 3.11) — auth, JET chaîné, ventes/Z signés (HMAC v3), SumUp, migrations Alembic
apps/web/    Next.js 15 — connexion, caisse, administration (charte : docs/CHARTE_GRAPHIQUE.md)
docker/      Compose prod/dev, Dockerfiles, init des rôles PostgreSQL, fragment Caddy
scripts/     deploy.sh, backup.sh
docs/        DEPLOIEMENT.md, ARCHITECTURE_PR{2,3}.md (contrats), JEU_ESSAI_PR{2,3}.md (valeurs attendues)
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
| PR1 | Squelette : auth mono-compte, JET chaîné, migration initiale, Docker, proxy, backup | livré |
| PR2 | Vente en saisie libre, remise globale, espèces, tickets, Z journalier, annulation, TPE SumUp (push API) | livré |
| PR3 | Client, ticket par e-mail (Brevo), liste newsletter dédiée, consentement, RGPD | livré |
| PR3b | Charte graphique de l'affiche, logo, domaine app.lloomi.fr (OVH), impression des tickets (imprimante ticket réseau/USB + tiroir-caisse), purge des références à l'application source | livré |
| PR4 | Écritures comptables par Z, CSV Pennylane et FEC (format de l'application source), exports bruts, clôtures mensuelle/annuelle avec archive signée, export fiscal, PDF du Z, attestation, guide vendeur, procédure de clôture | livré |
| PR5 | Sauvegarde applicative planifiée de la base (dump `pg_dump\|gzip` nocturne, écran admin, alerte e-mail), libellé SumUp dérivé du nom de boutique | livré |
| PR6 | Tableau de bord d'accueil (jour, mois, 7 derniers jours) et objectifs de chiffre d'affaires réglables | livré |
| PR7 | Barre latérale unifiée, sorties de caisse explicites, client en caisse sans fidélité (téléphone ou e-mail, ticket au prénom), logo régénéré | livré |
| PR8 | Vendeuses identifiées par code PIN (relève en cours de journée, ticket et rapport Z au nom de la vendeuse), facture B2B numérotée (`F-AAAA-NNNN`, PDF déterministe scellé au premier téléchargement) et avoir automatique à l'annulation (`A-AAAA-NNNN`) | livré |
| PR9 | Encaissements carte échoués mis en file et relançables depuis la caisse, journal des échanges avec le terminal (lecture, filtres, purge) et analyse des causes d'échec, ventilation par vendeuse du Z nette des annulations | **en cours (cette PR)** |
