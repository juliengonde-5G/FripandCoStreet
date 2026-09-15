/**
 * Vendeuses en caisse (PR8, docs/ARCHITECTURE_PR8.md §1, J2/J4).
 *
 * Un seul endroit pour les trois gestes du comptoir — lister les
 * vendeuses actives, s'identifier avec un code à 4 chiffres, faire la
 * relève — et pour la lecture du blocage temporaire renvoyé en 429 après
 * cinq codes faux.
 *
 * Aucun état ici : la vendeuse courante vit sur le tiroir
 * (`GET /api/pos/drawer/current` → `current_cashier`), jamais dans le
 * navigateur. Rafraîchir la page ne doit pas « perdre » la vendeuse, et
 * deux onglets ouverts sur la même caisse doivent voir la même personne.
 */
import { api, ApiError } from "./api";
import type {
  CashierIdentifyResponse,
  CashierRef,
  PosCashier,
  PosCashierListResponse,
  PosSettings,
} from "./types";

/** Longueur du code — fixée par le contrat (J3), partagée par le pavé de
 * la caisse et le formulaire d'administration. */
export const PIN_LENGTH = 4;

/** Un code n'est envoyé au serveur que complet : pas de tentative
 * consommée (ni de blocage rapproché) sur une saisie à moitié tapée. */
export function isPinComplete(pin: string): boolean {
  return new RegExp(`^\\d{${PIN_LENGTH}}$`).test(pin);
}

/** Ne garde que les chiffres — un pavé tactile, un clavier physique et un
 * copier-coller doivent produire la même valeur. */
export function digitsOnly(value: string): string {
  return value.replace(/\D/g, "");
}

/** Vendeuses actives, proposées à l'identification. */
export async function fetchPosCashiers(): Promise<PosCashier[]> {
  const data = await api.get<PosCashierListResponse>("/api/pos/cashiers");
  return data.cashiers ?? [];
}

/** Identification. Lève une `ApiError` : 401 `invalid_pin` (code faux),
 * 429 (trop d'essais — voir `retryAfterSeconds`). */
export async function identifyCashier(cashierId: string, pin: string): Promise<CashierRef> {
  const data = await api.post<CashierIdentifyResponse>("/api/pos/cashiers/identify", {
    cashier_id: cashierId,
    pin,
  });
  return data.cashier;
}

/** Relève : la caisse n'a plus de vendeuse identifiée. */
export async function releaseCashier(): Promise<void> {
  await api.post("/api/pos/cashiers/release");
}

/** Réglage `pos.cashier_required`. Un backend qui ne connaît pas encore
 * la route (501/404 en démo, déploiement partiel) ne doit pas bloquer la
 * caisse : on retombe sur « identification facultative ». */
export async function fetchPosSettings(): Promise<PosSettings> {
  try {
    return await api.get<PosSettings>("/api/admin/settings/pos");
  } catch {
    return { cashier_required: false };
  }
}

/**
 * Durée du blocage, en secondes, à partir d'une réponse 429.
 *
 * Deux sources, dans l'ordre : l'en-tête `Retry-After` (posé sur
 * `ApiError.retryAfter` par `lib/api.ts`), puis le texte français du
 * `detail` — le contrat dit seulement que `detail` « indique l'attente »,
 * sans en fixer la forme. On y lit donc aussi bien « réessayez dans
 * 5 minutes » que « dans 240 s ». Sans rien d'exploitable, `null` : le
 * message s'affiche alors sans compte à rebours, jamais avec un faux.
 */
export function retryAfterSeconds(err: unknown): number | null {
  if (!(err instanceof ApiError)) return null;

  const header = err.retryAfter ? Number.parseInt(err.retryAfter, 10) : NaN;
  if (Number.isFinite(header) && header > 0) return header;

  const detail = err.detail ?? "";
  const minutes = detail.match(/(\d+)\s*(?:min|minute)/i);
  if (minutes) {
    const extraSeconds = detail.match(/min(?:ute)?s?\s*(?:et\s*)?(\d+)\s*s/i);
    return Number.parseInt(minutes[1], 10) * 60 + (extraSeconds ? Number.parseInt(extraSeconds[1], 10) : 0);
  }
  const seconds = detail.match(/(\d+)\s*(?:s\b|sec|seconde)/i);
  if (seconds) return Number.parseInt(seconds[1], 10);
  return null;
}

/** Compte à rebours lisible au comptoir : « 45 s », « 4 min 05 s ». */
export function formatCountdown(totalSeconds: number): string {
  const safe = Math.max(0, Math.round(totalSeconds));
  if (safe < 60) return `${safe} s`;
  const minutes = Math.floor(safe / 60);
  const seconds = safe % 60;
  return `${minutes} min ${String(seconds).padStart(2, "0")} s`;
}
