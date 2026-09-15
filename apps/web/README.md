# Frip & Co Street — Web

Caisse boutique éphémère (Rouen). Next.js 15 (App Router), React 18,
Tailwind CSS 3.4, TypeScript.

## Développement

En prod, le site et l'API sont servis sur la même origine (Caddy route
`/api/*` vers l'API derrière `https://app.lloomi.fr`). En dev, le front
et l'API tournent sur des ports séparés — pour reproduire ce same-origin
(et éviter CORS + le piège `localhost` → IPv6 alors qu'uvicorn écoute en
127.0.0.1), Next relaie lui-même `/api/*` vers l'API via `API_PROXY_TARGET`
(voir `next.config.ts`).

```bash
cp .env.local.example .env.local
npm install

# API sur un autre port (ex. 8000) :
API_PROXY_TARGET=http://127.0.0.1:8000 npm run dev   # http://localhost:3000
```

## Production

Déploiement même origine : le site est servi sur `https://app.lloomi.fr`
et l'API sous `https://app.lloomi.fr/api/*`, routée par Caddy — pas de
`API_PROXY_TARGET` ni de rewrite en prod. `NEXT_PUBLIC_API_URL` reste vide
(repli sur des chemins relatifs `/api/...`).

```bash
npm run build
npm run start
```

## Variables d'environnement

Voir `.env.local.example`.

| Variable | Description |
|---|---|
| `API_PROXY_TARGET` | Dev uniquement, variable serveur (jamais exposée au navigateur). URL de l'API que Next relaie derrière `/api/*` (ex. `http://127.0.0.1:8000`). Absente → pas de rewrite (comportement prod). |
| `NEXT_PUBLIC_API_URL` | Base URL alternative pour appeler l'API en cross-origin direct (nécessite `CORS_ORIGINS` côté API). Vide par défaut → chemins relatifs `/api/...`, comme en prod. |

## Qualité

```bash
npm run lint
npx tsc --noEmit
npm run build
```
