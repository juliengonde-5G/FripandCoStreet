/**
 * Paiements carte — file des échecs récupérables et journal des échanges
 * avec le terminal (PR9, docs/ARCHITECTURE_PR9.md §1, K2/K3/K4).
 *
 * Un seul endroit pour les appels de la file (`failed_payments`) et du
 * journal des échanges (`sumup_exchanges`) : la caisse y prend le
 * réessai d'un paiement mis en file, l'administration y prend la liste,
 * l'analyse, le journal et la purge.
 *
 * Aucun état ici, aucun stockage navigateur : la file vit en base, le
 * front la relit à chaque fois. Le journal ne contient jamais de donnée
 * personnelle ni de secret — le serveur rédige avant d'écrire (K1), le
 * front se contente d'afficher ce qu'il reçoit.
 */
import { api } from "./api";
import type { CbCheckoutState } from "./types";

// ---------------------------------------------------------------------------
// Journal des échanges avec le terminal (K1/K2)
// ---------------------------------------------------------------------------

/** Opérations journalisées — un appel sortant vers le terminal chacune. */
export type SumupOperation =
  | "ping_reader"
  | "push_to_reader"
  | "checkout_status"
  | "reader_checkout_status"
  | "cancel_checkout"
  | "terminate_reader"
  | "refund"
  | "get_transaction";

/** Causes d'échec d'un échange (K1). */
export type SumupErrorType = "transport" | "timeout" | "http_4xx" | "http_5xx" | "decode";

/** Un appel sortant vers le terminal, rédigé côté serveur.
 * `request_payload` / `response_payload` sont du JSON libre : le front
 * les affiche formatés sous le mot « détail », jamais autrement. */
export interface SumupExchange {
  id: string;
  created_at: string;
  operation: SumupOperation;
  method: string;
  url_path: string;
  request_payload: unknown | null;
  response_status: number | null;
  response_payload: unknown | null;
  duration_ms: number;
  /** Essais de transport rejoués avant d'obtenir cette réponse. */
  retry_count: number;
  is_error: boolean;
  error_type: SumupErrorType | null;
  error_message: string | null;
  checkout_id: string | null;
  client_transaction_id: string | null;
  request_id: string | null;
}

export interface SumupExchangeListResponse {
  exchanges: SumupExchange[];
  total: number;
}

/** Filtres de `GET /api/admin/sumup-exchanges`. Tous facultatifs ; les
 * champs vides ne sont pas envoyés. `limit` est borné à 500 côté
 * serveur. `from` / `to` sont des dates ISO (`2026-09-15` ou horodatage
 * complet). */
export interface SumupExchangeFilters {
  only_failed?: boolean;
  operation?: SumupOperation | "";
  error_type?: SumupErrorType | "";
  checkout_id?: string;
  from?: string;
  to?: string;
  limit?: number;
}

function buildQuery(filters: SumupExchangeFilters): string {
  const params = new URLSearchParams();
  if (filters.only_failed) params.set("only_failed", "true");
  if (filters.operation) params.set("operation", filters.operation);
  if (filters.error_type) params.set("error_type", filters.error_type);
  if (filters.checkout_id?.trim()) params.set("checkout_id", filters.checkout_id.trim());
  if (filters.from) params.set("from", filters.from);
  if (filters.to) params.set("to", filters.to);
  if (filters.limit !== undefined) params.set("limit", String(filters.limit));
  const query = params.toString();
  return query ? `?${query}` : "";
}

/** Journal des échanges, le plus récent d'abord. */
export async function fetchSumupExchanges(
  filters: SumupExchangeFilters = {},
): Promise<SumupExchangeListResponse> {
  const data = await api.get<SumupExchangeListResponse>(
    `/api/admin/sumup-exchanges${buildQuery(filters)}`,
  );
  const exchanges = data?.exchanges ?? [];
  return { exchanges, total: data?.total ?? exchanges.length };
}

/** Vide le journal — renvoie le nombre de lignes effacées. Geste
 * irréversible : l'appelant confirme avant. */
export async function purgeSumupExchanges(): Promise<number> {
  const data = await api.delete<{ deleted: number }>("/api/admin/sumup-exchanges");
  return data?.deleted ?? 0;
}

// ---------------------------------------------------------------------------
// Analyse des échecs (K2)
// ---------------------------------------------------------------------------

export interface PaymentFailuresAttempts {
  failed: number;
  paid: number;
  pending: number;
  cancelled: number;
}

export interface PaymentFailureTopError {
  error_message: string;
  count: number;
}

export interface PaymentFailureByOperation {
  operation: SumupOperation;
  count: number;
  errors: number;
}

export interface PaymentFailureRetries {
  queued: number;
  succeeded: number;
  exhausted: number;
  abandoned: number;
}

/** `GET /api/admin/payment-failures?days=` — photographie de la période. */
export interface PaymentFailuresReport {
  period_days: number;
  attempts: PaymentFailuresAttempts;
  /** Dix causes les plus fréquentes, décroissant. */
  top_errors: PaymentFailureTopError[];
  /** Nombre d'échanges en erreur par cause (`transport`, `timeout`…). */
  exchanges_by_error_type: Partial<Record<SumupErrorType, number>>;
  by_operation: PaymentFailureByOperation[];
  retries: PaymentFailureRetries;
}

const EMPTY_REPORT_ATTEMPTS: PaymentFailuresAttempts = { failed: 0, paid: 0, pending: 0, cancelled: 0 };
const EMPTY_REPORT_RETRIES: PaymentFailureRetries = { queued: 0, succeeded: 0, exhausted: 0, abandoned: 0 };

export async function fetchPaymentFailures(days: number): Promise<PaymentFailuresReport> {
  const data = await api.get<PaymentFailuresReport>(`/api/admin/payment-failures?days=${days}`);
  return {
    period_days: data?.period_days ?? days,
    attempts: { ...EMPTY_REPORT_ATTEMPTS, ...(data?.attempts ?? {}) },
    top_errors: data?.top_errors ?? [],
    exchanges_by_error_type: data?.exchanges_by_error_type ?? {},
    by_operation: data?.by_operation ?? [],
    retries: { ...EMPTY_REPORT_RETRIES, ...(data?.retries ?? {}) },
  };
}

// ---------------------------------------------------------------------------
// File des paiements échoués (K3)
// ---------------------------------------------------------------------------

export type FailedPaymentStatus = "pending" | "succeeded" | "exhausted" | "abandoned";

/** Causes d'un échec mis en file : celles d'un échange, plus le refus de
 * carte (`declined`, qui n'est jamais mis en file mais peut remonter). */
export type FailedPaymentErrorType = SumupErrorType | "declined";

/** Une ligne de la file : un encaissement carte qui n'a pas abouti pour
 * une cause récupérable, que la vendeuse peut relancer. Aucune vente
 * n'existe tant que le paiement n'est pas accepté. */
export interface FailedPayment {
  id: string;
  created_at: string;
  updated_at: string;
  attempt_id: string;
  client_uuid: string;
  amount: number;
  status: FailedPaymentStatus;
  error_type: FailedPaymentErrorType | null;
  last_error: string | null;
  retry_count: number;
  max_retries: number;
  next_retry_at: string | null;
  resolved_at: string | null;
  /** Vente finalement encaissée, une fois le paiement accepté. */
  transaction_id: string | null;
  cashier_id: string | null;
}

export interface FailedPaymentListResponse {
  failed_payments: FailedPayment[];
}

/** File des paiements en échec, filtrée par statut (par défaut : tous). */
export async function fetchFailedPayments(status?: FailedPaymentStatus): Promise<FailedPayment[]> {
  const query = status ? `?status=${status}` : "";
  const data = await api.get<FailedPaymentListResponse | FailedPayment[]>(
    `/api/admin/failed-payments${query}`,
  );
  if (Array.isArray(data)) return data;
  return data?.failed_payments ?? [];
}

/** Renonce à un encaissement en file (motif libre, 200 caractères). */
export async function abandonFailedPayment(id: string, reason: string): Promise<FailedPayment> {
  return api.post<FailedPayment>(`/api/admin/failed-payments/${id}/abandon`, { reason });
}

/** Réponse d'un réessai : un nouvel encaissement est poussé sur le
 * terminal, la caisse reprend son suivi sur `checkout_id`. */
export interface RetryFailedPaymentResponse {
  checkout_id: string;
  status: CbCheckoutState;
  failed_payment_id?: string;
  retry_count?: number;
}

/** Relance un paiement de la file — même mécanique qu'un réessai normal
 * (nouvel encaissement sur le terminal). Lève une `ApiError` 409
 * `retries_exhausted` au-delà du nombre de réessais autorisés. */
export async function retryFailedPayment(id: string): Promise<RetryFailedPaymentResponse> {
  return api.post<RetryFailedPaymentResponse>(`/api/pos/payments/cb/retry-failed/${id}`, {});
}

// ---------------------------------------------------------------------------
// Libellés partagés (caisse et administration parlent la même langue)
// ---------------------------------------------------------------------------

export const OPERATION_LABELS: Record<SumupOperation, string> = {
  ping_reader: "Test du terminal",
  push_to_reader: "Envoi au terminal",
  checkout_status: "État de l'encaissement",
  reader_checkout_status: "État côté terminal",
  cancel_checkout: "Annulation de l'encaissement",
  terminate_reader: "Arrêt du terminal",
  refund: "Remboursement",
  get_transaction: "Lecture de l'opération",
};

export const ERROR_TYPE_LABELS: Record<FailedPaymentErrorType, string> = {
  transport: "Réseau",
  timeout: "Délai dépassé",
  http_4xx: "Demande refusée",
  http_5xx: "Panne côté terminal",
  decode: "Réponse illisible",
  declined: "Carte refusée",
};

export const FAILED_PAYMENT_STATUS_LABELS: Record<FailedPaymentStatus, string> = {
  pending: "En attente",
  succeeded: "Encaissé",
  exhausted: "Réessais épuisés",
  abandoned: "Abandonné",
};

/** Motif d'abandon — borne du contrat, partagée par le champ de saisie. */
export const ABANDON_REASON_MAX_LENGTH = 200;
