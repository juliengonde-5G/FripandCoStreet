/*
 * Frip & Co Street — service worker minimal (PR9, contrat K6).
 *
 * Son SEUL rôle est de rendre l'application installable dans Chrome : un
 * navigateur n'affiche `beforeinstallprompt` que si la page déclare un
 * manifeste valide ET enregistre un service worker contrôlant `start_url`.
 *
 * Il ne met RIEN en cache. C'est délibéré et non négociable : une caisse
 * fiscale ne doit jamais servir une réponse périmée. Un total, un état de
 * tiroir, un rapport Z ou une tentative de paiement lus dans un cache
 * seraient faux au moment où la vendeuse les regarde, et un écran de caisse
 * servi depuis un cache pourrait tourner sur une version de l'application
 * plus ancienne que l'API. Le `fetch` n'est donc même pas intercepté : le
 * réseau fait foi, `/api/*` comme le reste.
 *
 * `skipWaiting` + `clients.claim` : une nouvelle version publiée prend la
 * main immédiatement, sans attendre la fermeture de tous les onglets — on
 * ne veut pas qu'une tablette reste des jours sur un ancien service worker.
 */

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // Filet de sécurité : si une version antérieure avait laissé des
      // caches, on les efface (rien dans ce fichier n'en crée).
      const names = await caches.keys();
      await Promise.all(names.map((name) => caches.delete(name)));
      await self.clients.claim();
    })(),
  );
});

// Volontairement aucun gestionnaire `fetch` : toute requête part sur le
// réseau, sans interception ni cache.
