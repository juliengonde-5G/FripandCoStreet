"use client";

/**
 * Journal comptable consultable — appels d'API (PR12,
 * `docs/ARCHITECTURE_PR12.md` §1 N1). Source unique des types et du chemin
 * du contrat, pour que la carte « Journal » de l'onglet Comptabilité ne
 * connaisse ni l'URL ni la forme brute de la réponse.
 *
 * Contrat (N1) :
 *   GET /api/admin/accounting/journal?from=AAAA-MM-JJ&to=AAAA-MM-JJ&account=&z=
 *     → {period, lines[], totals, accounts[], z_without_export[]}
 *     422 `invalid_date` / `period_too_long` (période ≤ 366 jours)
 *   Lecture seule : aucune écriture, aucun événement au journal technique.
 *
 * Montants : chaînes à deux décimales (« 1234.50 »), jamais des nombres
 * flottants côté transport — on les convertit au dernier moment pour
 * l'affichage (`journalAmount` → `formatCurrency`).
 */
import { api } from "./api";

/** Origine d'une ligne : l'export comptable enregistré du Z (`export`) ou
 * un calcul à la volée quand ce Z n'a pas encore d'export (`computed`,
 * son numéro figure alors dans `z_without_export`). */
export type AccountingJournalSource = "export" | "computed";

/** Une ligne du journal (`lines[]` du contrat N1). */
export interface AccountingJournalLine {
  /** Date comptable, ISO « AAAA-MM-JJ ». */
  date: string;
  z_report_number: number;
  z_report_id: string;
  account_number: string;
  account_label: string;
  label: string;
  /** Chaîne à deux décimales (« 0.00 » quand la ligne est au crédit). */
  debit: string;
  credit: string;
  source: AccountingJournalSource;
}

/** Cumul par compte sur la période (`accounts[]`). */
export interface AccountingJournalAccount {
  account_number: string;
  account_label: string;
  debit: string;
  credit: string;
}

/** Pied de tableau (`totals`) — `balanced` est calculé côté serveur. */
export interface AccountingJournalTotals {
  debit: string;
  credit: string;
  balanced: boolean;
}

export interface AccountingJournalPeriod {
  from: string;
  to: string;
}

/** Réponse complète de `GET /api/admin/accounting/journal`. */
export interface AccountingJournal {
  period: AccountingJournalPeriod;
  lines: AccountingJournalLine[];
  totals: AccountingJournalTotals;
  accounts: AccountingJournalAccount[];
  /** Numéros des Z dont l'export comptable n'est pas encore enregistré :
   * leurs lignes sont recalculées (`source: "computed"`). */
  z_without_export: number[];
}

/** Filtres de la carte « Journal ». `account` est un **préfixe** de numéro
 * de compte (« 7 » → tous les comptes de produits) ; `z` cible un seul
 * numéro de clôture. Les deux sont facultatifs et omis quand ils sont
 * vides, jamais envoyés en chaîne vide. */
export interface AccountingJournalQuery {
  from: string;
  to: string;
  account?: string;
  z?: number | string;
}

/** Construit la query string du contrat, filtres vides omis. */
export function journalQueryString(query: AccountingJournalQuery): string {
  const qs = new URLSearchParams({ from: query.from, to: query.to });
  const account = (query.account ?? "").trim();
  if (account) qs.set("account", account);
  const z = typeof query.z === "number" ? String(query.z) : (query.z ?? "").trim();
  if (z) qs.set("z", z);
  return qs.toString();
}

/** Lit le journal comptable sur une période. Les erreurs métier (422
 * `invalid_date` / `period_too_long`) remontent en `ApiError` : l'appelant
 * affiche `detail` tel quel. */
export async function fetchAccountingJournal(query: AccountingJournalQuery): Promise<AccountingJournal> {
  return api.get<AccountingJournal>(`/api/admin/accounting/journal?${journalQueryString(query)}`);
}

/** Chemin du CSV mensuel déjà existant (route `monthly-csv`, PR4) — repris
 * tel quel par le bouton « CSV du mois » de la carte « Journal ». */
export function monthlyCsvUrl(year: number, month: number): string {
  return `/api/admin/accounting/monthly-csv/${year}/${month}`;
}

/** Nom de fichier du CSV mensuel, cohérent avec la carte des écritures. */
export function monthlyCsvFilename(year: number, month: number): string {
  return `ecritures_${year}-${String(month).padStart(2, "0")}.csv`;
}

/** Montant transporté en chaîne (« 1234.50 ») converti pour l'affichage ;
 * une valeur illisible donne `null` → « — » plutôt que « NaN € ». */
export function journalAmount(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** `true` quand le montant est nul (« 0.00 ») : la cellule reste vide, le
 * tableau se lit alors en diagonale débit/crédit. */
export function isZeroAmount(value: string | null | undefined): boolean {
  const n = journalAmount(value);
  return n === null || n === 0;
}

/** Premier et dernier jour d'un mois, au format ISO attendu par le contrat.
 * Construit sans `Date` UTC pour éviter tout décalage de fuseau. */
export function monthRange(year: number, month: number): AccountingJournalPeriod {
  const lastDay = new Date(year, month, 0).getDate();
  const mm = String(month).padStart(2, "0");
  return { from: `${year}-${mm}-01`, to: `${year}-${mm}-${String(lastDay).padStart(2, "0")}` };
}

/** Mois précédent / suivant, pour les boutons ‹ › du sélecteur de période. */
export function shiftMonth(year: number, month: number, delta: number): { year: number; month: number } {
  const index = year * 12 + (month - 1) + delta;
  return { year: Math.floor(index / 12), month: (index % 12) + 1 };
}
