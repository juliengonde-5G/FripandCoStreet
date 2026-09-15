"use client";

/**
 * Extrait de Vintiz `apps/web/src/components/pos/CashDrawerOpenModal.tsx`,
 * jetons `vz-*` → `fc-*`. Écran plein « Ouvrir la caisse » (§6 PR2) :
 * rien d'autre n'est cliquable tant que la caisse est fermée.
 */
import React, { useId, useState } from "react";

import NumPad from "@/components/ui/NumPad";
import { formatCurrency } from "@/lib/format";
import { useDialogA11y } from "@/lib/useDialogA11y";

import DenominationGrid, { type DenominationLine, totalFromBreakdown } from "./DenominationGrid";

interface Props {
  onSubmit: (payload: { opening_amount: number; opening_breakdown: DenominationLine[] | null }) => Promise<void> | void;
  error?: string | null;
}

/**
 * Toujours affiché plein écran quand la caisse est fermée — 2 colonnes :
 * mode de saisie à gauche, saisie (grille de coupures ou NumPad) à droite.
 */
export default function CashDrawerOpenModal({ onSubmit, error }: Props) {
  const [detailMode, setDetailMode] = useState<boolean>(true);
  const [breakdown, setBreakdown] = useState<DenominationLine[]>([]);
  const [quickAmount, setQuickAmount] = useState<number>(0);
  const [submitting, setSubmitting] = useState(false);
  const titleId = useId();
  // Toujours monté seul (rien d'autre n'est cliquable tant que la caisse
  // est fermée, voir /caisse) : pas de fermeture au clavier, donc pas de
  // callback ESC — seul le piège de focus + la sémantique dialog importent.
  const containerRef = useDialogA11y<HTMLDivElement>(true);

  const total = detailMode ? totalFromBreakdown(breakdown) : quickAmount;
  const valid = total > 0;

  const handleSubmit = async (): Promise<void> => {
    if (!valid) return;
    setSubmitting(true);
    try {
      await onSubmit({
        opening_amount: total,
        opening_breakdown: detailMode ? breakdown : null,
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      ref={containerRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-[58] bg-fc-bg flex flex-col"
    >
      <header className="flex-shrink-0 h-14 bg-fc-primary-deep text-white flex items-center px-4 gap-3 shadow-lg">
        <h1 id={titleId} className="text-lg font-semibold">
          Ouverture de caisse
        </h1>
        <div className="flex-1" />
        <div className="flex flex-col items-end leading-tight">
          <span className="text-[10px] opacity-70 uppercase tracking-wider">Fond de caisse</span>
          <span className="text-2xl font-bold font-mono">{formatCurrency(total)}</span>
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden flex-col md:flex-row">
        <div className="w-full md:w-[360px] xl:w-[400px] flex flex-col bg-fc-surface border-b md:border-b-0 md:border-r border-fc-line p-5 gap-4">
          <section>
            <h2 className="text-base font-semibold text-fc-ink mb-2">Mode de saisie</h2>
            <div className="flex items-center justify-between rounded-fc-lg bg-fc-bg-alt p-1">
              <button
                type="button"
                onClick={() => setDetailMode(true)}
                aria-pressed={detailMode}
                className={`flex-1 rounded-fc px-3 py-2.5 text-sm font-medium transition-colors min-h-touch ${
                  detailMode ? "bg-fc-surface text-fc-primary-deep shadow-sm" : "text-fc-ink-soft hover:text-fc-ink"
                }`}
              >
                Détail
              </button>
              <button
                type="button"
                onClick={() => setDetailMode(false)}
                aria-pressed={!detailMode}
                className={`flex-1 rounded-fc px-3 py-2.5 text-sm font-medium transition-colors min-h-touch ${
                  !detailMode ? "bg-fc-surface text-fc-primary-deep shadow-sm" : "text-fc-ink-soft hover:text-fc-ink"
                }`}
              >
                Rapide
              </button>
            </div>
          </section>

          <section className="bg-fc-bg-alt rounded-fc-lg p-3 text-sm text-fc-ink-soft">
            {detailMode
              ? "Compte le fond de caisse au démarrage, coupure par coupure."
              : "Saisis directement le total, si le fond de caisse n'a pas changé."}
          </section>

          {error && (
            <section role="alert" className="rounded-fc-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700">
              {error}
            </section>
          )}

          <section className="mt-auto p-4 bg-fc-primary-soft rounded-fc-lg text-center">
            <p className="text-xs uppercase tracking-wider text-fc-primary-deep mb-1">Total fond de caisse</p>
            <p className="font-mono text-3xl font-bold text-fc-primary-deep tabular-nums">{formatCurrency(total)}</p>
          </section>
        </div>

        <div className="flex-1 overflow-y-auto bg-fc-bg p-5">
          {detailMode ? (
            <DenominationGrid value={breakdown} onChange={setBreakdown} />
          ) : (
            <div className="max-w-md mx-auto">
              <NumPad value={quickAmount} onChange={setQuickAmount} presets={[50, 100, 150, 200]} />
            </div>
          )}
        </div>
      </div>

      <footer className="flex-shrink-0 bg-fc-surface border-t border-fc-line px-4 md:px-6 py-3 shadow-[0_-2px_8px_rgba(0,0,0,0.04)]">
        <button
          type="button"
          disabled={!valid || submitting}
          onClick={() => void handleSubmit()}
          className="w-full py-3 rounded-fc-lg text-lg font-bold transition-colors min-h-[52px] flex items-center justify-center gap-3 bg-fc-primary text-white hover:bg-fc-primary-deep active:bg-fc-primary-deep shadow-lg disabled:bg-fc-line disabled:text-fc-ink-mute disabled:cursor-not-allowed disabled:shadow-none"
        >
          {submitting ? (
            <>
              <div className="w-5 h-5 border-2 border-white border-t-transparent rounded-full animate-spin" />
              Ouverture…
            </>
          ) : (
            <>Ouvrir la caisse — {formatCurrency(total)}</>
          )}
        </button>
      </footer>
    </div>
  );
}
