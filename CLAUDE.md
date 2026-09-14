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
- **Chaîne fiscale** : ventes/annulations signées HMAC-SHA256 v3 (`services/fiscal.py`,
  payload documenté dans `docs/ARCHITECTURE_PR2.md` §3), Z journalier scellé à la
  création (montants de caisse, mouvements, cumuls perpétuels inclus), le tout sous
  un **seul** verrou `pg_advisory_xact_lock(5252026)` partagé par ventes, annulations
  et clôture. Toute modification du payload signé = évolution fiscale majeure
  (bump `FISCAL_SIGNATURE_VERSION` + `FISCAL_VERSION_DATE`, note dans l'attestation).
- Une vente est **refusée caisse fermée** (409 `drawer_closed`). Une annulation est une
  transaction `refund` référençant l'originale, jamais une modification.
- CB : le serveur relit SumUp avant d'écrire la vente ; rien de ce que le navigateur
  envoie sur la carte n'est cru. Sans TPE configuré/en ligne : espèces uniquement.
- Aucune mention « conforme NF525 » nulle part (auto-attestation art. 286 I-3° bis CGI).
- Erreurs métier : lever `PosServiceError(message, code=…, status_code=…)` →
  `{"detail": "…", "code": "snake_case"}` ; le front affiche `detail` tel quel.
- Secrets (SumUp, Brevo) : variables d'environnement uniquement, jamais en base ni en
  réponse d'API (`describe()` masque tout).

## Stack

FastAPI + SQLAlchemy async + asyncpg + Alembic · Next.js 15 (App Router) +
Tailwind · PostgreSQL 16 · Docker Compose · Caddy (reverse-proxy partagé du VPS).

## Tests de bout en bout

Un faux serveur SumUp (HTTP local) permet d'exercer le vrai chemin réseau du
paiement CB sans TPE : voir le rapport de test PR2. Jeu d'essai chiffré de
référence : `docs/JEU_ESSAI_PR2.md`.

## Commandes

```bash
cd apps/api && python -m ruff check . && TEST_DATABASE_URL=postgresql+asyncpg://fripco:fripco@localhost:5432/fripco_test python -m pytest -q
cd apps/web && npm run lint && npx tsc --noEmit && npm run build
./scripts/deploy.sh   # sur le VPS
```

Voir `docs/DEPLOIEMENT.md`.
