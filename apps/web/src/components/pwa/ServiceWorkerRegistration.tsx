"use client";

/**
 * Enregistrement du service worker (PR9, contrat K6).
 *
 * `public/sw.js` ne met rien en cache : il n'existe que parce que Chrome
 * refuse de proposer l'installation d'une application qui n'en a pas.
 * L'enregistrement est **réservé à la production** — en développement, un
 * service worker persistant entre deux `next dev` brouille le rechargement
 * à chaud sans rien apporter.
 *
 * Portée `/` : le fichier est servi depuis la racine du site, donc il
 * contrôle `start_url` (`/`) comme le manifeste l'exige.
 */
import { useEffect } from "react";

export default function ServiceWorkerRegistration() {
  useEffect(() => {
    if (process.env.NODE_ENV !== "production") return;
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;

    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {
      // Contexte non sécurisé (http:// hors localhost) ou service worker
      // désactivé par la politique du navigateur : l'application reste
      // parfaitement utilisable, simplement non installable.
    });
  }, []);

  return null;
}
