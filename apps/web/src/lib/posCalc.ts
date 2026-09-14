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
  if (!discount || brut <= 0) return 0;
  if (discount.type === "percent") {
    if (discount.value <= 0 || discount.value > 100) return 0;
    return round2((brut * discount.value) / 100);
  }
  if (discount.value <= 0) return 0;
  return round2(Math.min(discount.value, brut));
}
