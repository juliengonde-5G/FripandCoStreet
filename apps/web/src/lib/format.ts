/**
 * Aides de formatage d'affichage — source unique pour l'UI.
 *
 * Convention typographique française :
 *   - virgule décimale
 *   - espace fine insécable pour les milliers
 *   - espace insécable avant le symbole €
 *
 * Fonctions pures : ne lèvent jamais, retournent toujours une chaîne.
 */

const NBSP = " ";
const NNBSP = " ";

export type CurrencyOptions = {
  /** Nombre de décimales (défaut 2). 0 pour les tuiles KPI, 2 pour les prix. */
  decimals?: number;
  /** Insère une NNBSP entre les milliers (défaut true). */
  thousands?: boolean;
};

/**
 * Formate un nombre en euros, convention française.
 *
 *   formatCurrency(12.5)          → "12,50 €"
 *   formatCurrency(1234.5)        → "1 234,50 €"
 *   formatCurrency(1234, { decimals: 0 }) → "1 234 €"
 *
 * ``null`` / ``undefined`` / ``NaN`` → "—" (tiret cadratin) pour que les
 * cellules vides s'affichent proprement sans "NaN €".
 */
export function formatCurrency(
  value: number | null | undefined,
  opts: CurrencyOptions = {},
): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  const decimals = opts.decimals ?? 2;
  const thousands = opts.thousands ?? true;
  const fixed = value.toFixed(decimals);
  const [intPart, decPart] = fixed.split(".");
  const intGrouped = thousands
    ? intPart.replace(/\B(?=(\d{3})+(?!\d))/g, NNBSP)
    : intPart;
  const body = decPart ? `${intGrouped},${decPart}` : intGrouped;
  return `${body}${NBSP}€`;
}

/**
 * Devise entière pour les tuiles KPI où les centimes ne comptent pas.
 */
export function formatCurrencyInt(value: number | null | undefined): string {
  return formatCurrency(value, { decimals: 0 });
}

/**
 * Formate un entier avec séparateur de milliers français. Pas de suffixe devise.
 *
 *   formatNumber(1234567)  → "1 234 567"
 */
export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return Math.round(value)
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, NNBSP);
}

/**
 * Formate une fraction en pourcentage à une décimale :
 *
 *   formatPercent(0.124)  → "12,4 %"
 *   formatPercent(12.4)   → "12,4 %"        (auto-détection 0..1 vs 0..100)
 */
export function formatPercent(
  value: number | null | undefined,
  opts: { decimals?: number; alreadyPercent?: boolean } = {},
): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  const decimals = opts.decimals ?? 1;
  const scaled = opts.alreadyPercent ?? Math.abs(value) > 1 ? value : value * 100;
  return `${scaled.toFixed(decimals).replace(".", ",")}${NBSP}%`;
}

/**
 * Formate une date ISO en format court français (jj/mm/aaaa).
 */
export function formatDate(value: string | Date | null | undefined): string {
  if (!value) return "—";
  const d = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit", year: "numeric" });
}

/**
 * Formate une date-heure ISO en format court français (jj/mm/aaaa hh:mm).
 */
export function formatDateTime(value: string | Date | null | undefined): string {
  if (!value) return "—";
  const d = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("fr-FR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Formate une taille en octets en unité lisible (PR5, écran Sauvegardes) :
 *
 *   formatBytes(1536)       → "1,5 Ko"
 *   formatBytes(52_428_800) → "50,0 Mo"
 *
 * Base 1024, unités françaises (o/Ko/Mo/Go/To). `null`/`undefined`/`NaN`
 * → "—", comme les autres formateurs de ce module.
 */
export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value) || value < 0) {
    return "—";
  }
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let v = value;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  const decimals = i === 0 ? 0 : 1;
  return `${v.toFixed(decimals).replace(".", ",")}${NBSP}${units[i]}`;
}

/**
 * Formate une date ISO en durée relative courte, française (PR5, écran
 * Sauvegardes — « dernière sauvegarde : il y a 3 h ») :
 *
 *   formatRelativeTime(nowIso)              → "à l'instant"
 *   formatRelativeTime(fiveMinutesAgo)      → "il y a 5 min"
 *   formatRelativeTime(threeDaysAgo)        → "il y a 3 j"
 *
 * Au-delà de 30 jours, retombe sur `formatDate` (date absolue courte) —
 * une durée relative en mois/années serait moins lisible qu'une date.
 * `null`/`undefined`/invalide → "—".
 */
export function formatRelativeTime(value: string | Date | null | undefined): string {
  if (!value) return "—";
  const d = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return "—";
  const diffSec = Math.round((Date.now() - d.getTime()) / 1000);
  if (diffSec < 60) return "à l'instant";
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `il y a ${diffMin} min`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `il y a ${diffHour} h`;
  const diffDay = Math.round(diffHour / 24);
  if (diffDay < 30) return `il y a ${diffDay} j`;
  return formatDate(value);
}

/**
 * Validation simple d'un e-mail, pour activer/désactiver un bouton d'envoi
 * côté front (PR3). La validation qui fait foi reste côté API (422 si
 * invalide) — celle-ci n'a qu'un rôle d'ergonomie de saisie.
 */
export function isValidEmail(value: string | null | undefined): boolean {
  if (!value) return false;
  const v = value.trim();
  if (!v || v.length > 254) return false;
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);
}

/**
 * Masque partiellement un e-mail pour un affichage sobre côté RGPD
 * (minimisation visuelle) : "julie.dupont@exemple.fr" → "j***@exemple.fr".
 */
export function maskEmail(email: string | null | undefined): string {
  if (!email) return "—";
  const at = email.indexOf("@");
  if (at <= 0) return "***";
  return `${email[0]}***@${email.slice(at + 1)}`;
}
