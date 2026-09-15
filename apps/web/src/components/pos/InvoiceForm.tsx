"use client";

/**
 * Facture pour un professionnel (PR8, J6) — formulaire d'émission et
 * encart du document émis.
 *
 * Deux états, un seul composant :
 *   1. saisie — raison sociale, SIRET (validé en direct, affiché
 *      « 123 456 789 00012 »), n° de TVA facultatif, adresse ;
 *   2. émis — n° de facture affiché en gros + « Télécharger le PDF »
 *      (lib/download.ts : jeton posé, jamais un lien direct vers l'API).
 *
 * Le client professionnel n'est PAS une fiche cliente (J5) : rien n'est
 * enregistré dans la base clients, les coordonnées saisies ici ne vivent
 * que sur la facture.
 *
 * Vocabulaire d'écran sans jargon (CDC §3.2) : « Facture pro », « SIRET »,
 * « n° de TVA » — jamais « B2B » ni un code d'erreur technique. Les
 * erreurs métier s'affichent telles que l'API les rend (`detail`).
 */
import React, { useState } from "react";

import { ApiError } from "@/lib/api";
import { formatCurrency, formatDateTime } from "@/lib/format";
import {
  buildInvoicePayload,
  downloadInvoicePdf,
  invoiceAmount,
  invoiceKindLabel,
  issueInvoice,
} from "@/lib/invoices";
import { formatSiret, normalizeSiret, normalizeVatNumber, siretError, validateSiret, validateVatNumber, vatSirenMismatch } from "@/lib/siret";
import type { Invoice } from "@/lib/types";

const FIELD_CLASS =
  "w-full min-h-touch px-3 py-2 rounded-fc border bg-fc-surface text-fc-ink placeholder-fc-ink-mute focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary";
const LABEL_CLASS = "block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5";

interface FormState {
  company_name: string;
  siret: string;
  vat_number: string;
  address_line1: string;
  address_line2: string;
  postal_code: string;
  city: string;
}

const EMPTY_FORM: FormState = {
  company_name: "",
  siret: "",
  vat_number: "",
  address_line1: "",
  address_line2: "",
  postal_code: "",
  city: "",
};

interface Props {
  /** Vente à facturer. */
  transactionId: string;
  /** Numéro de ticket, rappelé en tête du formulaire. */
  transactionNumber?: number;
  /** Facture déjà émise : le composant s'ouvre directement sur l'encart du
   * document (n° + PDF), sans formulaire. */
  invoice?: Invoice | null;
  /** Remonte la facture émise au parent (mise à jour du détail du ticket,
   * fermeture de la modale…). */
  onIssued?: (invoice: Invoice) => void;
  /** Bouton secondaire de la barre d'action (« Annuler » / « Fermer »). */
  onCancel?: () => void;
  cancelLabel?: string;
}

export default function InvoiceForm({
  transactionId,
  transactionNumber,
  invoice: existingInvoice = null,
  onIssued,
  onCancel,
  cancelLabel = "Annuler",
}: Props) {
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [issued, setIssued] = useState<Invoice | null>(existingInvoice);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** Vrai dès la première tentative d'envoi : les erreurs de champ ne
   * s'affichent pas tant que la personne n'a rien tapé ni tenté d'émettre
   * (pas de formulaire rouge à l'ouverture). */
  const [attempted, setAttempted] = useState(false);

  const set = (field: keyof FormState) => (e: React.ChangeEvent<HTMLInputElement>) => {
    const raw = e.target.value;
    setForm((f) => ({
      ...f,
      // Le SIRET est stocké en chiffres seuls et RÉAFFICHÉ formaté : la
      // personne tape 14 chiffres d'affilée, l'écran montre les groupes.
      [field]: field === "siret" ? normalizeSiret(raw) : field === "vat_number" ? normalizeVatNumber(raw) : raw,
    }));
    setError(null);
  };

  const siretMessage = siretError(form.siret);
  const vatValid = validateVatNumber(form.vat_number);
  const sirenWarning = vatSirenMismatch(form.siret, form.vat_number);

  const missing: (keyof FormState)[] = (["company_name", "address_line1", "postal_code", "city"] as const).filter(
    (f) => form[f].trim().length === 0,
  );
  const canSubmit = missing.length === 0 && validateSiret(form.siret) && vatValid && !submitting;

  const handleSubmit = async (e?: React.FormEvent): Promise<void> => {
    e?.preventDefault();
    setAttempted(true);
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await issueInvoice(transactionId, buildInvoicePayload(form));
      setIssued(created);
      onIssued?.(created);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Impossible d'émettre la facture.");
    } finally {
      setSubmitting(false);
    }
  };

  if (issued) {
    return (
      <div className="space-y-3">
        <InvoiceSummary invoice={issued} highlight />
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="w-full min-h-touch rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-sm font-medium text-fc-ink hover:bg-fc-bg-alt"
          >
            Fermer
          </button>
        )}
      </div>
    );
  }

  return (
    <form className="space-y-3" onSubmit={(e) => void handleSubmit(e)} noValidate>
      <p className="text-sm text-fc-ink-soft">
        {transactionNumber !== undefined
          ? `Facture sur le ticket n° ${transactionNumber}. `
          : ""}
        Renseignez la société : la facture est émise une seule fois, avec un numéro suivi.
      </p>

      {error && (
        <div role="alert" className="rounded-fc bg-fc-danger-soft border border-fc-danger/30 p-2.5 text-sm text-fc-danger">
          {error}
        </div>
      )}

      <label className="block">
        <span className={LABEL_CLASS}>Raison sociale</span>
        <input
          type="text"
          value={form.company_name}
          onChange={set("company_name")}
          maxLength={120}
          autoComplete="organization"
          placeholder="Nom de la société"
          aria-label="Raison sociale"
          aria-invalid={attempted && form.company_name.trim().length === 0}
          className={`${FIELD_CLASS} ${attempted && form.company_name.trim().length === 0 ? "border-fc-danger" : "border-fc-line"}`}
        />
        {attempted && form.company_name.trim().length === 0 && (
          <span className="mt-1 block text-xs text-fc-danger">La raison sociale est obligatoire.</span>
        )}
      </label>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className={LABEL_CLASS}>SIRET (14 chiffres)</span>
          <input
            type="text"
            inputMode="numeric"
            value={formatSiret(form.siret)}
            onChange={set("siret")}
            placeholder="123 456 789 00012"
            aria-label="SIRET"
            aria-describedby="invoice-siret-help"
            aria-invalid={!!siretMessage || (attempted && !validateSiret(form.siret))}
            className={`${FIELD_CLASS} font-mono tabular-nums ${
              siretMessage || (attempted && !validateSiret(form.siret)) ? "border-fc-danger" : "border-fc-line"
            }`}
          />
          <span id="invoice-siret-help" className="mt-1 block text-xs">
            {siretMessage ? (
              <span className="text-fc-danger">{siretMessage}</span>
            ) : validateSiret(form.siret) ? (
              <span className="text-fc-success">SIRET valide.</span>
            ) : attempted ? (
              <span className="text-fc-danger">Le SIRET est obligatoire.</span>
            ) : (
              <span className="text-fc-ink-mute">Sur le tampon ou la carte de visite de la société.</span>
            )}
          </span>
        </label>

        <label className="block">
          <span className={LABEL_CLASS}>N° de TVA (facultatif)</span>
          <input
            type="text"
            value={form.vat_number}
            onChange={set("vat_number")}
            placeholder="FR40123456789"
            aria-label="Numéro de TVA"
            aria-invalid={!vatValid}
            className={`${FIELD_CLASS} font-mono ${!vatValid ? "border-fc-danger" : "border-fc-line"}`}
          />
          <span className="mt-1 block text-xs">
            {!vatValid ? (
              <span className="text-fc-danger">Format attendu : FR suivi de 11 caractères (ex. FR40123456789).</span>
            ) : sirenWarning ? (
              <span className="text-fc-warn">{sirenWarning}</span>
            ) : (
              <span className="text-fc-ink-mute">À laisser vide si la société n&apos;en donne pas.</span>
            )}
          </span>
        </label>
      </div>

      <label className="block">
        <span className={LABEL_CLASS}>Adresse</span>
        <input
          type="text"
          value={form.address_line1}
          onChange={set("address_line1")}
          maxLength={120}
          autoComplete="address-line1"
          placeholder="12 rue des Lilas"
          aria-label="Adresse"
          aria-invalid={attempted && form.address_line1.trim().length === 0}
          className={`${FIELD_CLASS} ${attempted && form.address_line1.trim().length === 0 ? "border-fc-danger" : "border-fc-line"}`}
        />
        {attempted && form.address_line1.trim().length === 0 && (
          <span className="mt-1 block text-xs text-fc-danger">L&apos;adresse est obligatoire.</span>
        )}
      </label>

      <label className="block">
        <span className={LABEL_CLASS}>Complément d&apos;adresse (facultatif)</span>
        <input
          type="text"
          value={form.address_line2}
          onChange={set("address_line2")}
          maxLength={120}
          autoComplete="address-line2"
          placeholder="Bâtiment, étage…"
          aria-label="Complément d'adresse"
          className={`${FIELD_CLASS} border-fc-line`}
        />
      </label>

      <div className="grid gap-3 sm:grid-cols-[140px_1fr]">
        <label className="block">
          <span className={LABEL_CLASS}>Code postal</span>
          <input
            type="text"
            inputMode="numeric"
            value={form.postal_code}
            onChange={set("postal_code")}
            maxLength={10}
            autoComplete="postal-code"
            placeholder="76000"
            aria-label="Code postal"
            aria-invalid={attempted && form.postal_code.trim().length === 0}
            className={`${FIELD_CLASS} font-mono tabular-nums ${
              attempted && form.postal_code.trim().length === 0 ? "border-fc-danger" : "border-fc-line"
            }`}
          />
        </label>
        <label className="block">
          <span className={LABEL_CLASS}>Ville</span>
          <input
            type="text"
            value={form.city}
            onChange={set("city")}
            maxLength={80}
            autoComplete="address-level2"
            placeholder="Rouen"
            aria-label="Ville"
            aria-invalid={attempted && form.city.trim().length === 0}
            className={`${FIELD_CLASS} ${attempted && form.city.trim().length === 0 ? "border-fc-danger" : "border-fc-line"}`}
          />
        </label>
      </div>
      {attempted && (form.postal_code.trim().length === 0 || form.city.trim().length === 0) && (
        <p className="text-xs text-fc-danger">Le code postal et la ville sont obligatoires.</p>
      )}

      <div className="flex gap-2 pt-1">
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            disabled={submitting}
            className="flex-1 min-h-touch rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-sm font-medium text-fc-ink hover:bg-fc-bg-alt disabled:opacity-50"
          >
            {cancelLabel}
          </button>
        )}
        <button
          type="submit"
          disabled={!canSubmit}
          className="flex-1 min-h-touch rounded-fc-lg bg-fc-primary px-4 py-3 text-sm font-semibold text-white hover:bg-fc-primary-deep disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {submitting ? "Émission…" : "Émettre la facture"}
        </button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Encart d'un document émis (facture ou avoir) — réutilisé dans le détail
// d'un ticket (« Tickets du jour ») et sur l'écran de fin de vente.
// ---------------------------------------------------------------------------

export function InvoiceSummary({
  invoice,
  highlight = false,
  compact = false,
}: {
  invoice: Invoice;
  /** Fond teinté + message de confirmation : juste après l'émission. */
  highlight?: boolean;
  /** Une seule ligne (n° + bouton) : pour le détail d'un ticket. */
  compact?: boolean;
}) {
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDownload = async (): Promise<void> => {
    if (downloading) return;
    setDownloading(true);
    setError(null);
    try {
      await downloadInvoicePdf(invoice);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Échec du téléchargement du PDF.");
    } finally {
      setDownloading(false);
    }
  };

  const label = invoiceKindLabel(invoice.kind);
  const ttc = invoiceAmount(invoice.total_ttc);

  return (
    <div
      className={`rounded-fc-lg p-3 space-y-2 ${
        highlight ? "bg-fc-primary-soft" : "border border-fc-line bg-fc-surface"
      }`}
    >
      {highlight && (
        <p role="status" className="text-sm font-semibold text-fc-primary-deep">
          {invoice.kind === "credit_note" ? "Avoir émis." : "Facture émise."}
        </p>
      )}
      <p className="text-sm text-fc-ink">
        <span className="font-medium">{label} :</span>{" "}
        <span className="font-mono font-semibold tabular-nums">{invoice.invoice_number}</span>
      </p>
      {!compact && (
        <div className="text-xs text-fc-ink-soft space-y-0.5">
          <p>{invoice.company_name}</p>
          <p className="font-mono tabular-nums">SIRET {formatSiret(invoice.siret)}</p>
          <p>
            {invoice.address_line1}
            {invoice.address_line2 ? `, ${invoice.address_line2}` : ""} — {invoice.postal_code} {invoice.city}
          </p>
          <p>
            {formatDateTime(invoice.issued_at)} · Total TTC{" "}
            <span className="font-mono tabular-nums">{formatCurrency(ttc)}</span>
          </p>
        </div>
      )}
      {error && (
        <div role="alert" className="rounded-fc bg-fc-danger-soft border border-fc-danger/30 p-2 text-sm text-fc-danger">
          {error}
        </div>
      )}
      <button
        type="button"
        onClick={() => void handleDownload()}
        disabled={downloading}
        className="w-full min-h-touch rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-sm font-semibold text-fc-ink hover:bg-fc-bg-alt disabled:opacity-60 disabled:cursor-not-allowed"
      >
        {downloading ? "Préparation…" : "Télécharger le PDF"}
      </button>
    </div>
  );
}
