/**
 * Rapports quotidien / hebdomadaire / mensuel et météo locale
 * (PR11, docs/ARCHITECTURE_PR11.md §1, M1 et M3).
 *
 * Un seul endroit pour :
 *   - les trois routes de rapport (`GET /api/reports/daily|weekly|monthly`)
 *     et leur variante CSV (`?format=csv`) ;
 *   - la météo (`GET /api/reports/weather`), affichée à côté du chiffre du
 *     jour sur l'accueil et figée dans le cahier du jour ;
 *   - le réglage « Météo » (`GET/PUT /api/admin/settings/weather`).
 *
 * Lecture seule côté fiscal : rien ici n'écrit une vente, un Z ou une
 * ligne de journal — l'export CSV est le seul geste tracé, et il l'est
 * côté serveur (JET `export.downloaded`).
 *
 * Convention de montants du contrat : **chaînes à deux décimales**
 * ("1234.50"). On ne les convertit jamais implicitement ; `parseAmount`
 * est le seul passage vers un nombre (barres du graphique, tests de
 * présence d'un objectif).
 */
import { api } from "./api";
import { downloadFile } from "./download";

// ---------------------------------------------------------------------------
// Période
// ---------------------------------------------------------------------------

/** Les trois granularités du contrat M1. */
export type ReportPeriodKind = "daily" | "weekly" | "monthly";

export const REPORT_PERIOD_KINDS: ReportPeriodKind[] = ["daily", "weekly", "monthly"];

/** Libellés du sélecteur (M5 : « Jour / Semaine / Mois »). */
export const REPORT_PERIOD_LABELS: Record<ReportPeriodKind, string> = {
  daily: "Jour",
  weekly: "Semaine",
  monthly: "Mois",
};

/**
 * Bornes de la période retenue par le serveur. `from`/`to` sont des jours
 * civils Europe/Paris (`AAAA-MM-JJ`, inclusifs) ; `label` est le libellé
 * lisible calculé côté serveur (« lundi 14 septembre 2026 », « semaine du
 * 14 au 20 septembre 2026 », « septembre 2026 »).
 */
export interface ReportPeriod {
  kind: ReportPeriodKind;
  from: string;
  to: string;
  label: string;
}

// ---------------------------------------------------------------------------
// Corps du rapport
// ---------------------------------------------------------------------------

/** Totaux de la période. Montants en chaînes à deux décimales. */
export interface ReportTotals {
  sales_count: number;
  refunds_count: number;
  /** Ventes brutes, annulations non déduites. */
  gross: string;
  /** Total des annulations (valeur positive). */
  refunds: string;
  /** `gross` − `refunds`. */
  net: string;
  /** Net / nombre de ventes non annulées ("0.00" si aucune vente). */
  average_basket: string;
  /** Nombre de lignes d'articles vendues (libellés saisis en caisse). */
  items_count: number;
}

/** Encaissements par moyen de paiement, annulations déduites. */
export interface ReportPayments {
  cash: string;
  card: string;
}

/** Un point de la série quotidienne (rapports hebdomadaire et mensuel). */
export interface ReportDayPoint {
  /** `AAAA-MM-JJ`. */
  date: string;
  net: string;
  sales_count: number;
}

/** Un point de la série horaire (rapport quotidien, 24 entrées, 0 → 23). */
export interface ReportHourPoint {
  hour: number;
  net: string;
  sales_count: number;
}

/** Une ligne du tableau « Par vendeuse ». */
export interface ReportCashierRow {
  /** `null` pour les ventes saisies sans vendeuse identifiée. */
  cashier_id: string | null;
  display_name: string;
  sales_count: number;
  sales_total: string;
  refunds_count: number;
  refunds_total: string;
  net_total: string;
}

/** Une ligne du tableau « Articles les plus vendus » (10 au plus, libellé
 * normalisé côté serveur : minuscules, espaces réduits). */
export interface ReportTopItem {
  label: string;
  quantity: number;
  net: string;
}

/** Même période précédente. `delta_pct` vaut `null` quand la période
 * précédente est à zéro (pas de variation calculable). */
export interface ReportPrevious {
  from: string;
  to: string;
  net: string;
  delta_pct: number | null;
}

/** Objectif de la période, `null` quand aucun objectif n'est fixé. */
export interface ReportTarget {
  amount: string;
  /** 0 à 999, une décimale — calculé par le serveur, jamais recalculé ici. */
  progress_pct: number;
}

/** Un rapport Z clôturé dans la période. */
export interface ReportZ {
  report_number: number;
  closed_at: string;
  net: string;
}

/**
 * Enveloppe commune aux trois routes (M1). `by_hour` n'est présent que sur
 * le rapport quotidien, `by_day` que sur l'hebdomadaire et le mensuel ;
 * `weather` n'est présent que sur le quotidien (instantané du cahier).
 */
export interface Report {
  period: ReportPeriod;
  totals: ReportTotals;
  payments: ReportPayments;
  by_day?: ReportDayPoint[];
  by_hour?: ReportHourPoint[];
  by_cashier: ReportCashierRow[];
  top_items: ReportTopItem[];
  previous: ReportPrevious;
  target: ReportTarget | null;
  z_reports: ReportZ[];
  weather?: Weather | null;
}

// ---------------------------------------------------------------------------
// Météo (M3)
// ---------------------------------------------------------------------------

/** Météo disponible : instantané OpenWeather mis en forme par le serveur. */
export interface WeatherAvailable {
  unavailable: false;
  /** Description en français, déjà traduite par l'API (`lang=fr`). */
  description: string;
  /** Degrés Celsius (`units=metric`). */
  temp: number;
  temp_min: number;
  temp_max: number;
  /** Code d'icône OpenWeather (« 04d », « 10n »…). */
  icon: string;
  /** Vent en m/s. */
  wind_speed: number;
  city: string;
  /** Horodatage ISO de l'appel sortant (cache 15 min côté serveur). */
  fetched_at: string;
}

/** Météo indisponible : clé absente, ville absente, erreur réseau. Jamais
 * une exception, jamais la clé dans la raison. */
export interface WeatherUnavailable {
  unavailable: true;
  /** Raison courte, affichée telle quelle en gris à côté du chiffre. */
  reason?: string | null;
}

export type Weather = WeatherAvailable | WeatherUnavailable;

/** Garde de type : `true` quand la météo est exploitable. */
export function isWeatherAvailable(weather: Weather | null | undefined): weather is WeatherAvailable {
  return !!weather && weather.unavailable === false;
}

/** Réglage « Météo » (`GET/PUT /api/admin/settings/weather`).
 *
 * `lat`/`lon` sont facultatives : sans elles, la ville est passée telle
 * quelle à OpenWeather. `api_key_configured` est une information de
 * lecture seule (la valeur de la clé n'est jamais renvoyée) ; un backend
 * qui ne la fournit pas laisse le champ absent et l'écran déduit l'état de
 * la réponse de `fetchWeather`. */
export interface WeatherSettings {
  city: string;
  lat: number | null;
  lon: number | null;
  api_key_configured?: boolean;
}

// ---------------------------------------------------------------------------
// Appels
// ---------------------------------------------------------------------------

/** Chemin d'une route de rapport. `value` est un jour `AAAA-MM-JJ` pour
 * `daily`/`weekly` (la semaine ISO qui contient ce jour) et un mois
 * `AAAA-MM` pour `monthly`. */
export function reportPath(kind: ReportPeriodKind, value: string, format?: "csv"): string {
  const param = kind === "monthly" ? `month=${encodeURIComponent(value)}` : `date=${encodeURIComponent(value)}`;
  const suffix = format === "csv" ? "&format=csv" : "";
  return `/api/reports/${kind}?${param}${suffix}`;
}

/** `GET /api/reports/{daily|weekly|monthly}` — 422 `invalid_date` si la
 * date ne respecte pas le format attendu (l'appelant affiche `detail`). */
export async function fetchReport(kind: ReportPeriodKind, value: string): Promise<Report> {
  return api.get<Report>(reportPath(kind, value));
}

/** Chemin CSV de la même période (`?format=csv`). Jamais utilisé comme
 * `href` direct : le jeton d'authentification doit être posé, d'où
 * `downloadReportCsv` ci-dessous. */
export function reportCsvUrl(kind: ReportPeriodKind, value: string): string {
  return reportPath(kind, value, "csv");
}

/** Nom de fichier du contrat M1 : `rapport_<kind>_<from>.csv`. */
export function reportCsvFilename(kind: ReportPeriodKind, from: string): string {
  return `rapport_${kind}_${from}.csv`;
}

/**
 * Télécharge le CSV de la période (UTF-8 avec BOM, séparateur `;`, côté
 * serveur). `from` est la borne basse renvoyée par le rapport affiché ;
 * à défaut, la valeur du sélecteur fait l'affaire.
 */
export async function downloadReportCsv(
  kind: ReportPeriodKind,
  value: string,
  from?: string,
): Promise<void> {
  await downloadFile(reportCsvUrl(kind, value), reportCsvFilename(kind, from ?? value));
}

/** `GET /api/reports/weather` — ne lève jamais côté serveur : une météo
 * indisponible est une réponse normale (`{unavailable: true, reason}`). */
export async function fetchWeather(): Promise<Weather> {
  return api.get<Weather>("/api/reports/weather");
}

/** `GET /api/admin/settings/weather`. */
export async function fetchWeatherSettings(): Promise<WeatherSettings> {
  return api.get<WeatherSettings>("/api/admin/settings/weather");
}

/** `PUT /api/admin/settings/weather` — ville, latitude, longitude. La clé
 * d'API reste une variable d'environnement, jamais un réglage. */
export async function saveWeatherSettings(settings: WeatherSettings): Promise<WeatherSettings> {
  return api.put<WeatherSettings>("/api/admin/settings/weather", {
    city: settings.city,
    lat: settings.lat,
    lon: settings.lon,
  });
}

// ---------------------------------------------------------------------------
// Aides de dates — jours civils, jamais d'UTC
// ---------------------------------------------------------------------------

/** `Date` → `AAAA-MM-JJ` en heure locale (jamais `toISOString()`, qui
 * bascule en UTC et décale d'un jour en soirée). */
export function toIsoDay(d: Date): string {
  const y = d.getFullYear();
  const m = `${d.getMonth() + 1}`.padStart(2, "0");
  const day = `${d.getDate()}`.padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** `"2026-09-15"` → `Date` locale, `null` si la chaîne n'est pas un jour. */
export function parseIsoDay(iso: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Jour courant, `AAAA-MM-JJ`. */
export function todayIso(): string {
  return toIsoDay(new Date());
}

/** Mois courant, `AAAA-MM`. */
export function currentMonthIso(): string {
  return todayIso().slice(0, 7);
}

/** Décale un jour de `days` jours (négatif = vers le passé). */
export function shiftIsoDay(iso: string, days: number): string {
  const d = parseIsoDay(iso);
  if (!d) return iso;
  d.setDate(d.getDate() + days);
  return toIsoDay(d);
}

/** Décale un mois `AAAA-MM` de `months` mois. */
export function shiftIsoMonth(iso: string, months: number): string {
  const m = /^(\d{4})-(\d{2})$/.exec(iso);
  if (!m) return iso;
  const d = new Date(Number(m[1]), Number(m[2]) - 1 + months, 1);
  return `${d.getFullYear()}-${`${d.getMonth() + 1}`.padStart(2, "0")}`;
}

/** Lundi de la semaine ISO qui contient `iso`. */
export function isoWeekStart(iso: string): string {
  const d = parseIsoDay(iso);
  if (!d) return iso;
  // getDay() : 0 = dimanche → 6 jours après le lundi.
  const offset = (d.getDay() + 6) % 7;
  d.setDate(d.getDate() - offset);
  return toIsoDay(d);
}

/** Valeur du sélecteur pour une granularité : un jour, ou un mois. */
export function defaultPeriodValue(kind: ReportPeriodKind): string {
  return kind === "monthly" ? currentMonthIso() : todayIso();
}

/** Pas de navigation ‹ › selon la granularité. */
export function shiftPeriodValue(kind: ReportPeriodKind, value: string, direction: -1 | 1): string {
  if (kind === "monthly") return shiftIsoMonth(value, direction);
  return shiftIsoDay(value, kind === "weekly" ? 7 * direction : direction);
}

/** Conversion d'une valeur de sélecteur quand on change de granularité :
 * un mois donne son premier jour, un jour donne son mois. */
export function convertPeriodValue(
  from: ReportPeriodKind,
  to: ReportPeriodKind,
  value: string,
): string {
  if (from === to) return value;
  if (to === "monthly") return value.slice(0, 7);
  if (from === "monthly") {
    const today = todayIso();
    // Le mois courant reste sur aujourd'hui ; un autre mois part du 1er.
    return today.startsWith(value) ? today : `${value}-01`;
  }
  return value;
}

/** Les montants du contrat sont des chaînes : seul passage vers un nombre. */
export function parseAmount(value: string | number | null | undefined): number {
  if (typeof value === "number") return Number.isFinite(value) ? value : 0;
  const n = Number.parseFloat(value ?? "");
  return Number.isFinite(n) ? n : 0;
}
