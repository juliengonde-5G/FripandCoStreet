"use client";

/**
 * Bannière « Installer l'application » (PR9, contrat K6).
 *
 * Rendue par `app/layout.tsx`, donc présente sur **toutes** les pages, y
 * compris l'écran de connexion : la tablette de la boutique est souvent
 * mise en service avant la première connexion.
 *
 * Règles d'affichage, dans l'ordre :
 *   - jamais si l'application tourne déjà en fenêtre autonome
 *     (`display-mode: standalone`) — elle est installée, il n'y a rien à
 *     proposer ;
 *   - jamais si Chrome n'a pas donné d'invite native à rejouer ;
 *   - jamais si « Ne plus proposer » a été touché (refus persistant) ou
 *     « Plus tard » pendant cette session d'onglet ;
 *   - sinon **à chaque chargement**, tant que l'application n'est pas
 *     installée : une tablette de comptoir change de mains, on ne compte
 *     pas sur le fait que quelqu'un ait déjà vu la proposition.
 *
 * `?reinstall=1` dans l'URL efface le refus persistant (voir `lib/pwaInstall`).
 *
 * La carte est fixée en bas, centrée. Elle ne doit rien recouvrir de ce
 * qui vit au ras du bas de fenêtre — le bouton **Encaisser** de la caisse
 * (`h-screen` sans défilement) comme le bouton **Se connecter**. Tant
 * qu'elle est visible, elle pose `data-pwa-banner="on"` sur `<body>` avec
 * sa **hauteur mesurée** dans `--fc-pwa-banner-h`, et les règles de
 * `globals.css` rendent cette hauteur aux écrans pleine fenêtre. Aucune
 * page n'a donc à connaître l'existence de la bannière.
 */
import Image from "next/image";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

import {
  consumeReinstallRequest,
  getPwaInstallServerSnapshot,
  getPwaInstallSnapshot,
  promptInstall,
  setInstallDismissedForever,
  setInstallSnoozedForSession,
  subscribeToPwaInstall,
} from "@/lib/pwaInstall";

export default function PwaInstallBanner() {
  // Les deux refus vivent dans le même magasin que l'invite différée : la
  // carte « Application » des Réglages voit donc immédiatement un « Ne plus
  // proposer » touché ici, et réciproquement. Le rendu serveur en ignore
  // tout (`getPwaInstallServerSnapshot`), ce qui évite l'écart
  // d'hydratation qu'une lecture directe de `localStorage` provoquerait.
  const { standalone, canInstall, dismissedForever, snoozedForSession } = useSyncExternalStore(
    subscribeToPwaInstall,
    getPwaInstallSnapshot,
    getPwaInstallServerSnapshot,
  );

  const [mounted, setMounted] = useState(false);
  const cardRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // `?reinstall=1` efface le refus persistant avant le premier affichage.
    consumeReinstallRequest();
    setMounted(true);
  }, []);

  const visible = mounted && !standalone && canInstall && !dismissedForever && !snoozedForSession;

  // Réservation de la place en bas de la zone de travail (voir en-tête).
  // La hauteur est **mesurée**, pas devinée : à 400 px de large les boutons
  // passent à la ligne et la carte grandit, or c'est justement là que le
  // bouton « Encaisser » a le moins de marge.
  useEffect(() => {
    if (typeof document === "undefined") return;
    const body = document.body;

    const release = () => {
      delete body.dataset.pwaBanner;
      body.style.removeProperty("--fc-pwa-banner-h");
    };

    if (!visible) {
      release();
      return release;
    }

    body.dataset.pwaBanner = "on";
    const card = cardRef.current;
    const measure = () => {
      const height = card ? card.getBoundingClientRect().height : 0;
      // `bottom-4` (1 rem) au-dessous de la carte, 1 rem de respiration
      // au-dessus : la carte flotte sans jamais toucher les boutons.
      body.style.setProperty("--fc-pwa-banner-h", `${Math.ceil(height) + 32}px`);
    };
    measure();

    if (!card || typeof ResizeObserver === "undefined") return release;
    const observer = new ResizeObserver(measure);
    observer.observe(card);
    return () => {
      observer.disconnect();
      release();
    };
  }, [visible]);

  const handleInstall = useCallback(async () => {
    const outcome = await promptInstall();
    // Acceptée : `canInstall` retombe à faux, la carte disparaît d'elle-même.
    // Refusée dans l'invite native : on ne réinsiste pas cette session.
    if (outcome !== "accepted") setInstallSnoozedForSession(true);
  }, []);

  const handleLater = useCallback(() => setInstallSnoozedForSession(true), []);

  const handleNever = useCallback(() => setInstallDismissedForever(true), []);

  if (!visible) return null;

  return (
    <div
      role="dialog"
      aria-label="Installer l'application"
      data-testid="pwa-install-banner"
      className="fixed inset-x-0 bottom-4 z-[70] flex justify-center px-4"
    >
      {/* Sur tablette (≥ 640 px) le texte et les boutons tiennent sur une
          seule ligne : la carte reste basse, donc la hauteur rendue à
          l'écran de caisse reste faible et rien n'y est rogné. En dessous,
          tout s'empile. */}
      <div
        ref={cardRef}
        className="w-full max-w-lg rounded-fc-lg bg-fc-primary px-4 py-3 text-white shadow-lg sm:max-w-3xl sm:px-5"
      >
        <div className="sm:flex sm:items-center sm:gap-5">
          <div className="flex items-start gap-3 sm:flex-1 sm:items-center">
            <Image
              src="/brand/logo-mark-on-blue.png"
              alt=""
              aria-hidden
              width={40}
              height={40}
              className="mt-0.5 h-10 w-10 flex-shrink-0 object-contain sm:mt-0"
            />
            <div className="min-w-0">
              <p className="text-base font-semibold leading-tight">Installer Frip &amp; Co Street</p>
              <p className="mt-0.5 text-sm leading-snug text-white/90">
                Ajoute l&apos;application à l&apos;écran d&apos;accueil de la tablette pour un
                lancement direct, en plein écran.
              </p>
            </div>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2 sm:mt-0 sm:flex-shrink-0 sm:flex-nowrap">
            <button
              type="button"
              onClick={() => void handleInstall()}
              className="min-h-touch flex-1 rounded-fc bg-white px-4 py-2 text-base font-semibold text-fc-primary-deep transition-colors hover:bg-fc-primary-soft sm:flex-none"
            >
              Installer
            </button>
            <button
              type="button"
              onClick={handleLater}
              className="min-h-touch flex-1 rounded-fc border border-white/60 px-4 py-2 text-base font-medium text-white transition-colors hover:bg-white/10 sm:flex-none"
            >
              Plus tard
            </button>
            <button
              type="button"
              onClick={handleNever}
              className="min-h-touch w-full whitespace-nowrap rounded-fc px-3 py-2 text-sm text-white/80 underline underline-offset-2 transition-colors hover:text-white sm:w-auto"
            >
              Ne plus proposer
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
