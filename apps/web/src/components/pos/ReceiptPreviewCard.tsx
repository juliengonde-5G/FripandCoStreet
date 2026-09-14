"use client";

/**
 * Extrait de Vintiz `apps/web/src/components/pos/ReceiptPreviewCard.tsx`,
 * jetons `vz-*` → `fc-*`, réduit : impression ESC/POS, renvoi email/SMS et
 * facture PDF retirés (hors périmètre matériel/PR3 de ce dépôt — voir
 * commentaire « emplacement prévu » ci-dessous, §6 : "L'envoi e-mail
 * arrive en PR3").
 */
import React from "react";

import { formatCurrency } from "@/lib/format";

interface Props {
  ticketNumber: number;
  totalTtc: number;
  isCancellation?: boolean;
  receiptText: string;
  onNewSale: () => void;
}

export default function ReceiptPreviewCard({ ticketNumber, totalTtc, isCancellation, receiptText, onNewSale }: Props) {
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

      <button
        type="button"
        disabled
        title="Envoi du ticket par e-mail — disponible prochainement."
        className="w-full min-h-touch rounded-fc-lg border border-dashed border-fc-line bg-fc-bg-alt px-4 py-3 text-base font-medium text-fc-ink-mute cursor-not-allowed"
      >
        Envoyer par e-mail — bientôt disponible
      </button>

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
