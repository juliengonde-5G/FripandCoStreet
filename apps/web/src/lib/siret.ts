/**
 * SIRET et numéro de TVA intracommunautaire — validation et mise en forme
 * côté front (PR8, J5/J6).
 *
 * La validation qui fait foi reste côté API (422 `invalid_siret`) : celle-ci
 * n'a qu'un rôle d'ergonomie de saisie (message immédiat sous le champ,
 * bouton « Émettre la facture » désactivé), comme `isValidEmail` dans
 * lib/format.ts. Fonctions pures, elles ne lèvent jamais.
 */

/** Ne garde que les chiffres — la saisie peut contenir des espaces (le
 * champ affiche « 123 456 789 00012 »), des points ou des tirets. */
export function normalizeSiret(value: string | null | undefined): string {
  if (!value) return "";
  return value.replace(/\D/g, "").slice(0, 14);
}

/**
 * Clé de Luhn sur 14 chiffres (norme SIRET : somme de Luhn multiple de 10).
 * Sur un SIRET, ce sont les chiffres de rang PAIR en partant de la gauche
 * (positions 2, 4, …, indices impairs) qui sont doublés — c'est-à-dire un
 * rang sur deux en partant de la droite, le dernier chiffre (clé) n'étant
 * jamais doublé.
 */
function luhnOk(digits: string): boolean {
  let sum = 0;
  for (let i = 0; i < digits.length; i++) {
    // On double un rang sur deux en partant de la DROITE, clé exclue.
    const fromRight = digits.length - 1 - i;
    let n = digits.charCodeAt(i) - 48;
    if (fromRight % 2 === 1) {
      n *= 2;
      if (n > 9) n -= 9;
    }
    sum += n;
  }
  return sum % 10 === 0;
}

/** Vrai si la saisie contient 14 chiffres ET une clé de Luhn correcte. */
export function validateSiret(value: string | null | undefined): boolean {
  const digits = normalizeSiret(value);
  if (digits.length !== 14) return false;
  return luhnOk(digits);
}

/**
 * Message d'erreur lisible sous le champ SIRET, ou `null` si la saisie est
 * valide (ou encore vide : on n'accuse pas un champ qu'on n'a pas rempli).
 */
export function siretError(value: string | null | undefined): string | null {
  const digits = normalizeSiret(value);
  if (digits.length === 0) return null;
  if (digits.length < 14) return `SIRET incomplet : ${digits.length} chiffre${digits.length > 1 ? "s" : ""} sur 14.`;
  if (!luhnOk(digits)) return "Ce SIRET n'est pas valide (clé de contrôle incorrecte). Vérifiez les 14 chiffres.";
  return null;
}

/** « 12345678900012 » → « 123 456 789 00012 » (SIREN + NIC, lecture à voix
 * haute au comptoir). Formate aussi une saisie partielle, au fil de la
 * frappe. */
export function formatSiret(value: string | null | undefined): string {
  const d = normalizeSiret(value);
  const parts = [d.slice(0, 3), d.slice(3, 6), d.slice(6, 9), d.slice(9, 14)].filter(Boolean);
  return parts.join(" ");
}

/** Normalise un n° de TVA : majuscules, sans espace ni ponctuation. */
export function normalizeVatNumber(value: string | null | undefined): string {
  if (!value) return "";
  return value.toUpperCase().replace(/[^0-9A-Z]/g, "").slice(0, 13);
}

/**
 * Format français : `FR` + clé sur 2 caractères (chiffres ou lettres, I et O
 * exclues par l'administration) + les 9 chiffres du SIREN — « FRXX123456789 ».
 * Le champ est FACULTATIF : une saisie vide est considérée comme valide.
 */
export function validateVatNumber(value: string | null | undefined): boolean {
  const v = normalizeVatNumber(value);
  if (v.length === 0) return true;
  return /^FR[0-9A-HJ-NP-Z]{2}\d{9}$/.test(v);
}

/** Message d'erreur du champ TVA, ou `null` (vide = pas d'erreur). */
export function vatNumberError(value: string | null | undefined): string | null {
  const v = normalizeVatNumber(value);
  if (v.length === 0) return null;
  if (validateVatNumber(v)) return null;
  return "Format attendu : FR suivi de 11 caractères (ex. FR40123456789).";
}

/**
 * Cohérence SIREN : les 9 premiers chiffres du SIRET doivent être les 9
 * derniers du n° de TVA. Renvoie un avertissement lisible, jamais bloquant
 * (c'est le serveur qui tranche) — `null` si l'un des deux est absent,
 * incomplet ou si les deux concordent.
 */
export function vatSirenMismatch(
  siret: string | null | undefined,
  vatNumber: string | null | undefined,
): string | null {
  const s = normalizeSiret(siret);
  const v = normalizeVatNumber(vatNumber);
  if (s.length !== 14 || !validateVatNumber(v) || v.length === 0) return null;
  if (s.slice(0, 9) === v.slice(4)) return null;
  return "Le n° de TVA ne correspond pas au SIRET saisi (SIREN différent).";
}
