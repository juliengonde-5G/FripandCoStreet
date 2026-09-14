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
