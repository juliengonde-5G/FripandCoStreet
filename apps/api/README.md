# Frip & Co Street — API

API de caisse pour la boutique éphémère **Frip & Co Street** (Rouen).
Squelette backend isolé, extrait et simplifié du monorepo Vintiz — voir les
en-têtes `# Extrait de Vintiz (...)` dans le code pour la provenance de
chaque fichier. Mono-boutique, mono-utilisateur, auto-attestation NF525.

## Démarrage rapide

```bash
cd apps/api
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp ../../.env.example .env   # remplir SECRET_KEY, FISCAL_SIGNING_KEY, DATABASE_URL

# Schéma piloté par Alembic — jamais de create_all, même en dev
python -m alembic upgrade head

# Premier (et unique) compte
python scripts/create_manager.py --username <nom> --email <email>

uvicorn app.main:app --reload --port 8000
```

## Variables d'environnement

| Variable | Rôle |
|---|---|
| `ENVIRONMENT` | `development` \| `test` \| `production` |
| `DATABASE_URL` | Connexion applicative — rôle **non propriétaire** (`fripco_app`, sans droit DDL) |
| `MIGRATION_DATABASE_URL` | Connexion Alembic — rôle **propriétaire** du schéma (défaut : réutilise `DATABASE_URL`) |
| `SECRET_KEY` | JWT — obligatoire en production (refus de boot sinon) |
| `FISCAL_SIGNING_KEY` | HMAC du journal des événements techniques (JET) — obligatoire ≥32 caractères en production |
| `CORS_ORIGINS` | Vide par défaut (déploiement same-origin derrière Caddy) |
| `LOGIN_RATE_LIMIT_ATTEMPTS` / `LOGIN_RATE_LIMIT_WINDOW_SECONDS` | Rate-limit `/api/auth/login` (défaut 10 / 300s) |
| `LOG_LEVEL` / `LOG_JSON` | Logging |
| `SHOP_NAME` | `Frip & Co Street` |
| `FRIPCO_BUILD_SHA` / `FRIPCO_BUILD_DATE` | Injectées au build, exposées par `/api/health` |

## Deux rôles PostgreSQL

L'API tourne avec un rôle applicatif **sans droit DDL** (`fripco_app`) : une
faille applicative ne peut ni altérer le schéma ni contourner le trigger
d'immuabilité du JET. Les migrations s'exécutent avec le rôle propriétaire
via `MIGRATION_DATABASE_URL` :

```bash
python -m alembic upgrade head   # utilise MIGRATION_DATABASE_URL si définie
```

## Journal des événements techniques (JET)

`journal_events` est un journal chaîné (HMAC-SHA256, genèse `"0"`) — chaque
ligne référence le hash de la précédente. Un trigger PostgreSQL
(`trg_protect_journal_event`, migration `0001`) rejette tout `UPDATE` ou
`DELETE` sur la table : l'immuabilité est garantie par la base, pas
seulement par convention applicative. Endpoints (authentifiés) :

- `GET /api/admin/jet?limit=&before_seq=` — liste du plus récent au plus ancien
- `GET /api/admin/jet/integrity` — recalcule et vérifie toute la chaîne

## Tests — PostgreSQL uniquement

Les tests exigent PostgreSQL (le trigger d'immuabilité doit être exercé pour
de vrai — `test_jet.py` vérifie qu'un `UPDATE`/`DELETE` brut est bien rejeté,
ce qu'aucun mock ne peut prouver). `tests/conftest.py` refuse de démarrer si
`TEST_DATABASE_URL` ne pointe pas vers PostgreSQL.

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://fripco:fripco@localhost:5432/fripco_test
python -m alembic upgrade head   # optionnel — conftest.py le fait aussi
python -m pytest -q
python -m ruff check .
```

## Périmètre de ce squelette (PR1)

Inclus : authentification (login/refresh/me/logout), rate-limit, JET,
middlewares (request-id, en-têtes de sécurité), schéma Alembic + trigger
d'immuabilité. **Exclus** (prochaines PR) : vente, caisse espèces, paiement
CB SumUp, fidélité/newsletter Brevo, export comptable — voir
`CDC_Caisse_FripCo_Street.md` à la racine du monorepo Vintiz pour le périmètre
complet et `fripco-street/CLAUDE.md` pour les règles non négociables du repo.
