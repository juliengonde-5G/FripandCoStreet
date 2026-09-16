/**
 * Fiches clientes — doublons, fusion, historique d'achats et suppression
 * différée (PR10, docs/ARCHITECTURE_PR10.md §1, L2 à L5).
 *
 * Un seul endroit pour les appels partagés par l'administration et la
 * caisse : l'onglet Clients y prend les groupes de doublons, la fusion et
 * les deux gestes de la suppression programmée ; la caisse y prend les
 * candidats à la création d'une fiche et l'historique d'achats.
 *
 * Aucun état ici, aucun stockage navigateur : tout vit en base, le front
 * relit à chaque fois. Les coordonnées renvoyées à la caisse sont
 * masquées côté serveur (`email_masked`, `phone_masked`) — on affiche ce
 * qu'on reçoit, on ne démasque jamais.
 */
import { api, fetchAPI } from "./api";
import type { Client } from "./types";

// ---------------------------------------------------------------------------
// Doublons (L2)
// ---------------------------------------------------------------------------

/** Pourquoi deux fiches se ressemblent. Ordre du contrat : e-mail, puis
 * téléphone, puis nom. */
export type DuplicateReason = "email" | "phone" | "name";

/** Libellé lisible au comptoir et en administration. */
export const DUPLICATE_REASON_LABELS: Record<DuplicateReason, string> = {
  email: "même e-mail",
  phone: "même téléphone",
  name: "même nom",
};

export function duplicateReasonLabel(reason: DuplicateReason | string): string {
  return DUPLICATE_REASON_LABELS[reason as DuplicateReason] ?? "fiche proche";
}

/**
 * Une fiche d'un groupe de doublons côté administration
 * (`GET /api/admin/clients/duplicates`).
 *
 * Coordonnées MASQUÉES, comme en caisse : reconnaître la bonne fiche
 * n'exige pas de lire l'adresse entière, et cet écran peut être ouvert
 * devant du public. `created_at` sert à présélectionner la fiche à
 * conserver (la plus visitée, puis la plus ancienne).
 */
export interface DuplicateGroupClient {
  id: string;
  first_name: string | null;
  last_name: string | null;
  email_masked: string | null;
  phone_masked: string | null;
  visits_count: number;
  last_visit_at: string | null;
  created_at: string | null;
}

/** Deux fiches ou plus qui se ressemblent pour un même motif. */
export interface DuplicateGroup {
  reason: DuplicateReason;
  clients: DuplicateGroupClient[];
}

export interface DuplicateGroupsResponse {
  groups: DuplicateGroup[];
  total: number;
}

/**
 * Une fiche proposée en caisse au moment de créer un nouveau client
 * (`GET /api/pos/clients/duplicates`) : coordonnées MASQUÉES, comme
 * partout en caisse (PR7, I3).
 */
export interface DuplicateCandidate {
  id: string;
  first_name: string | null;
  last_name: string | null;
  /** `j***@exemple.fr`, ou `null` si la fiche n'a pas d'e-mail. */
  email_masked: string | null;
  /** `•••••••66`, ou `null` si la fiche n'a pas de téléphone. */
  phone_masked: string | null;
  visits_count: number;
  last_visit_at: string | null;
  reason: DuplicateReason;
}

export interface DuplicateCandidatesResponse {
  candidates: DuplicateCandidate[];
}

/** Critères de recherche de doublons en caisse. Au moins un doit être
 * non vide, sinon le serveur répond 422 `criteria_required`. */
export interface DuplicateCriteria {
  first_name?: string;
  last_name?: string;
  email?: string;
  phone?: string;
}

/** Vrai dès qu'un critère est renseigné — évite d'appeler le serveur
 * pour rien (et de récolter un 422 `criteria_required`). */
export function hasDuplicateCriteria(criteria: DuplicateCriteria): boolean {
  return Object.values(criteria).some((value) => (value ?? "").trim() !== "");
}

/** Groupes de doublons pour l'administration (50 groupes maximum). */
export async function fetchDuplicateGroups(): Promise<DuplicateGroupsResponse> {
  const data = await api.get<DuplicateGroupsResponse>("/api/admin/clients/duplicates");
  return { groups: data?.groups ?? [], total: data?.total ?? 0 };
}

/**
 * Fiches qui ressemblent à celle qu'on est en train de saisir en caisse.
 * Sans aucun critère, on ne fait pas d'appel : la liste est vide.
 */
export async function fetchPosDuplicates(
  criteria: DuplicateCriteria,
  options?: { signal?: AbortSignal },
): Promise<DuplicateCandidate[]> {
  if (!hasDuplicateCriteria(criteria)) return [];
  const params = new URLSearchParams();
  for (const key of ["first_name", "last_name", "email", "phone"] as const) {
    const value = (criteria[key] ?? "").trim();
    if (value) params.set(key, value);
  }
  const data = await fetchAPI<DuplicateCandidatesResponse>(
    `/api/pos/clients/duplicates?${params.toString()}`,
    { signal: options?.signal },
  );
  return data?.candidates ?? [];
}

// ---------------------------------------------------------------------------
// Fusion (L3)
// ---------------------------------------------------------------------------

/** Ce que la fusion a rattaché à la fiche conservée. */
export interface MergeMovedCounts {
  transactions: number;
  consents: number;
  communications: number;
}

/** Réponse de `POST /api/admin/clients/{winner_id}/merge`. */
export interface MergeClientsResponse {
  /** La fiche conservée, à jour (champs vides complétés depuis l'autre). */
  client: Client;
  moved: MergeMovedCounts;
}

/**
 * Fusionne deux fiches : `sourceId` est vidée et pointe désormais vers
 * `winnerId`, qui récupère ventes, consentements et messages.
 *
 * Lève une `ApiError` 409 : `same_client`, `client_inactive`,
 * `deletion_pending` (la fiche conservée a une suppression programmée —
 * l'annuler d'abord).
 */
export async function mergeClients(
  winnerId: string,
  sourceId: string,
): Promise<MergeClientsResponse> {
  return api.post<MergeClientsResponse>(`/api/admin/clients/${winnerId}/merge`, {
    source_id: sourceId,
  });
}

// ---------------------------------------------------------------------------
// Historique d'achats (L4)
// ---------------------------------------------------------------------------

/** Une ligne d'un ticket, telle qu'affichée dans l'historique. Le serveur
 * en renvoie 5 au maximum, puis une ligne de libellé `"…"`. */
export interface ClientHistoryItem {
  label: string;
  quantity: number;
  /** Montant en chaîne à deux décimales (« 12.50 »). */
  unit_price: string;
}

/** Un ticket de l'historique (ventes seulement, plus récent d'abord). */
export interface ClientHistoryTransaction {
  id: string;
  transaction_number: number;
  created_at: string;
  /** Montant en chaîne à deux décimales (« 42.00 »). */
  total_ttc: string;
  items_count: number;
  items: ClientHistoryItem[];
  /** Vrai si une annulation référence cette vente. */
  refunded: boolean;
}

/** Réponse de `GET /api/pos/clients/{id}/history`. */
export interface ClientHistory {
  client_id: string;
  visits_count: number;
  last_visit_at: string | null;
  /** Somme des ventes non annulées, en chaîne à deux décimales. */
  total_spent: string;
  transactions: ClientHistoryTransaction[];
}

/** Nombre de tickets affichés par défaut (le serveur borne à 20). */
export const CLIENT_HISTORY_DEFAULT_LIMIT = 5;
export const CLIENT_HISTORY_MAX_LIMIT = 20;

/**
 * Historique d'achats d'une fiche. Lève une `ApiError` 404 `not_found`
 * pour une fiche inconnue, absorbée ou anonymisée.
 */
export async function fetchClientHistory(
  clientId: string,
  limit: number = CLIENT_HISTORY_DEFAULT_LIMIT,
  options?: { signal?: AbortSignal },
): Promise<ClientHistory> {
  const bounded = Math.min(Math.max(1, Math.round(limit)), CLIENT_HISTORY_MAX_LIMIT);
  return fetchAPI<ClientHistory>(`/api/pos/clients/${clientId}/history?limit=${bounded}`, {
    signal: options?.signal,
  });
}

// ---------------------------------------------------------------------------
// Suppression RGPD différée (L5)
// ---------------------------------------------------------------------------

/** Réponse des deux routes de suppression programmée. */
export interface ClientDeletionResponse {
  client: Client;
}

/**
 * Programme la suppression de la fiche (30 jours par défaut, réglage
 * `rgpd.deletion_delay_days`). Lève une `ApiError` 409
 * `already_requested` ou `client_inactive`.
 */
export async function requestClientDeletion(clientId: string): Promise<Client> {
  const data = await api.post<ClientDeletionResponse>(
    `/api/admin/clients/${clientId}/deletion-request`,
  );
  return data.client;
}

/** Annule une suppression programmée. `ApiError` 409 `not_requested`. */
export async function cancelClientDeletion(clientId: string): Promise<Client> {
  const data = await api.post<ClientDeletionResponse>(
    `/api/admin/clients/${clientId}/deletion-cancel`,
  );
  return data.client;
}

// ---------------------------------------------------------------------------
// Petits utilitaires d'affichage partagés (administration et caisse)
// ---------------------------------------------------------------------------

/**
 * Montant renvoyé par l'historique : chaîne à deux décimales côté
 * serveur, nombre côté fiche admin. Tolère les deux et ne lève jamais.
 */
export function parseAmount(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Vrai si la fiche a été absorbée par une autre (elle n'est plus
 * utilisable : le front redirige vers la fiche conservée). */
export function isMergedClient(client: Pick<Client, "merged_into_client_id">): boolean {
  return Boolean(client.merged_into_client_id);
}

/** Vrai si la fiche est en attente de suppression (elle reste utilisable
 * en caisse jusqu'à la date d'effet). */
export function hasPendingDeletion(
  client: Pick<Client, "deletion_scheduled_for">,
): boolean {
  return Boolean(client.deletion_scheduled_for);
}
