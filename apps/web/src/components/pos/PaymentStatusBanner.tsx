"use client";

/**
 * Extrait de l'application source `apps/web/src/components/pos/PaymentStatusBanner.tsx`,
 * jetons `vz-*` → `fc-*`. Statuts alignés sur le contrat §4.5 :
 * pending|paid|failed|cancelled (le "timeout" client reste possible côté
 * front — même rendu que "failed").
 */
import React from "react";

import ErrorReference from "@/components/ui/ErrorReference";

export type PaymentStatus = "pending" | "paid" | "failed" | "cancelled" | "timeout";

interface Props {
  status: PaymentStatus;
  label?: string;
  detail?: string;
  /** Référence à noter (PR12 N5) — posée seulement quand l'échec vient du
   * serveur ou du réseau, jamais sur un refus de carte. */
  reference?: string | null;
  actionLabel?: string;
  onAction?: () => void;
  secondaryActionLabel?: string;
  onSecondaryAction?: () => void;
}

const PALETTE: Record<PaymentStatus, { bg: string; text: string; ring: string; defaultLabel: string }> = {
  pending: { bg: "bg-fc-bg-alt", text: "text-fc-ink", ring: "ring-fc-line", defaultLabel: "En attente du terminal…" },
  paid: { bg: "bg-fc-primary-soft", text: "text-fc-primary-deep", ring: "ring-fc-primary", defaultLabel: "Paiement validé" },
  failed: { bg: "bg-fc-warn-soft", text: "text-fc-ink", ring: "ring-fc-warn", defaultLabel: "Paiement refusé" },
  cancelled: { bg: "bg-fc-bg-alt", text: "text-fc-ink-soft", ring: "ring-fc-line", defaultLabel: "Paiement annulé" },
  timeout: { bg: "bg-fc-warn-soft", text: "text-fc-ink", ring: "ring-fc-warn", defaultLabel: "Délai dépassé" },
};

export default function PaymentStatusBanner({
  status,
  label,
  detail,
  reference,
  actionLabel,
  onAction,
  secondaryActionLabel,
  onSecondaryAction,
}: Props) {
  const palette = PALETTE[status];
  const showSpinner = status === "pending";
  const showCheck = status === "paid";
  return (
    <div role="status" aria-live="polite" className={`rounded-fc-lg px-4 py-4 ring-1 ${palette.bg} ${palette.text} ${palette.ring}`}>
      <div className="flex items-center gap-3 flex-wrap">
        {showSpinner && (
          <span aria-hidden="true" className="inline-block h-5 w-5 animate-spin rounded-full border-2 border-fc-primary border-t-transparent" />
        )}
        {showCheck && (
          <svg aria-hidden="true" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 10l5 5 9-9" />
          </svg>
        )}
        <span className="font-semibold">{label || palette.defaultLabel}</span>
        <div className="ml-auto flex items-center gap-2">
          {secondaryActionLabel && onSecondaryAction && (
            <button
              type="button"
              onClick={onSecondaryAction}
              className="min-h-touch rounded-fc border border-current px-3 py-1.5 text-sm font-medium hover:bg-white/40"
            >
              {secondaryActionLabel}
            </button>
          )}
          {actionLabel && onAction && (
            <button
              type="button"
              onClick={onAction}
              className="min-h-touch rounded-fc border border-current px-3 py-1.5 text-sm font-medium hover:bg-white/40"
            >
              {actionLabel}
            </button>
          )}
        </div>
      </div>
      {detail && <p className="mt-1 text-sm opacity-80">{detail}</p>}
      <ErrorReference reference={reference} />
    </div>
  );
}
