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
 * La carte est fixée en bas, centrée. Sur `/caisse`, l'écran est un
 * `h-screen` sans défilement dont le bouton **Encaisser** touche le bas de
 * la fenêtre : tant que la bannière est visible elle pose
 * `data-pwa-banner="on"` sur `<body>`, et une règle de `globals.css` rend
 * cette hauteur à la zone de travail. La carte ne recouvre donc jamais les
 * boutons de paiement.
 */
import Image from "next/image";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

import {
  consumeReinstallRequest,
  getPwaInstallServerSnapshot,
  getPwaInstallSnapshot,
  isInstallDismissedForever,
  isInstallSnoozedForSession,
  promptInstall,
  setInstallDismissedForever,
  setInstallSnoozedForSession,
  subscribeToPwaInstall,
} from "@/lib/pwaInstall";

export default function PwaInstallBanner() {
  const { standalone, canInstall } = useSyncExternalStore(
    subscribeToPwaInstall,
    getPwaInstallSnapshot,
    getPwaInstallServerSnapshot,
  );

  // `refused` couvre les deux refus (session et persistant). Il n'est lu
  // qu'après le montage : le rendu serveur ne connaît ni `sessionStorage`
  // ni `localStorage`, et un écart provoquerait une erreur d'hydratation.
  const [refused, setRefused] = useState(true);
  const [mounted, setMounted] = useState(false);
  const cardRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    consumeReinstallRequest();
    setRefused(isInstallDismissedForever() || isInstallSnoozedForSession());
    setMounted(true);
  }, []);

  const visible = mounted && !standalone && canInstall && !refused;

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
    if (outcome !== "accepted") {
      setInstallSnoozedForSession(true);
      setRefused(true);
    }
  }, []);

  const handleLater = useCallback(() => {
    setInstallSnoozedForSession(true);
    setRefused(true);
  }, []);

  const handleNever = useCallback(() => {
    setInstallDismissedForever(true);
    setRefused(true);
  }, []);

  if (!visible) return null;

  return (
    <div
      role="dialog"
      aria-label="Installer l'application"
      data-testid="pwa-install-banner"
      className="fixed inset-x-0 bottom-4 z-[70] flex justify-center px-4"
    >
      <div
        ref={cardRef}
        className="w-full max-w-lg rounded-fc-lg bg-fc-primary px-4 py-4 text-white shadow-lg sm:px-5"
      >
        <div className="flex items-start gap-3">
          <Image
            src="/brand/logo-mark-on-blue.png"
            alt=""
            aria-hidden
            width={40}
            height={40}
            className="mt-0.5 h-10 w-10 flex-shrink-0 object-contain"
          />
          <div className="min-w-0">
            <p className="text-base font-semibold leading-tight">Installer Frip &amp; Co Street</p>
            <p className="mt-1 text-sm leading-snug text-white/90">
              Ajoute l&apos;application à l&apos;écran d&apos;accueil de la tablette pour un
              lancement direct, en plein écran.
            </p>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
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
            className="min-h-touch rounded-fc px-3 py-2 text-sm text-white/80 underline underline-offset-2 transition-colors hover:text-white"
          >
            Ne plus proposer
          </button>
        </div>
      </div>
    </div>
  );
}
