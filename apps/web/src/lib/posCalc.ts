/**
 * Calculs panier partagés entre la page /caisse et la modale de paiement —
 * doit rester le miroir exact de la règle serveur §4.1 point 4
 * (ARCHITECTURE_PR2.md) pour que le total présenté au comptoir corresponde
 * au total que l'API validera à l'encaissement.
 */
import type { DiscountInput } from "./types";

export function round2(n: number): number {
  return Math.round((n + Number.EPSILON) * 100) / 100;
}

export function computeBrut(lines: { unitPrice: number; quantity: number }[]): number {
  return round2(lines.reduce((s, l) => s + l.unitPrice * l.quantity, 0));
}

export function computeDiscountAmount(brut: number, discount: DiscountInput | null): number {
  if (!discount || brut <= 0 || discount.value <= 0) return 0;
  if (discount.type === "percent") {
    // Défensif : une valeur >100 % ne doit jamais annuler la remise, elle
    // est plafonnée à 100 % (remise totale) — voir clampDiscountValue,
    // qui plafonne déjà la valeur SAISIE avant stockage ; ce garde-fou
    // couvre toute autre origine (ex. état restauré, valeur non passée
    // par l'éditeur).
    const rate = Math.min(discount.value, 100);
    return round2((brut * rate) / 100);
  }
  return round2(Math.min(discount.value, brut));
}

/**
 * Plafonne une valeur de remise SAISIE avant de la stocker/afficher — un
 * pourcentage > 100 est ramené à 100, un montant € supérieur au total du
 * panier est ramené à ce total (ex. 200 € tapés sur un panier à 40 € →
 * remise réellement appliquée 40,00 €). Le chip de remise affiche ensuite
 * toujours la valeur stockée (donc déjà plafonnée), jamais le nombre tapé.
 */
export function clampDiscountValue(type: "percent" | "amount", value: number, brut: number): number {
  const safeValue = Number.isFinite(value) ? Math.max(0, value) : 0;
  if (type === "percent") return round2(Math.min(safeValue, 100));
  return round2(Math.min(safeValue, Math.max(0, brut)));
}
