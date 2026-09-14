/**
 * Client API — Frip & Co Street.
 *
 * Déploiement même origine en production (site + API sous
 * https://lloomi.fr, API sous /api/*) : NEXT_PUBLIC_API_URL reste
 * vide et les chemins d'API sont donc relatifs. En dev, on pointe vers
 * l'API locale (http://localhost:8000 par défaut).
 */

import { ApiError, extractErrorCode, extractErrorDetail } from "./apiError";
import { mockFetchAPI, isMockEnabled } from "./mockApi";

export { ApiError };

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "";

// Timeout par requête (ms). Assez long pour ne pas couper une requête
// lente, assez court pour ne pas laisser l'UI tourner indéfiniment.
const DEFAULT_TIMEOUT_MS = 30_000;

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
    // Retry-After est utilisé par la page de connexion pour le rate-limit.
    const retryAfter = res.headers.get("Retry-After");
    if (retryAfter) {
      error.retryAfter = retryAfter;
    }
    throw error;
  }

  return data as T;
}

export const api = {
  get: <T = unknown>(url: string) => fetchAPI<T>(url),
  post: <T = unknown>(url: string, data?: unknown) =>
    fetchAPI<T>(url, { method: "POST", body: data !== undefined ? JSON.stringify(data) : undefined }),
  put: <T = unknown>(url: string, data?: unknown) =>
    fetchAPI<T>(url, { method: "PUT", body: data !== undefined ? JSON.stringify(data) : undefined }),
  patch: <T = unknown>(url: string, data?: unknown) =>
    fetchAPI<T>(url, { method: "PATCH", body: data !== undefined ? JSON.stringify(data) : undefined }),
  delete: <T = unknown>(url: string) => fetchAPI<T>(url, { method: "DELETE" }),
};
