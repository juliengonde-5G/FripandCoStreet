/**
 * Client API — Frip & Co Street.
 *
 * Déploiement même origine en production (site + API sous
 * https://app.lloomi.fr, API sous /api/*) : NEXT_PUBLIC_API_URL reste
 * vide et les chemins d'API sont donc relatifs. En dev, on pointe vers
 * l'API locale (http://localhost:8000 par défaut).
 */

import { ApiError, extractErrorCode, extractErrorDetail, extractRequestId } from "./apiError";
import { mockFetchAPI, mockFetchBytes, mockFetchBytesWithHeaders, isMockEnabled } from "./mockApi";

export { ApiError };

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "";

// Timeout par requête (ms). Assez long pour ne pas couper une requête
// lente, assez court pour ne pas laisser l'UI tourner indéfiniment.
const DEFAULT_TIMEOUT_MS = 30_000;

/** Longueur de l'identifiant de requête (PR12 N5). Seize caractères : assez
 * pour ne jamais collisionner sur une journée de caisse, assez court pour
 * être recopié à la main ou dicté au téléphone. */
const REQUEST_ID_LENGTH = 16;

/**
 * Identifiant de requête envoyé en `X-Request-ID` sur chaque appel (N5).
 * Le serveur le reprend tel quel (sinon il en génère un), le renvoie sur
 * toutes ses réponses et l'écrit dans ses logs : une erreur vue au
 * comptoir se retrouve donc côté serveur sans chercher.
 *
 * `crypto.randomUUID` n'existe qu'en contexte sécurisé (HTTPS ou
 * localhost) ; sur une tablette servie en HTTP clair, ou dans un
 * navigateur ancien, on retombe sur un générateur simple. L'identifiant
 * n'a aucune valeur de sécurité — seulement d'unicité raisonnable.
 */
export function newRequestId(): string {
  const c = typeof globalThis !== "undefined" ? globalThis.crypto : undefined;
  if (c && typeof c.randomUUID === "function") {
    return c.randomUUID().replace(/-/g, "").slice(0, REQUEST_ID_LENGTH);
  }
  let out = "";
  while (out.length < REQUEST_ID_LENGTH) {
    out += Math.random().toString(16).slice(2);
  }
  return out.slice(0, REQUEST_ID_LENGTH);
}

/** Identifiant renvoyé par le serveur, ou à défaut celui qu'on a envoyé :
 * l'en-tête peut être masqué par un proxy ou une politique CORS, la
 * référence affichée reste alors valable car le serveur journalise
 * l'identifiant entrant. */
function responseRequestId(res: Response, sent: string, body?: unknown): string {
  return res.headers.get("X-Request-ID") ?? extractRequestId(body) ?? sent;
}

// Endpoints où un 401 est une réponse métier normale (ex. mauvais mot de
// passe) : l'appelant gère l'erreur lui-même, on NE DOIT PAS effacer le
// token ni rediriger vers /login.
const SOFT_401_ENDPOINTS = ["/api/auth/login"];

function isSoft401(endpoint: string): boolean {
  return SOFT_401_ENDPOINTS.some((p) => endpoint.startsWith(p));
}

function handleUnauthorized() {
  if (typeof window === "undefined") return;
  localStorage.removeItem("token");
  localStorage.removeItem("username");
  // Évite une boucle de redirection si /login renvoie elle-même un 401.
  if (!window.location.pathname.startsWith("/login")) {
    window.location.href = "/login";
  }
}

export interface FetchAPIOptions extends RequestInit {
  timeoutMs?: number;
}

/**
 * Appelle l'API, ajoute le Bearer token, applique un timeout, parse le JSON
 * et lève une ApiError en cas de statut non-2xx.
 */
export async function fetchAPI<T = unknown>(
  endpoint: string,
  options?: FetchAPIOptions,
): Promise<T> {
  // Mode démo PR2 — NEXT_PUBLIC_MOCK_API=1 : aucune requête réseau n'est
  // émise, tout passe par lib/mockApi.ts (routes §5 simulées en mémoire).
  // Complètement séparé du chemin réel ci-dessous, jamais actif sans la
  // variable d'environnement.
  if (isMockEnabled()) {
    return mockFetchAPI<T>(endpoint, options);
  }

  const token = typeof window !== "undefined" ? localStorage.getItem("token") : null;
  const headers: Record<string, string> = {};
  const isFormEncoded =
    typeof options?.body === "string" &&
    (options.headers as Record<string, string> | undefined)?.["Content-Type"] ===
      "application/x-www-form-urlencoded";
  if (!isFormEncoded && options?.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const requestId = newRequestId();
  headers["X-Request-ID"] = requestId;

  const controller = new AbortController();
  const timeoutMs = options?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let res: Response;
  try {
    res = await fetch(`${API_URL}${endpoint}`, {
      ...options,
      signal: options?.signal ?? controller.signal,
      headers: { ...headers, ...options?.headers },
    });
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      throw new Error(`La requête a expiré après ${Math.round(timeoutMs / 1000)}s`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 401 && !isSoft401(endpoint)) {
    handleUnauthorized();
  }

  if (res.status === 204) {
    return undefined as T;
  }

  const contentType = res.headers.get("content-type") ?? "";
  const data = contentType.includes("application/json")
    ? await res.json().catch(() => null)
    : await res.text().catch(() => null);

  if (!res.ok) {
    // Jamais String(objet) : voir extractErrorDetail (apiError.ts) — un
    // detail objet/tableau (validation Pydantic, backend qui imbrique
    // {message,code}…) ne doit jamais s'afficher « [object Object] ».
    const detail = extractErrorDetail(data);
    const code = extractErrorCode(data);
    const error = new ApiError(res.status, detail, code);
    error.requestId = responseRequestId(res, requestId, data);
    // Corps brut conservé pour les erreurs qui portent des champs de reprise
    // en plus de `detail`/`code` (PR9 : `recoverable`, `failed_payment_id`).
    error.body = data;
    // Retry-After est utilisé par la page de connexion pour le rate-limit.
    const retryAfter = res.headers.get("Retry-After");
    if (retryAfter) {
      error.retryAfter = retryAfter;
    }
    throw error;
  }

  return data as T;
}

/**
 * Variante binaire de `fetchAPI` — pour les octets ESC/POS bruts
 * (`GET /hardware/receipt/test-escpos`, `GET
 * /pos/transactions/{id}/escpos`, `GET /pos/drawer/kick-escpos`), servis
 * en mode WebUSB (tablette) : le corps n'est jamais du JSON, donc jamais
 * envoyé à `fetchAPI` (qui suppose text/JSON). Même gestion d'erreurs
 * (Bearer, timeout, 401, `ApiError`) que la variante JSON.
 */
export async function fetchBytes(endpoint: string, options?: FetchAPIOptions): Promise<Uint8Array> {
  if (isMockEnabled()) {
    return mockFetchBytes(endpoint, options);
  }

  const token = typeof window !== "undefined" ? localStorage.getItem("token") : null;
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const requestId = newRequestId();
  headers["X-Request-ID"] = requestId;

  const controller = new AbortController();
  const timeoutMs = options?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let res: Response;
  try {
    res = await fetch(`${API_URL}${endpoint}`, {
      ...options,
      signal: options?.signal ?? controller.signal,
      headers: { ...headers, ...options?.headers },
    });
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      throw new Error(`La requête a expiré après ${Math.round(timeoutMs / 1000)}s`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 401) {
    handleUnauthorized();
  }

  if (!res.ok) {
    const contentType = res.headers.get("content-type") ?? "";
    const data = contentType.includes("application/json")
      ? await res.json().catch(() => null)
      : await res.text().catch(() => null);
    const error = new ApiError(res.status, extractErrorDetail(data), extractErrorCode(data));
    error.requestId = responseRequestId(res, requestId, data);
    throw error;
  }

  return new Uint8Array(await res.arrayBuffer());
}

export interface BytesWithHeaders {
  bytes: Uint8Array;
  /** En-têtes de la réponse, clés en minuscules (comportement natif de
   * `Headers`). Utilisé par `lib/download.ts` (PR4) pour lire l'empreinte
   * (`X-Archive-SHA256`, `X-Export-SHA256`) d'un téléchargement — archive
   * fiscale, export fiscal à la demande — sans requête supplémentaire. */
  headers: Record<string, string>;
}

/**
 * Variante de `fetchBytes` qui renvoie aussi les en-têtes de la réponse
 * (PR4 : `X-Archive-SHA256` sur `/fiscal-closures/{id}/archive`,
 * `X-Export-SHA256` sur `/admin/fiscal-export`). Les téléchargements qui
 * n'ont pas besoin de l'empreinte (CSV, FEC, exports de table, PDF du Z)
 * passent aussi par ici : le corps du helper `downloadFile` reste unique.
 */
export async function fetchBytesWithHeaders(
  endpoint: string,
  options?: FetchAPIOptions,
): Promise<BytesWithHeaders> {
  if (isMockEnabled()) {
    return mockFetchBytesWithHeaders(endpoint, options);
  }

  const token = typeof window !== "undefined" ? localStorage.getItem("token") : null;
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const requestId = newRequestId();
  headers["X-Request-ID"] = requestId;

  const controller = new AbortController();
  const timeoutMs = options?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let res: Response;
  try {
    res = await fetch(`${API_URL}${endpoint}`, {
      ...options,
      signal: options?.signal ?? controller.signal,
      headers: { ...headers, ...options?.headers },
    });
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      throw new Error(`La requête a expiré après ${Math.round(timeoutMs / 1000)}s`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 401) {
    handleUnauthorized();
  }

  if (!res.ok) {
    const contentType = res.headers.get("content-type") ?? "";
    const data = contentType.includes("application/json")
      ? await res.json().catch(() => null)
      : await res.text().catch(() => null);
    const error = new ApiError(res.status, extractErrorDetail(data), extractErrorCode(data));
    error.requestId = responseRequestId(res, requestId, data);
    throw error;
  }

  const responseHeaders: Record<string, string> = {};
  res.headers.forEach((value, key) => {
    responseHeaders[key] = value;
  });

  return { bytes: new Uint8Array(await res.arrayBuffer()), headers: responseHeaders };
}

export const api = {
  get: <T = unknown>(url: string) => fetchAPI<T>(url),
  /** GET renvoyant des octets bruts (endpoints `*escpos*`, mode WebUSB). */
  getBytes: (url: string) => fetchBytes(url),
  /** GET renvoyant des octets bruts + en-têtes (PR4 : téléchargements avec
   * empreinte SHA-256). */
  getBytesWithHeaders: (url: string) => fetchBytesWithHeaders(url),
  post: <T = unknown>(url: string, data?: unknown) =>
    fetchAPI<T>(url, { method: "POST", body: data !== undefined ? JSON.stringify(data) : undefined }),
  put: <T = unknown>(url: string, data?: unknown) =>
    fetchAPI<T>(url, { method: "PUT", body: data !== undefined ? JSON.stringify(data) : undefined }),
  patch: <T = unknown>(url: string, data?: unknown) =>
    fetchAPI<T>(url, { method: "PATCH", body: data !== undefined ? JSON.stringify(data) : undefined }),
  delete: <T = unknown>(url: string) => fetchAPI<T>(url, { method: "DELETE" }),
};
