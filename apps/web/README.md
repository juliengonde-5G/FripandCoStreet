# Frip & Co Street — Web

Caisse boutique éphémère (Rouen). Next.js 15 (App Router), React 18,
Tailwind CSS 3.4, TypeScript.

## Développement

```bash
cp .env.local.example .env.local
npm install
npm run dev   # http://localhost:3000
```

## Production

Déploiement même origine : le site est servi sur `https://street.fripco.fr`
et l'API sous `https://street.fripco.fr/api/*`. `NEXT_PUBLIC_API_URL` doit
donc rester vide en prod (repli sur des chemins relatifs `/api/...`).

```bash
npm run build
npm run start
```

## Variables d'environnement

Voir `.env.local.example`.

| Variable | Description |
|---|---|
| `NEXT_PUBLIC_API_URL` | Base URL de l'API. Vide en prod (même origine) ; `http://localhost:8000` en dev. |

## Qualité

```bash
npm run lint
npx tsc --noEmit
npm run build
```
