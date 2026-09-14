"use client";

/**
 * Extrait de Vintiz `apps/web/src/components/pos/ReceiptPreviewCard.tsx`,
 * jetons `vz-*` → `fc-*` — impression ESC/POS, SMS et facture PDF retirés
 * (hors périmètre matériel de ce dépôt). PR3 : le bloc « Envoyer le ticket
 * par e-mail » retiré en PR2 (bouton désactivé « bientôt disponible ») est
 * livré ici — §5 ARCHITECTURE_PR3.md, `POST /pos/transactions/{id}/client`.
 */
import React, { useState } from "react";

import { api, ApiError } from "@/lib/api";
import { formatCurrency, isValidEmail } from "@/lib/format";
import type { AttachClientResponse } from "@/lib/types";

interface Props {
  /** Id de la vente — sert au rattachement client + envoi du ticket. */
  transactionId: string;
  ticketNumber: number;
  totalTtc: number;
  isCancellation?: boolean;
  receiptText: string;
  /** `shop.dpo_email` — mention RGPD (E8). Bloc affiché même si absent
   * (l'espace du droit d'accès reste alors vide plutôt que de bloquer
   * l'envoi du ticket). */
  dpoEmail?: string;
  onNewSale: () => void;
}

export default function ReceiptPreviewCard({
  transactionId,
  ticketNumber,
  totalTtc,
  isCancellation,
  receiptText,
  dpoEmail,
  onNewSale,
}: Props) {
  return (
    <div className="space-y-4">
      <div className="rounded-fc-lg bg-fc-primary-soft p-4 text-center">
        <div className="mx-auto mb-2 flex h-14 w-14 items-center justify-center rounded-full bg-fc-primary text-white">
          <svg width="28" height="28" viewBox="0 0 28 28" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M5 14l5 5 13-13" />
          </svg>
        </div>
        <div className="text-xl font-semibold text-fc-primary-deep">{isCancellation ? "Annulation enregistrée" : "Vente validée"}</div>
        <div className="mt-1 text-sm text-fc-ink-soft">
          Ticket n° {ticketNumber} · {formatCurrency(totalTtc)}
        </div>
      </div>

      {/* max-h assez haut pour un petit ticket (3 lignes d'articles) sans
          barre de défilement ; au-delà, ça défile ICI (dans la carte),
          jamais en faisant défiler toute la page — correctif (mineur)
          persona vendeuse. */}
      <pre className="max-h-96 overflow-y-auto rounded-fc-lg border border-fc-line bg-fc-bg-alt p-4 font-mono text-xs leading-tight text-fc-ink whitespace-pre-wrap" aria-label="Aperçu du ticket">
        {receiptText}
      </pre>

      {!isCancellation && <SendReceiptByEmail transactionId={transactionId} dpoEmail={dpoEmail} />}

      <button
        type="button"
        onClick={onNewSale}
        className="w-full min-h-touch rounded-fc-lg bg-fc-primary px-4 py-3 text-base font-semibold text-white hover:bg-fc-primary-deep transition-colors"
      >
        Nouveau ticket
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bloc « Envoyer le ticket par e-mail » (§5 PR3)
// ---------------------------------------------------------------------------

function SendReceiptByEmail({ transactionId, dpoEmail }: { transactionId: string; dpoEmail?: string }) {
  const [email, setEmail] = useState("");
  const [showName, setShowName] = useState(false);
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [newsletter, setNewsletter] = useState(false);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [result, setResult] = useState<AttachClientResponse | null>(null);
  const [skipped, setSkipped] = useState(false);

  const emailValid = isValidEmail(email);

  const handleSend = async (): Promise<void> => {
    if (!isValidEmail(email) || sending) return;
    setSending(true);
    setSendError(null);
    try {
      const data = await api.post<AttachClientResponse>(`/api/pos/transactions/${transactionId}/client`, {
        email: email.trim(),
        first_name: firstName.trim() || undefined,
        last_name: lastName.trim() || undefined,
        newsletter_optin: newsletter,
        send_receipt: true,
      });
      setResult(data);
    } catch (err) {
      setSendError(err instanceof ApiError ? err.detail : "Impossible d'envoyer le ticket.");
    } finally {
      setSending(false);
    }
  };

  if (skipped) {
    return <p className="text-sm text-fc-ink-mute text-center">Ticket non envoyé par e-mail.</p>;
  }

  if (result) {
    const emailFailed = result.receipt_email?.status === "failed";
    return (
      <div className="space-y-2">
        {emailFailed ? (
          <div role="alert" className="rounded-fc-lg bg-red-50 border border-red-200 p-4 space-y-2">
            <p className="text-sm font-medium text-red-700">Envoi impossible : le ticket n&apos;a pas pu être envoyé.</p>
            <button
              type="button"
              onClick={() => void handleSend()}
              disabled={sending}
              className="text-sm font-semibold text-red-700 underline disabled:opacity-50"
            >
              {sending ? "Nouvel essai…" : "Réessayer"}
            </button>
          </div>
        ) : (
          <div role="status" className="rounded-fc-lg bg-fc-primary-soft p-4 text-sm font-medium text-fc-primary-deep">
            Ticket envoyé à {result.client.email}.
          </div>
        )}
        {result.brevo?.status === "failed" && (
          <p className="text-xs text-fc-ink-mute">Inscription newsletter en attente (réessai auto).</p>
        )}
      </div>
    );
  }

  return (
    <div className="rounded-fc-lg border border-fc-line bg-fc-surface p-4 space-y-3">
      <p className="text-sm font-semibold text-fc-ink">Envoyer le ticket par e-mail</p>

      {sendError && (
        <div role="alert" className="rounded-fc bg-red-50 border border-red-200 p-2 flex items-center justify-between gap-3">
          <span className="text-sm text-red-700">Envoi impossible : {sendError}</span>
          <button type="button" onClick={() => void handleSend()} className="text-xs font-semibold text-red-700 underline flex-shrink-0">
            Réessayer
          </button>
        </div>
      )}

      <input
        type="email"
        inputMode="email"
        autoComplete="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="adresse@exemple.fr"
        aria-label="Adresse e-mail"
        className="w-full min-h-touch px-3 py-2 rounded-fc border border-fc-line bg-fc-surface text-fc-ink placeholder-fc-ink-mute focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
      />

      {!showName ? (
        <button type="button" onClick={() => setShowName(true)} className="text-xs font-medium text-fc-primary hover:underline">
          + Ajouter le nom
        </button>
      ) : (
        <div className="grid grid-cols-2 gap-2">
          <input
            type="text"
            value={firstName}
            onChange={(e) => setFirstName(e.target.value)}
            placeholder="Prénom"
            aria-label="Prénom"
            className="min-h-touch px-3 py-2 rounded-fc border border-fc-line bg-fc-surface text-fc-ink placeholder-fc-ink-mute text-sm focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
          />
          <input
            type="text"
            value={lastName}
            onChange={(e) => setLastName(e.target.value)}
            placeholder="Nom"
            aria-label="Nom"
            className="min-h-touch px-3 py-2 rounded-fc border border-fc-line bg-fc-surface text-fc-ink placeholder-fc-ink-mute text-sm focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
          />
        </div>
      )}

      <label className="flex items-start gap-2 text-sm text-fc-ink-soft">
        <input
          type="checkbox"
          checked={newsletter}
          onChange={(e) => setNewsletter(e.target.checked)}
          className="mt-0.5 h-5 w-5 flex-shrink-0 rounded border-fc-line text-fc-primary focus:ring-fc-primary"
        />
        <span>Je souhaite recevoir les actualités et événements Frip &amp; Co Street</span>
      </label>

      <p className="text-xs leading-snug text-fc-ink-mute">
        Vos coordonnées servent uniquement à vous envoyer ce ticket. La newsletter est facultative et se désinscrit en un
        clic. Responsable : Frip &amp; Co. Vos droits (accès, suppression) : {dpoEmail || "—"}.
      </p>

      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => setSkipped(true)}
          className="flex-1 min-h-touch rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-sm font-medium text-fc-ink hover:bg-fc-bg-alt"
        >
          Passer
        </button>
        <button
          type="button"
          disabled={!emailValid || sending}
          onClick={() => void handleSend()}
          className="flex-1 min-h-touch rounded-fc-lg bg-fc-primary px-4 py-3 text-sm font-semibold text-white hover:bg-fc-primary-deep disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {sending ? "Envoi…" : "Envoyer le ticket"}
        </button>
      </div>
    </div>
  );
}
