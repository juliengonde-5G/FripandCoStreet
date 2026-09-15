/** @type {import('next').NextConfig} */
import type { NextConfig } from "next";

// En production, Caddy sert le site et route /api/* vers l'API sur la même
// origine (https://app.lloomi.fr) : aucune rewrite n'est nécessaire.
//
// En développement, le front (ex. port 3120) et l'API (ex. port 8020)
// tournent sur des ports séparés. Sans same-origin, deux problèmes :
//   - CORS_ORIGINS est volontairement vide côté API (le déploiement prod
//     ne dépend d'aucune config CORS) → une requête cross-origin en dev
//     serait bloquée ;
//   - "localhost" résout parfois en IPv6 (::1) alors qu'uvicorn n'écoute
//     que sur 127.0.0.1 → connexion refusée, "Erreur de connexion au
//     serveur" côté navigateur alors que l'API tourne bien.
//
// Cette rewrite ne fait donc que reproduire la prod en dev : le navigateur
// appelle toujours /api/* en same-origin, et c'est le serveur Next (côté
// serveur, pas le navigateur) qui relaie vers l'API définie par la variable
// serveur API_PROXY_TARGET (ex. http://127.0.0.1:8000). Sans cette
// variable, aucune rewrite n'est ajoutée.
const apiProxyTarget = process.env.API_PROXY_TARGET;

const nextConfig: NextConfig = {
  async rewrites() {
    if (!apiProxyTarget) return [];
    return [
      {
        source: "/api/:path*",
        destination: `${apiProxyTarget}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
