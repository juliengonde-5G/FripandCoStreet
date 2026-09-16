/**
 * Application installable — capture et rejeu de l'invite native (PR9, K6).
 *
 * Chrome (la cible : tablette Android au comptoir) n'expose l'installation
 * que via l'événement `beforeinstallprompt`, et seulement une fois : si on
 * le laisse passer, l'invite n'est plus jamais disponible pour la page.
 * Ce module l'intercepte donc au plus tôt (`preventDefault`), garde
 * l'événement différé dans un singleton de module, et le partage entre les
 * deux endroits qui doivent pouvoir le rejouer : la bannière du bas d'écran
 * et la carte « Application » des Réglages.
 *
 * Aucune bibliothèque tierce : ce sont trois écouteurs et un booléen.
 *
 * Le module s'auto-installe à l'évaluation (côté navigateur uniquement) et
 * non dans un `useEffect`, pour être en place avant l'hydratation de React —
 * l'événement arrive parfois très tôt après le chargement.
 */

/** Clé de refus définitif (« Ne plus proposer ») — contrat K6. */
export const DISMISS_STORAGE_KEY = "fripco_pwa_install_dismissed";

/** Clé de report pour la session d'onglet (« Plus tard ») — contrat K6. */
export const SNOOZE_SESSION_KEY = "fripco_pwa_install_snoozed";

/** Paramètre d'URL qui efface le refus définitif. */
export const REINSTALL_QUERY_PARAM = "reinstall";

/**
 * Forme de l'événement Chrome, absent de la bibliothèque DOM standard.
 * On ne déclare que ce qu'on utilise.
 */
export interface BeforeInstallPromptEvent extends Event {
  readonly platforms: string[];
  prompt(): Promise<void>;
  readonly userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
}

export type InstallOutcome = "accepted" | "dismissed" | "unavailable";

export interface PwaInstallState {
  /** L'application tourne déjà dans sa propre fenêtre (installée). */
  standalone: boolean;
  /** Une invite native a été capturée et peut être rejouée. */
  canInstall: boolean;
  /** Le navigateur a signalé l'installation pendant cette visite. */
  justInstalled: boolean;
  /** « Ne plus proposer » a été touché sur cet appareil (persistant). */
  dismissedForever: boolean;
  /** « Plus tard » a été touché dans cet onglet. */
  snoozedForSession: boolean;
}

// --- singleton de module -----------------------------------------------

let deferred: BeforeInstallPromptEvent | null = null;
let justInstalled = false;
let started = false;
const listeners = new Set<() => void>();

/**
 * Instantané mémoïsé : `useSyncExternalStore` exige que deux lectures
 * consécutives sans notification renvoient la **même** référence.
 */
let snapshot: PwaInstallState = {
  standalone: false,
  canInstall: false,
  justInstalled: false,
  dismissedForever: false,
  snoozedForSession: false,
};

const SERVER_SNAPSHOT: PwaInstallState = {
  standalone: false,
  canInstall: false,
  justInstalled: false,
  dismissedForever: false,
  snoozedForSession: false,
};

/** L'application tourne-t-elle déjà en fenêtre autonome ? */
export function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  try {
    if (window.matchMedia?.("(display-mode: standalone)").matches) return true;
  } catch {
    // `matchMedia` peut manquer dans des environnements de test réduits.
  }
  // Safari iOS n'implémente pas `display-mode` et pose ce drapeau non standard.
  return (window.navigator as Navigator & { standalone?: boolean }).standalone === true;
}

function recompute(): void {
  const next: PwaInstallState = {
    standalone: isStandalone(),
    canInstall: deferred !== null,
    justInstalled,
    dismissedForever: readFlag("local", DISMISS_STORAGE_KEY),
    snoozedForSession: readFlag("session", SNOOZE_SESSION_KEY),
  };
  if (
    next.standalone === snapshot.standalone &&
    next.canInstall === snapshot.canInstall &&
    next.justInstalled === snapshot.justInstalled &&
    next.dismissedForever === snapshot.dismissedForever &&
    next.snoozedForSession === snapshot.snoozedForSession
  ) {
    return;
  }
  snapshot = next;
  listeners.forEach((l) => l());
}

function start(): void {
  if (started || typeof window === "undefined") return;
  started = true;

  window.addEventListener("beforeinstallprompt", (event) => {
    // Sans `preventDefault`, Chrome affiche sa propre mini-barre et
    // l'événement n'est plus rejouable : c'est lui qui rend la bannière
    // et le bouton des Réglages possibles.
    event.preventDefault();
    deferred = event as BeforeInstallPromptEvent;
    justInstalled = false;
    recompute();
  });

  window.addEventListener("appinstalled", () => {
    deferred = null;
    justInstalled = true;
    recompute();
  });

  // Le passage en fenêtre autonome peut survenir sans rechargement.
  try {
    window
      .matchMedia?.("(display-mode: standalone)")
      ?.addEventListener?.("change", () => recompute());
  } catch {
    // Navigateur sans `addEventListener` sur MediaQueryList : sans effet.
  }

  recompute();
}

start();

// --- API consommée par les composants ----------------------------------

export function subscribeToPwaInstall(listener: () => void): () => void {
  start();
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getPwaInstallSnapshot(): PwaInstallState {
  return snapshot;
}

export function getPwaInstallServerSnapshot(): PwaInstallState {
  return SERVER_SNAPSHOT;
}

/**
 * Rejoue l'invite native. Un événement `beforeinstallprompt` n'est
 * utilisable qu'une fois : on le relâche quel que soit le choix, Chrome en
 * émettra un nouveau s'il redevient pertinent.
 */
export async function promptInstall(): Promise<InstallOutcome> {
  const event = deferred;
  if (!event) return "unavailable";
  deferred = null;
  recompute();
  try {
    await event.prompt();
    const choice = await event.userChoice;
    return choice.outcome === "accepted" ? "accepted" : "dismissed";
  } catch {
    return "dismissed";
  }
}

// --- mémorisation des refus --------------------------------------------

function readFlag(storage: "local" | "session", key: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    const store = storage === "local" ? window.localStorage : window.sessionStorage;
    return store.getItem(key) === "1";
  } catch {
    // Mode navigation privée / stockage bloqué : on propose, tant pis.
    return false;
  }
}

function writeFlag(storage: "local" | "session", key: string, value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    const store = storage === "local" ? window.localStorage : window.sessionStorage;
    if (value) store.setItem(key, "1");
    else store.removeItem(key);
  } catch {
    // Stockage indisponible : le refus ne sera pas mémorisé, sans gravité.
  }
}

export function isInstallDismissedForever(): boolean {
  return readFlag("local", DISMISS_STORAGE_KEY);
}

export function setInstallDismissedForever(value: boolean): void {
  writeFlag("local", DISMISS_STORAGE_KEY, value);
  recompute();
}

export function isInstallSnoozedForSession(): boolean {
  return readFlag("session", SNOOZE_SESSION_KEY);
}

export function setInstallSnoozedForSession(value: boolean): void {
  writeFlag("session", SNOOZE_SESSION_KEY, value);
  recompute();
}

/**
 * `?reinstall=1` dans l'URL efface le refus définitif : c'est la porte de
 * sortie quand la vendeuse a touché « Ne plus proposer » et que la tablette
 * doit finalement recevoir l'application. Renvoie `true` si un refus a été
 * effacé.
 */
export function consumeReinstallRequest(): boolean {
  if (typeof window === "undefined") return false;
  let requested = false;
  try {
    requested = new URLSearchParams(window.location.search).get(REINSTALL_QUERY_PARAM) === "1";
  } catch {
    return false;
  }
  if (!requested) return false;
  setInstallDismissedForever(false);
  setInstallSnoozedForSession(false);
  return true;
}
