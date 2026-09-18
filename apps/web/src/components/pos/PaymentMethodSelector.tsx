"use client";

/**
 * Extrait de l'application source `apps/web/src/components/pos/PaymentMethodSelector.tsx`,
 * jetons `vz-*` → `fc-*`, réduit à 3 gestes (§6 PR2) : Espèces / Carte
 * bancaire / Mixte. Chèque, chèque CDC, avoir : retirés (hors périmètre
 * PR2 — pas dans le modèle `payments.method`).
 */
import React from "react";

import { formatCurrency } from "@/lib/format";

export type PosPaymentMethod = "cash" | "card" | "mixed";

interface MethodConfig {
  id: PosPaymentMethod;
  label: string;
  hint?: string;
  icon: React.ReactNode;
}

interface Props {
  methods?: PosPaymentMethod[];
  disabled?: Partial<Record<PosPaymentMethod, boolean>>;
  disabledReasons?: Partial<Record<PosPaymentMethod, string>>;
  onPick: (method: PosPaymentMethod) => void;
  remainingAmount?: number;
  remainingLabel?: string;
}

const ICON_PROPS = {
  width: 44,
  height: 44,
  viewBox: "0 0 48 48",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

const METHODS: Record<PosPaymentMethod, MethodConfig> = {
  cash: {
    id: "cash",
    label: "Espèces",
    hint: "Rendu monnaie calculé",
    icon: (
      <svg {...ICON_PROPS} aria-hidden="true">
        <rect x="4" y="14" width="40" height="20" rx="2" />
        <circle cx="24" cy="24" r="5" />
        <path d="M11 18v-1M37 30v1" />
      </svg>
    ),
  },
  card: {
    id: "card",
    label: "Carte bancaire",
    hint: "Terminal de paiement",
    icon: (
      <svg {...ICON_PROPS} aria-hidden="true">
        <rect x="4" y="10" width="40" height="28" rx="3" />
        <path d="M4 18h40" />
        <path d="M10 30h8M22 30h6" />
      </svg>
    ),
  },
  mixed: {
    id: "mixed",
    label: "Mixte",
    hint: "Espèces puis carte",
    icon: (
      <svg {...ICON_PROPS} aria-hidden="true">
        <rect x="4" y="14" width="24" height="16" rx="2" />
        <rect x="20" y="10" width="24" height="20" rx="3" />
      </svg>
    ),
  },
};

const DEFAULT_METHODS: PosPaymentMethod[] = ["cash", "card", "mixed"];

/**
 * Sélecteur de moyen de paiement — 3 grandes cartes tactiles.
 */
export default function PaymentMethodSelector({
  methods = DEFAULT_METHODS,
  disabled = {},
  disabledReasons = {},
  onPick,
  remainingAmount,
  remainingLabel = "Reste à encaisser",
}: Props) {
  return (
    <div className="space-y-4">
      {remainingAmount !== undefined && (
        <div className="flex items-center justify-between rounded-fc-lg bg-fc-primary-soft px-4 py-3">
          <span className="text-sm font-medium text-fc-primary-deep">{remainingLabel}</span>
          <span className="font-mono text-2xl font-bold tabular-nums text-fc-primary-deep">{formatCurrency(remainingAmount)}</span>
        </div>
      )}

      <div className="grid grid-cols-3 gap-3">
        {methods.map((id) => {
          const cfg = METHODS[id];
          const isDisabled = !!disabled[id];
          const reason = disabledReasons[id];
          // PR13/O6 — `min-w-0` + `overflow-hidden` sur la tuile, `break-words`
          // sur le texte : la raison affichée quand la carte est désactivée
          // (message SumUp) reste dans le cadre « Carte bancaire ».
          return (
            <button
              key={id}
              type="button"
              onClick={() => !isDisabled && onPick(id)}
              disabled={isDisabled}
              className={`flex h-[132px] min-w-0 flex-col items-center justify-center gap-2 overflow-hidden rounded-2xl border transition-all focus:outline-none focus:ring-2 focus:ring-fc-primary focus:ring-offset-2 min-h-touch ${
                isDisabled
                  ? "cursor-not-allowed border-fc-line bg-fc-bg-alt opacity-60"
                  : "border-fc-line bg-fc-bg-alt hover:bg-fc-primary-soft active:bg-fc-primary-soft active:scale-[0.98]"
              }`}
              aria-label={cfg.label}
              aria-disabled={isDisabled}
            >
              <span className={isDisabled ? "text-fc-ink-mute" : "text-fc-primary"}>{cfg.icon}</span>
              <span className="text-base font-semibold text-fc-ink">{cfg.label}</span>
              {(isDisabled ? reason : cfg.hint) && (
                <span className="min-w-0 break-words px-1 text-center text-xs text-fc-ink-mute">
                  {isDisabled ? reason : cfg.hint}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
