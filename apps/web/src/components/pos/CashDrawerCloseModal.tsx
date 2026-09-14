"use client";

/**
 * Extrait de Vintiz `apps/web/src/components/pos/CashDrawerCloseModal.tsx`,
 * jetons `vz-*` → `fc-*`, mentions techniques (NF525, chemins internes)
 * retirées (CDC §3.2 : aucun jargon visible). Assistant 3 phases : décompte
 * → comparaison → confirmation (le Z est affiché : attendu / compté /
 * écart, totaux par méthode, n° de Z).
 */
import React, { useState } from "react";

import NumPad from "@/components/ui/NumPad";
import { formatCurrency } from "@/lib/format";

import DenominationGrid, { type DenominationLine, totalFromBreakdown } from "./DenominationGrid";

interface Props {
  open: boolean;
  onClose: () => void;
  expectedAmount: number;
  defaultAllowedDiscrepancy?: number;
  onSubmit: (payload: {
    closing_amount: number;
    closing_breakdown: DenominationLine[] | null;
    note: string | null;
  }) => Promise<{ report_number: number } | void>;
}

type Phase = "count" | "compare" | "done";

export default function CashDrawerCloseModal({
  open,
  onClose,
  expectedAmount,
  defaultAllowedDiscrepancy = 2,
  onSubmit,
}: Props) {
  const [phase, setPhase] = useState<Phase>("count");
  const [detailMode, setDetailMode] = useState<boolean>(true);
  const [breakdown, setBreakdown] = useState<DenominationLine[]>([]);
  const [quickAmount, setQuickAmount] = useState<number>(0);
  const [closingNote, setClosingNote] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [zNumber, setZNumber] = useState<number | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const counted = detailMode ? totalFromBreakdown(breakdown) : quickAmount;
  const discrepancy = counted - expectedAmount;
  const overTolerance = Math.abs(discrepancy) > defaultAllowedDiscrepancy;
  const noteRequired = overTolerance;

  const handleClose = (): void => {
    if (submitting) return;
    setPhase("count");
    setBreakdown([]);
    setQuickAmount(0);
    setClosingNote("");
    setZNumber(null);
    setSubmitError(null);
    onClose();
  };

  const handleNext = (): void => {
    if (phase === "count") setPhase("compare");
    else if (phase === "compare") void handleSubmit();
  };

  const handleSubmit = async (): Promise<void> => {
    if (noteRequired && !closingNote.trim()) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const result = await onSubmit({
        closing_amount: counted,
        closing_breakdown: detailMode ? breakdown : null,
        note: closingNote.trim() || null,
      });
      if (result && "report_number" in result) setZNumber(result.report_number);
      setPhase("done");
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Erreur inconnue");
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;

  const phaseLabel = phase === "count" ? "Décompte" : phase === "compare" ? "Comparaison" : "Caisse clôturée";

  return (
    <div className="fixed inset-0 z-[58] bg-fc-bg flex flex-col">
      <header className="flex-shrink-0 h-14 bg-fc-primary-deep text-white flex items-center px-3 gap-3 shadow-lg">
        <button
          onClick={() => {
            if (submitting) return;
            if (phase === "compare") setPhase("count");
            else handleClose();
          }}
          disabled={submitting || phase === "done"}
          className="flex items-center gap-2 px-3 py-2 rounded-fc hover:bg-white/10 transition-colors min-h-[44px] disabled:opacity-30"
          aria-label={phase === "compare" ? "Modifier le décompte" : "Annuler"}
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="19" y1="12" x2="5" y2="12" />
            <polyline points="12 19 5 12 12 5" />
          </svg>
          <span className="text-sm font-medium">{phase === "compare" ? "Modifier" : "Retour"}</span>
        </button>
        <div className="h-7 w-px bg-white/15" />
        <h1 className="text-lg font-semibold">Clôture de caisse</h1>
        <span className="text-xs opacity-70 px-2 py-1 rounded bg-white/10">{phaseLabel}</span>
        <div className="flex-1" />
        <div className="flex flex-col items-end leading-tight">
          <span className="text-[10px] opacity-70 uppercase tracking-wider">Compté</span>
          <span className="text-2xl font-bold font-mono">{formatCurrency(counted)}</span>
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden">
        {phase === "count" && (
          <>
            <div className="hidden md:flex w-[360px] xl:w-[400px] flex-col bg-fc-surface border-r border-fc-line p-5 gap-4">
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

              <section className="mt-auto space-y-2">
                <div className="p-3 bg-fc-bg-alt rounded-fc-lg flex items-center justify-between">
                  <span className="text-xs uppercase tracking-wider text-fc-ink-mute">Attendu</span>
                  <span className="font-mono text-base font-semibold text-fc-ink tabular-nums">{formatCurrency(expectedAmount)}</span>
                </div>
                <div className="p-3 bg-fc-primary-soft rounded-fc-lg flex items-center justify-between">
                  <span className="text-xs uppercase tracking-wider text-fc-primary-deep">Compté</span>
                  <span className="font-mono text-2xl font-bold text-fc-primary-deep tabular-nums">{formatCurrency(counted)}</span>
                </div>
                {counted > 0 && (
                  <div className={`p-3 rounded-fc-lg flex items-center justify-between ${overTolerance ? "bg-fc-warn-soft" : "bg-green-50"}`}>
                    <span className={`text-xs uppercase tracking-wider ${overTolerance ? "text-fc-warn" : "text-green-700"}`}>Écart</span>
                    <span className={`font-mono text-base font-bold tabular-nums ${overTolerance ? "text-fc-warn" : "text-green-700"}`}>
                      {discrepancy > 0 ? "+" : ""}
                      {formatCurrency(discrepancy)}
                    </span>
                  </div>
                )}
              </section>
            </div>

            <div className="flex-1 overflow-y-auto bg-fc-bg p-5">
              {detailMode ? (
                <DenominationGrid value={breakdown} onChange={setBreakdown} />
              ) : (
                <div className="max-w-md mx-auto">
                  <NumPad value={quickAmount} onChange={setQuickAmount} />
                </div>
              )}
            </div>
          </>
        )}

        {phase === "compare" && (
          <div className="flex-1 overflow-y-auto p-5 md:p-8 bg-fc-bg">
            <div className="max-w-2xl mx-auto space-y-4">
              <section className="bg-fc-surface rounded-fc-lg border border-fc-line p-5">
                <h2 className="text-lg font-semibold text-fc-ink mb-4">Comparaison</h2>
                <div className="grid gap-2">
                  <SummaryRow label="Attendu en caisse" value={expectedAmount} muted />
                  <SummaryRow label="Compté" value={counted} />
                  <SummaryRow label="Écart" value={discrepancy} signed tone={overTolerance ? "alert" : "ok"} />
                </div>
              </section>

              <section className="bg-fc-surface rounded-fc border border-fc-line p-3 text-xs text-fc-ink-soft">
                Tolérance autorisée :{" "}
                <span className="font-mono font-semibold text-fc-ink">{formatCurrency(defaultAllowedDiscrepancy)}</span>
              </section>

              {overTolerance && (
                <section className="rounded-fc-lg bg-fc-warn-soft border border-fc-warn/40 p-4">
                  <div className="mb-1 flex items-center gap-2 text-sm font-semibold text-fc-ink">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="text-fc-warn">
                      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                      <line x1="12" y1="9" x2="12" y2="13" />
                      <line x1="12" y1="17" x2="12.01" y2="17" />
                    </svg>
                    Écart hors tolérance
                  </div>
                  <p className="text-xs text-fc-ink-soft">Un commentaire est obligatoire pour valider la clôture.</p>
                </section>
              )}

              {submitError && (
                <section role="alert" className="rounded-fc-lg bg-red-50 border border-red-200 p-4">
                  <div className="mb-1 flex items-center gap-2 text-sm font-semibold text-red-700">
                    <span aria-hidden>⚠</span>
                    Échec de la clôture
                  </div>
                  <p className="text-xs text-red-700">{submitError}</p>
                </section>
              )}

              <section className="bg-fc-surface rounded-fc border border-fc-line p-4">
                <label className="block">
                  <span className="mb-1.5 block text-sm font-medium text-fc-ink">
                    Commentaire de clôture
                    {noteRequired && <span className="ml-1 text-fc-warn">*</span>}
                  </span>
                  <textarea
                    value={closingNote}
                    onChange={(e) => setClosingNote(e.target.value)}
                    rows={4}
                    placeholder="Précise la cause de l'écart si nécessaire."
                    className="w-full rounded-fc border border-fc-line bg-fc-surface px-3 py-2.5 text-sm text-fc-ink focus:border-fc-primary focus:outline-none focus:ring-2 focus:ring-fc-primary"
                  />
                </label>
              </section>
            </div>
          </div>
        )}

        {phase === "done" && (
          <div className="flex-1 flex items-center justify-center bg-fc-bg p-6">
            <div className="max-w-lg w-full bg-fc-surface rounded-fc-lg border border-fc-line shadow-lg p-8 text-center">
              <div className="mx-auto w-20 h-20 rounded-full bg-fc-primary-soft flex items-center justify-center mb-5">
                <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="text-fc-primary-deep">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </div>
              <h2 className="text-2xl font-semibold text-fc-ink mb-2">Caisse clôturée</h2>
              <p className="text-sm text-fc-ink-soft mb-4">
                {zNumber !== null ? `Rapport Z n° ${zNumber} généré.` : "Rapport Z généré."}
              </p>
              <div className="grid gap-2 text-left">
                <SummaryRow label="Attendu" value={expectedAmount} muted />
                <SummaryRow label="Compté" value={counted} />
                <SummaryRow label="Écart" value={discrepancy} signed tone={overTolerance ? "alert" : "ok"} />
              </div>
            </div>
          </div>
        )}
      </div>

      <footer className="flex-shrink-0 bg-fc-surface border-t border-fc-line px-4 md:px-6 py-3 flex items-center gap-3 shadow-[0_-2px_8px_rgba(0,0,0,0.04)]">
        {phase !== "done" ? (
          <>
            <button
              type="button"
              onClick={() => {
                if (phase === "compare") setPhase("count");
                else handleClose();
              }}
              disabled={submitting}
              className="px-5 py-3 rounded-fc-lg text-sm font-medium text-fc-ink-soft bg-fc-bg-alt hover:bg-fc-line transition-colors min-h-[52px] disabled:opacity-50"
            >
              {phase === "compare" ? "Modifier le décompte" : "Annuler"}
            </button>
            <button
              type="button"
              disabled={submitting || counted <= 0 || (phase === "compare" && noteRequired && !closingNote.trim())}
              onClick={handleNext}
              className={`flex-1 py-3 rounded-fc-lg text-lg font-bold transition-colors min-h-[52px] flex items-center justify-center gap-3 ${
                submitting || counted <= 0 || (phase === "compare" && noteRequired && !closingNote.trim())
                  ? "bg-fc-line text-fc-ink-mute cursor-not-allowed"
                  : "bg-fc-primary text-white hover:bg-fc-primary-deep active:bg-fc-primary-deep shadow-lg"
              }`}
            >
              {submitting ? (
                <>
                  <div className="w-5 h-5 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  Clôture…
                </>
              ) : phase === "count" ? (
                "Continuer"
              ) : (
                <>Clôturer — {formatCurrency(counted)}</>
              )}
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={handleClose}
            className="flex-1 py-3 rounded-fc-lg text-lg font-bold bg-fc-primary text-white hover:bg-fc-primary-deep transition-colors min-h-[52px] shadow-lg"
          >
            Fermer
          </button>
        )}
      </footer>
    </div>
  );
}

interface SummaryRowProps {
  label: string;
  value: number;
  signed?: boolean;
  muted?: boolean;
  tone?: "ok" | "alert" | "neutral";
}

function SummaryRow({ label, value, signed, muted, tone = "neutral" }: SummaryRowProps) {
  const toneClass = tone === "alert" ? "text-fc-warn" : tone === "ok" ? "text-fc-primary-deep" : "text-fc-ink";
  const sign = signed && value > 0 ? "+" : "";
  return (
    <div className={`flex items-center justify-between rounded-fc-lg px-4 py-3 ${muted ? "bg-fc-bg-alt" : "bg-fc-surface border border-fc-line"}`}>
      <span className="text-sm font-medium text-fc-ink-soft">{label}</span>
      <span className={`font-mono text-xl font-bold tabular-nums ${toneClass}`}>
        {sign}
        {formatCurrency(value)}
      </span>
    </div>
  );
}
