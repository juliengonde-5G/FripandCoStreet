/**
 * Cahier du jour — types et appels (PR11, docs/ARCHITECTURE_PR11.md §1, M2).
 *
 * Le cahier est une page par journée : l'objectif du jour (déduit de
 * l'objectif du mois et des jours d'ouverture), ce qui a été réalisé, la
 * météo du matin, le rappel de l'an dernier, le message du jour,
 * l'opération en cours et les deux signatures.
 *
 * Rien n'est stocké côté navigateur : chaque affichage relit le serveur,
 * chaque saisie repart en base. Le serveur fige à la première lecture
 * d'une journée son objectif et son instantané météo — le front n'a donc
 * jamais à recalculer un objectif lui-même, il affiche ce qu'il reçoit.
 *
 * Montants : chaînes à deux décimales côté serveur (« 1234.50 »), comme
 * partout ailleurs dans l'application. `cahierAmount` les convertit pour
 * l'affichage sans jamais lever.
 */
import { api, fetchAPI } from "./api";

// ---------------------------------------------------------------------------
// Météo (M3)
// ---------------------------------------------------------------------------

/**
 * Instantané météo tel qu'il est figé dans la journée du cahier.
 *
 * Volontairement structurel (et non importé) : la carte météo de l'accueil
 * et des rapports expose le même objet sous le nom `Weather`
 * (`lib/reports.ts`). Les deux formes sont interchangeables pour
 * TypeScript ; le cahier n'a donc pas besoin d'une dépendance croisée pour
 * afficher sa ligne de météo.
 */
export interface CahierWeather {
  /** Vrai quand la météo n'a pas pu être lue (clé absente, ville absente,
   * erreur réseau). Dans ce cas seul `reason` est renseigné. */
  unavailable: boolean;
  /** Raison courte, affichée telle quelle à côté de « Météo indisponible ». */
  reason?: string | null;
  /** « ciel dégagé », « pluie modérée »… (OpenWeather, `lang=fr`). */
  description?: string | null;
  /** Températures en degrés Celsius. */
  temp?: number | null;
  temp_min?: number | null;
  temp_max?: number | null;
  /** Code d'icône OpenWeather (« 04d »), sans URL : c'est le front qui la
   * construit, pour ne dépendre d'aucun lien enregistré en base. */
  icon?: string | null;
  /** Vitesse du vent en m/s. */
  wind_speed?: number | null;
  city?: string | null;
  fetched_at?: string | null;
}

/**
 * Famille d'un code d'icône OpenWeather : les deux premiers caractères
 * (« 01 » ciel clair, « 10 » pluie…) et la marque du jour ou de la nuit.
 *
 * L'image distante `openweathermap.org/img/wn/…` n'est jamais chargée : la
 * boutique tourne sur une tablette qui n'a pas toujours d'accès sortant
 * vers ce domaine, et une icône manquante laisserait un carré vide. Chaque
 * écran dessine donc son glyphe à partir de cette famille.
 */
export function weatherFamily(icon: string | null | undefined): { family: string; night: boolean } {
  const code = (icon ?? "").trim();
  return { family: code.slice(0, 2), night: code.endsWith("n") };
}

// ---------------------------------------------------------------------------
// Une journée du cahier (`GET /api/cahier/{day}`)
// ---------------------------------------------------------------------------

/** Objectif du jour et situation du mois. */
export interface CahierTarget {
  /** Objectif de la journée, figé à la première lecture. `null` si le jour
   * est fermé ou qu'aucun objectif n'a été saisi. */
  daily: string | null;
  /** Objectif du mois entier (réglage `targets.monthly`). */
  monthly: string | null;
  /** Chiffre déjà réalisé depuis le 1er du mois. */
  month_realized: string;
  /** Avancement du mois en pourcentage (0 à 100 et au-delà). */
  month_progress_pct: number | null;
  /** Ce qu'il reste à faire sur le mois. */
  month_remaining: string | null;
  /** Ce qu'il faudrait faire chaque jour ouvert restant pour y arriver. */
  required_daily_rest_of_month: string | null;
}

/** Une heure de la journée dans le mini-graphique du réalisé. */
export interface CahierHour {
  /** 0 à 23, heure locale (Europe/Paris). */
  hour: number;
  net: string;
}

/** Ce qui a réellement été encaissé dans la journée. */
export interface CahierRealized {
  net: string;
  sales_count: number;
  average_basket: string;
  /** Avancement du jour par rapport à son objectif. `null` sans objectif. */
  progress_pct: number | null;
  by_hour: CahierHour[];
}

/** La même journée l'an dernier, quand elle existe. */
export interface CahierPreviousYear {
  day: string;
  net: string;
}

/** Signature du manager : l'identifiant du compte connecté. */
export interface CahierManagerSignature {
  at: string;
  username: string;
}

/** Signature de l'équipe : la vendeuse du tiroir, sinon un nom saisi. */
export interface CahierTeamSignature {
  at: string;
  name: string;
}

export interface CahierSignatures {
  manager: CahierManagerSignature | null;
  team: CahierTeamSignature | null;
}

/** Une journée complète du cahier. */
export interface CahierDay {
  /** Jour au format AAAA-MM-JJ. */
  day: string;
  /** 0 (lundi) à 6 (dimanche), comme `weekday_open`. */
  weekday: number;
  is_today: boolean;
  /** Vrai pour une journée révolue : textes et signatures sont fermés. */
  is_past: boolean;
  /** Vrai si la boutique est ouverte ce jour-là (réglage `cahier`). */
  is_open: boolean;
  target: CahierTarget;
  realized: CahierRealized;
  previous_year: CahierPreviousYear | null;
  message: string | null;
  operation: string | null;
  signatures: CahierSignatures;
  weather: CahierWeather | null;
}

// ---------------------------------------------------------------------------
// Réglage des jours d'ouverture (`GET`/`PUT /api/cahier/config`)
// ---------------------------------------------------------------------------

/**
 * Jours d'ouverture de la semaine, **lundi → dimanche** (7 booléens).
 * L'objectif mensuel est réparti à plat sur les jours ouverts ; un jour
 * fermé porte un objectif nul.
 */
export interface CahierConfig {
  weekday_open: boolean[];
}

/** Libellés des sept cases, dans l'ordre de `weekday_open`. */
export const WEEKDAY_LABELS = [
  "Lundi",
  "Mardi",
  "Mercredi",
  "Jeudi",
  "Vendredi",
  "Samedi",
  "Dimanche",
] as const;

/** Sept jours ouverts sauf le dimanche — même valeur que le réglage par
 * défaut du serveur, utilisée tant que la configuration n'est pas lue. */
export const DEFAULT_WEEKDAY_OPEN: boolean[] = [true, true, true, true, true, true, false];

/** Ramène n'importe quelle valeur reçue à sept booléens exploitables. */
export function normalizeWeekdayOpen(value: unknown): boolean[] {
  const source = Array.isArray(value) ? value : [];
  return DEFAULT_WEEKDAY_OPEN.map((fallback, index) =>
    typeof source[index] === "boolean" ? (source[index] as boolean) : fallback,
  );
}

// ---------------------------------------------------------------------------
// Appels
// ---------------------------------------------------------------------------

export type CahierSignatureRole = "manager" | "team";

/** Longueur maximale des deux textes libres (bornée aussi côté serveur). */
export const CAHIER_TEXT_MAX_LENGTH = 500;

/** Jour du cahier au format attendu par l'API (AAAA-MM-JJ, heure locale). */
export function toCahierDay(date: Date): string {
  const y = date.getFullYear();
  const m = `${date.getMonth() + 1}`.padStart(2, "0");
  const d = `${date.getDate()}`.padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** Aujourd'hui, au format du cahier. */
export function todayCahierDay(): string {
  return toCahierDay(new Date());
}

/** Journée voisine (`offset` en jours, négatif pour la veille). */
export function shiftCahierDay(day: string, offset: number): string {
  const [y, m, d] = day.split("-").map((part) => Number.parseInt(part, 10));
  const date = new Date(y, (m || 1) - 1, d || 1);
  if (Number.isNaN(date.getTime())) return day;
  date.setDate(date.getDate() + offset);
  return toCahierDay(date);
}

/**
 * Lit une journée du cahier. La première lecture d'un jour le crée côté
 * serveur (objectif et météo figés) : c'est une lecture pour le front, une
 * écriture d'archive pour le serveur.
 */
export async function fetchCahierDay(
  day: string,
  options?: { signal?: AbortSignal },
): Promise<CahierDay> {
  return fetchAPI<CahierDay>(`/api/cahier/${day}`, { signal: options?.signal });
}

/** Ce qu'on peut modifier dans le corps du cahier : deux textes libres. */
export interface CahierTextUpdate {
  message?: string;
  operation?: string;
}

/**
 * Enregistre le message du jour et/ou l'opération en cours.
 * Lève une `ApiError` 409 `day_closed` sur une journée révolue.
 */
export async function updateCahierText(
  day: string,
  update: CahierTextUpdate,
): Promise<CahierDay> {
  return api.put<CahierDay>(`/api/cahier/${day}/text`, update);
}

/**
 * Appose une signature. Le manager signe avec son compte ; l'équipe signe
 * avec la vendeuse identifiée sur le tiroir, sinon avec le nom saisi.
 * Lève une `ApiError` 409 `already_signed` ou `day_closed`.
 */
export async function signCahier(
  day: string,
  role: CahierSignatureRole,
  name?: string,
): Promise<CahierDay> {
  const body: { role: CahierSignatureRole; name?: string } = { role };
  const trimmed = (name ?? "").trim();
  if (trimmed) body.name = trimmed;
  return api.put<CahierDay>(`/api/cahier/${day}/signature`, body);
}

/** Jours d'ouverture de la semaine (carte « Jours d'ouverture » des réglages). */
export async function fetchCahierConfig(): Promise<CahierConfig> {
  const data = await api.get<Partial<CahierConfig>>("/api/cahier/config");
  return { weekday_open: normalizeWeekdayOpen(data?.weekday_open) };
}

/** Enregistre les jours d'ouverture (7 booléens, lundi → dimanche). */
export async function updateCahierConfig(weekdayOpen: boolean[]): Promise<CahierConfig> {
  const data = await api.put<Partial<CahierConfig>>("/api/cahier/config", {
    weekday_open: normalizeWeekdayOpen(weekdayOpen),
  });
  return { weekday_open: normalizeWeekdayOpen(data?.weekday_open) };
}

// ---------------------------------------------------------------------------
// Affichage
// ---------------------------------------------------------------------------

/**
 * Montant du cahier converti pour l'affichage. Le serveur envoie des
 * chaînes à deux décimales ; un déploiement plus ancien pourrait envoyer
 * un nombre. Les deux passent, rien ne lève, `null` pour « pas de valeur ».
 */
export function cahierAmount(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? value : Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** « Mardi 16 septembre 2026 » — l'en-tête de la page. */
export function formatCahierLongDate(day: string): string {
  const [y, m, d] = day.split("-").map((part) => Number.parseInt(part, 10));
  const date = new Date(y, (m || 1) - 1, d || 1);
  if (Number.isNaN(date.getTime())) return day;
  const label = date.toLocaleDateString("fr-FR", {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  });
  return label.charAt(0).toUpperCase() + label.slice(1);
}
