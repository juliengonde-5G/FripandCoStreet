"use client";

/**
 * Facture B2B — appels d'API (PR8, J5/J6). Source unique pour la caisse
 * (écran de fin de vente, détail d'un ticket) et l'administration
 * (Comptabilité > Factures), afin qu'un seul endroit connaisse les chemins
 * du contrat et le nom de fichier du PDF.
 *
 * Contrat (docs/ARCHITECTURE_PR8.md §1, J5) :
 *   POST /api/pos/transactions/{id}/invoice  → 201 {invoice}
 *        422 `invalid_siret` · 409 `invoice_exists` / `transaction_cancelled`
 *        / `not_a_sale`
 *   GET  /api/pos/transactions/{id}/invoice  → {invoice} · 404 `not_found`
 *   GET  /api/pos/invoices/{id}/pdf          → application/pdf
 *   GET  /api/admin/invoices?year=2026       → {invoices:[…]}
 */
import { api, ApiError } from "./api";
import { downloadFile } from "./download";
import type { Invoice, InvoiceListResponse, InvoiceResponse, IssueInvoiceRequest } from "./types";
import { normalizeSiret, normalizeVatNumber } from "./siret";

/** Nettoie la saisie du formulaire avant l'envoi : espaces retirés autour
 * des champs, SIRET réduit à ses 14 chiffres, n° de TVA en majuscules, et
 * champs facultatifs vides OMIS plutôt qu'envoyés en chaîne vide. */
export function buildInvoicePayload(form: {
  company_name: string;
  siret: string;
  vat_number: string;
  address_line1: string;
  address_line2: string;
  postal_code: string;
  city: string;
}): IssueInvoiceRequest {
  const payload: IssueInvoiceRequest = {
    company_name: form.company_name.trim(),
    siret: normalizeSiret(form.siret),
    address_line1: form.address_line1.trim(),
    postal_code: form.postal_code.trim(),
    city: form.city.trim(),
  };
  const vat = normalizeVatNumber(form.vat_number);
  if (vat) payload.vat_number = vat;
  const line2 = form.address_line2.trim();
  if (line2) payload.address_line2 = line2;
  return payload;
}

/** Émet la facture d'une vente. Les erreurs métier remontent telles quelles
 * (`ApiError` : `detail` affiché tel quel, `code` pour les branches). */
export async function issueInvoice(transactionId: string, payload: IssueInvoiceRequest): Promise<Invoice> {
  const data = await api.post<InvoiceResponse>(`/api/pos/transactions/${transactionId}/invoice`, payload);
  return data.invoice;
}

/** Facture (ou avoir) d'une transaction — `null` quand il n'y en a pas
 * (404 `not_found`), ce qui n'est pas une erreur côté UI. Toute autre
 * erreur est relancée. */
export async function fetchInvoiceForTransaction(transactionId: string): Promise<Invoice | null> {
  try {
    const data = await api.get<InvoiceResponse>(`/api/pos/transactions/${transactionId}/invoice`);
    return data.invoice;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

/** Liste des factures et avoirs d'une année (administration). */
export async function listInvoices(year: number): Promise<Invoice[]> {
  const data = await api.get<InvoiceListResponse>(`/api/admin/invoices?year=${year}`);
  return data.invoices;
}

/** Nom du fichier téléchargé : le n° du document, exactement comme le
 * `Content-Disposition` du contrat (`F-2026-0001.pdf`). */
export function invoicePdfFilename(invoice: Invoice): string {
  return `${invoice.invoice_number}.pdf`;
}

/** Télécharge le PDF (jeton posé, jamais un lien direct — cf. lib/download.ts). */
export async function downloadInvoicePdf(invoice: Invoice): Promise<void> {
  await downloadFile(`/api/pos/invoices/${invoice.id}/pdf`, invoicePdfFilename(invoice));
}

/** « Facture » / « Avoir » — libellé lisible, jamais `kind` brut à l'écran. */
export function invoiceKindLabel(kind: Invoice["kind"]): string {
  return kind === "credit_note" ? "Avoir" : "Facture";
}

/** Montant d'une facture (chaîne « 42.50 » du contrat) en nombre, pour
 * `formatCurrency`. Une valeur illisible donne `null` → « — » affiché. */
export function invoiceAmount(value: string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
