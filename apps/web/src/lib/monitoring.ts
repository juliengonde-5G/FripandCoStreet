/**
 * Supervision technique et compatibilité matériel (PR12,
 * docs/ARCHITECTURE_PR12.md §1, N2 et N4).
 *
 * Deux lectures, aucune écriture métier : l'état technique de la caisse
 * (`GET /api/admin/monitoring`, recalcul des intégrités par
 * `POST /api/admin/monitoring/check`) et la liste du matériel supporté
 * (`GET /api/hardware/compatibility`).
 *
 * Aucun secret ne transite : le serveur ne renvoie que des booléens
 * « configuré » pour les services externes, jamais une clé. Les champs
 * que le serveur peut ne pas savoir calculer sont typés `null`, et
 * l'affichage doit savoir les rendre sans valeur (« — ») plutôt que de
 * supposer une valeur par défaut.
 */
import { api } from "./api";

/** Santé globale, calculée par le serveur (N2) :
 * `critical` = base KO, révision de base différente ou intégrité invalide ;
 * `warning` = sauvegarde périmée, terminal non prêt, imprimante hors
 * ligne, 500 dans l'heure ou file de paiements non vide. */
export type MonitoringStatus = "ok" | "warning" | "critical";

export interface MonitoringApp {
  version: string;
  build_sha: string | null;
  build_date: string | null;
  environment: string;
  expected_db_revision: string;
  current_db_revision: string | null;
  db_revision_ok: boolean;
  uptime_seconds: number;
}

export interface MonitoringDatabase {
  ok: boolean;
  latency_ms: number | null;
  size_bytes: number | null;
  tables_count: number | null;
}

export interface MonitoringBackupLast {
  created_at: string;
  status: string;
  size_bytes: number | null;
}

export interface MonitoringBackups {
  last: MonitoringBackupLast | null;
  nightly_enabled: boolean;
  dir_free_bytes: number | null;
  /** Dernier succès de sauvegarde remontant à plus de 36 h. */
  stale: boolean;
}

export interface MonitoringJobRun {
  at: string;
  status: "ok" | "failed";
  detail: string | null;
}

export interface MonitoringJob {
  name: string;
  cron: string | null;
  next_run_at: string | null;
  last_run: MonitoringJobRun | null;
}

/** Intégrités : `null` tant qu'elles n'ont jamais été calculées (le
 * serveur ne les recalcule qu'à la demande, puis les garde en cache dix
 * minutes) — l'écran affiche alors « jamais vérifié ». */
export interface MonitoringIntegrityChain {
  valid: boolean;
  count?: number | null;
  checked?: number | null;
  checked_at?: string | null;
}

export interface MonitoringIntegrity {
  jet: MonitoringIntegrityChain | null;
  fiscal: MonitoringIntegrityChain | null;
  closures: MonitoringIntegrityChain | null;
}

export interface MonitoringSumup {
  configured: boolean;
  reader_configured: boolean;
  last_ping: { ready: boolean; at: string } | null;
}

export interface MonitoringExternal {
  sumup: MonitoringSumup;
  brevo: { configured: boolean };
  openweather: { configured: boolean; cache_age_seconds: number | null };
}

export interface MonitoringPrinter {
  mode: string;
  online: boolean | null;
  latency_ms: number | null;
}

export interface MonitoringQueues {
  failed_payments_pending: number;
  sumup_exchange_errors_24h: number;
  clients_deletion_due: number;
}

/** Une réponse 500 récente, telle que le serveur l'a retenue dans son
 * tampon circulaire : jamais de corps, jamais de donnée personnelle. */
export interface MonitoringRecentError {
  at: string;
  request_id: string;
  method: string;
  path: string;
  status: number;
  error_type: string | null;
}

export interface Monitoring {
  generated_at: string;
  app: MonitoringApp;
  database: MonitoringDatabase;
  backups: MonitoringBackups;
  jobs: MonitoringJob[];
  integrity: MonitoringIntegrity;
  external: MonitoringExternal;
  printer: MonitoringPrinter;
  queues: MonitoringQueues;
  recent_errors: MonitoringRecentError[];
  status: MonitoringStatus;
}

export interface FetchMonitoringOptions {
  /** `true` → `?check=1` : le serveur recalcule les trois intégrités au
   * lieu de servir son cache (bouton « Vérifier maintenant »). */
  check?: boolean;
}

/** État technique complet. Lecture seule. */
export async function fetchMonitoring(options?: FetchMonitoringOptions): Promise<Monitoring> {
  const suffix = options?.check ? "?check=1" : "";
  return api.get<Monitoring>(`/api/admin/monitoring${suffix}`);
}

/** Recalcule les trois chaînes d'intégrité et renvoie l'état complet
 * rafraîchi — un seul aller-retour, pas de relecture derrière. */
export async function runMonitoringCheck(): Promise<Monitoring> {
  return api.post<Monitoring>("/api/admin/monitoring/check");
}

// ---------------------------------------------------------------------------
// N4 — matériel compatible
// ---------------------------------------------------------------------------

/** `tested` = vérifié en boutique, `recommended` = devrait marcher sans
 * avoir été branché, `not_supported` = à ne pas acheter. */
export type HardwareCompatibilityStatus = "tested" | "recommended" | "not_supported";

export interface HardwareCompatibilityItem {
  category: string;
  model: string;
  connection: string;
  status: HardwareCompatibilityStatus;
  notes: string | null;
}

export interface HardwareCompatibilityResponse {
  items: HardwareCompatibilityItem[];
}

/** Liste statique tenue côté serveur : une seule source de vérité pour
 * ce qu'on sait faire fonctionner. */
export async function fetchHardwareCompatibility(): Promise<HardwareCompatibilityItem[]> {
  const data = await api.get<HardwareCompatibilityResponse>("/api/hardware/compatibility");
  return data.items ?? [];
}
