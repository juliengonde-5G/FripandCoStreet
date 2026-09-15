"use client";

/**
 * Extrait de Vintiz `apps/web/src/components/pos/ReceiptPreviewCard.tsx`,
 * jetons `vz-*` → `fc-*` — impression ESC/POS, SMS et facture PDF retirés
 * (hors périmètre matériel de ce dépôt). PR3 : le bloc « Envoyer le ticket
 * par e-mail » retiré en PR2 (bouton désactivé « bientôt disponible ») est
 * livré ici — §5 ARCHITECTURE_PR3.md, `POST /pos/transactions/{id}/client`.
 *
 * Correctif persona vendeuse (tablette 1024×768) : deux colonnes plutôt
 * qu'une pile verticale unique — gauche l'aperçu du ticket (défilement
 * interne limité à cette colonne), droite le bloc e-mail + « Nouveau
 * ticket » (toujours visible, jamais en bas d'un contenu qui déborde).
 * Avec un ticket de 3 lignes, tient sans le moindre défilement — voir
 * …/scratchpad/front-pr3-fix/ (captures + mesure `scrollHeight`).
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
   * (un renvoi générique « demandez en boutique » remplace alors
   * l'adresse — jamais un tiret nu). */
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
    <div className="grid h-full min-h-0 gap-4 md:grid-cols-[300px_1fr]">
      {/* Colonne gauche : confirmation + aperçu du ticket */}
      <div className="flex min-h-0 flex-col gap-3">
        <div className="flex-shrink-0 rounded-fc-lg bg-fc-primary-soft p-3 text-center">
          <div className="mx-auto mb-1.5 flex h-10 w-10 items-center justify-center rounded-full bg-fc-primary text-white">
            <svg width="20" height="20" viewBox="0 0 28 28" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M5 14l5 5 13-13" />
            </svg>
          </div>
          <div className="text-base font-semibold text-fc-primary-deep">{isCancellation ? "Annulation enregistrée" : "Vente validée"}</div>
          <div className="mt-0.5 text-xs text-fc-ink-soft">
            Ticket n° {ticketNumber} · {formatCurrency(totalTtc)}
          </div>
        </div>

        {/* Défilement interne réservé à CETTE colonne (ticket long) —
            n'affecte jamais la visibilité du bloc e-mail / bouton à
            droite, ni la hauteur de la page. */}
        <pre
          className="min-h-0 flex-1 overflow-y-auto rounded-fc-lg border border-fc-line bg-fc-bg-alt p-3 font-mono text-[11px] leading-tight text-fc-ink whitespace-pre-wrap"
          aria-label="Aperçu du ticket"
        >
          {receiptText}
        </pre>
      </div>

      {/* Colonne droite : e-mail (si vente) + « Nouveau ticket » — ce
          dernier reste toujours visible (flex-shrink-0, hors de la zone
          défilante), jamais d'écran mort après « Passer »/l'envoi. */}
      <div className="flex min-h-0 flex-col gap-3">
        <div className="min-h-0 flex-1 overflow-y-auto">
          {!isCancellation && (
            <SendReceiptByEmail transactionId={transactionId} dpoEmail={dpoEmail} onSkip={onNewSale} />
          )}
        </div>
        <button
          type="button"
          onClick={onNewSale}
          className="w-full min-h-touch flex-shrink-0 rounded-fc-lg bg-fc-primary px-4 py-3 text-base font-semibold text-white hover:bg-fc-primary-deep transition-colors"
        >
          Nouveau ticket
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bloc « Envoyer le ticket par e-mail » (§5 PR3)
// ---------------------------------------------------------------------------

function SendReceiptByEmail({
  transactionId,
  dpoEmail,
  onSkip,
}: {
  transactionId: string;
  dpoEmail?: string;
  onSkip: () => void;
}) {
  const [email, setEmail] = useState("");
  const [showName, setShowName] = useState(false);
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [newsletter, setNewsletter] = useState(false);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [result, setResult] = useState<AttachClientResponse | null>(null);

  const emailTouched = email.length > 0;
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

  if (result) {
    const emailFailed = result.receipt_email?.status === "failed";
    return (
      <div className="space-y-2">
        {emailFailed ? (
          <div role="alert" className="rounded-fc-lg bg-red-50 border border-red-200 p-3 space-y-2">
            <p className="text-sm font-medium text-red-700">Envoi impossible : le ticket n&apos;a pas pu être envoyé.</p>
            <button
              type="button"
              onClick={() => void handleSend()}
              disabled={sending}
              className="text-sm font-semibold text-red-700 underline disabled:opacity-50 disabled:no-underline"
            >
              {sending ? "Nouvel essai…" : "Réessayer"}
            </button>
          </div>
        ) : (
          <div role="status" className="rounded-fc-lg bg-fc-primary-soft p-3 text-sm font-medium text-fc-primary-deep">
            Ticket envoyé à {result.client.email}.
          </div>
        )}
        {/* Uniquement si la case newsletter était cochée (E2 : sans elle,
            `brevo` est toujours `null`, jamais poussé). */}
        {newsletter && result.brevo?.status === "failed" && (
          <p className="text-xs text-fc-ink-mute">Inscription à la newsletter en attente.</p>
        )}
      </div>
    );
  }

  return (
    <div className="rounded-fc-lg border border-fc-line bg-fc-surface p-3 space-y-2.5">
      <p className="text-sm font-semibold text-fc-ink">Envoyer le ticket par e-mail</p>

      {sendError && (
        <div role="alert" className="rounded-fc bg-red-50 border border-red-200 p-2 flex items-center justify-between gap-3">
          <span className="text-sm text-red-700">Envoi impossible : {sendError}</span>
          <button
            type="button"
            onClick={() => void handleSend()}
            disabled={sending}
            className="text-xs font-semibold text-red-700 underline flex-shrink-0 disabled:opacity-50 disabled:no-underline"
          >
            {sending ? "Envoi…" : "Réessayer"}
          </button>
        </div>
      )}

      <div>
        <input
          type="email"
          inputMode="email"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="adresse@exemple.fr"
          aria-label="Adresse e-mail"
          aria-invalid={emailTouched && !emailValid}
          className="w-full min-h-touch px-3 py-2 rounded-fc border border-fc-line bg-fc-surface text-fc-ink placeholder-fc-ink-mute focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
        />
        {emailTouched && !emailValid && <p className="mt-1 text-xs text-fc-danger">Adresse e-mail incomplète</p>}
      </div>

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
        clic. Responsable : Frip &amp; Co.{" "}
        {dpoEmail
          ? `Vos droits (accès, suppression) : ${dpoEmail}.`
          : "Vos droits (accès, suppression) : demandez en boutique."}
      </p>

      <div className="flex gap-2">
        <button
          type="button"
          onClick={onSkip}
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
