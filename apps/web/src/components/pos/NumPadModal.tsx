"use client";

/**
 * Extrait de l'application source `apps/web/src/components/pos/NumPadModal.tsx`, jetons
 * `vz-*` → `fc-*`, réduit aux méthodes espèces/carte. Ajout de
 * `mode: "partial"` pour la part « espèces » du paiement Mixte (§6 PR2) :
 * le montant saisi doit rester strictement inférieur au reste à encaisser
 * pour laisser une part à la carte.
 */
import React, { useEffect, useState } from "react";

import Modal from "@/components/ui/Modal";
import NumPad from "@/components/ui/NumPad";
import { formatCurrency } from "@/lib/format";

interface Props {
  open: boolean;
  onClose: () => void;
  method: "cash" | "card";
  title: string;
  /** Montant total restant à encaisser. */
  remainingAmount: number;
  /** "full" (défaut) : le montant saisi doit couvrir le reste (espèces :
   * dépassement autorisé, rendu affiché). "partial" : le montant doit
   * rester strictement inférieur au reste (paiement Mixte, part espèces). */
  mode?: "full" | "partial";
  onConfirm: (amount: number) => void;
  presets?: number[];
}

export default function NumPadModal({ open, onClose, method, title, remainingAmount, mode = "full", onConfirm, presets }: Props) {
  const [amount, setAmount] = useState<number>(0);

  useEffect(() => {
    if (open) {
      setAmount(method === "card" ? remainingAmount : 0);
    }
  }, [open, method, remainingAmount]);

  const change = method === "cash" && mode === "full" ? amount - remainingAmount : 0;
  const isCovering = mode === "full" ? amount >= remainingAmount - 0.001 : amount > 0 && amount < remainingAmount - 0.001;

  // Raccourcis sur une seule ligne (§6 PR2 : 5/10/20/50/100) — pas de
  // sixième bouton "reste dû" qui forcerait un retour à la ligne et
  // pousserait le bloc "Monnaie à rendre" hors de l'écran à 1024×768.
  const defaultPresets = mode === "full" ? [5, 10, 20, 50, 100] : [5, 10, 20, 50].filter((n) => n < remainingAmount);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`${title} — ${formatCurrency(remainingAmount)}`}
      closeOnBackdrop={false}
      actions={
        <>
          <button
            type="button"
            onClick={onClose}
            className="min-h-touch rounded-fc-lg border border-fc-line bg-fc-surface px-5 py-3 text-base font-medium text-fc-ink hover:bg-fc-bg-alt"
          >
            Annuler
          </button>
          <button
            type="button"
            disabled={!isCovering}
            onClick={() => onConfirm(amount)}
            className="min-h-touch rounded-fc-lg bg-fc-primary px-5 py-3 text-base font-semibold text-white transition-colors hover:bg-fc-primary-deep disabled:cursor-not-allowed disabled:opacity-50"
          >
            Valider
          </button>
        </>
      }
    >
      {/* Contenu compact (correctif persona vendeuse) : le bloc "Monnaie à
          rendre" doit rester entièrement visible sans défilement à
          1024×768, avant même de valider. `overflow-y-auto` reste en
          filet de sécurité (posé par Modal.tsx) si une tablette plus
          petite le nécessite malgré tout. */}
      <div className="space-y-2">
        <NumPad value={amount} onChange={setAmount} presets={presets ?? defaultPresets} compact />

        {method === "cash" && mode === "full" && amount > 0 && (
          <div
            className={`rounded-fc-lg px-3 py-2.5 transition-colors ${
              isCovering ? "bg-fc-primary-soft ring-2 ring-fc-primary/40 shadow-sm" : "bg-fc-warn-soft"
            }`}
          >
            <div className={isCovering ? "flex flex-col items-center text-center" : "flex items-center justify-between"}>
              <span
                className={
                  isCovering
                    ? "text-sm font-semibold uppercase tracking-wide text-fc-primary-deep"
                    : "text-sm font-medium uppercase tracking-wide text-fc-ink-soft"
                }
              >
                {isCovering ? "Monnaie à rendre" : "Manque à encaisser"}
              </span>
              <span
                className={
                  isCovering
                    ? "font-mono font-bold tabular-nums leading-none text-3xl md:text-4xl text-fc-primary-deep mt-0.5"
                    : "font-mono font-bold tabular-nums leading-none text-xl text-fc-ink"
                }
              >
                {formatCurrency(Math.abs(change))}
              </span>
            </div>
          </div>
        )}

        {mode === "partial" && (
          <p className="rounded-fc-lg bg-fc-bg-alt px-3 py-2 text-sm text-fc-ink-soft">
            Ce montant doit rester inférieur au total : le reste sera demandé à la carte bancaire.
          </p>
        )}

        {method !== "cash" && mode === "full" && amount > 0 && !isCovering && (
          <p className="rounded-fc-lg bg-fc-warn-soft px-3 py-2 text-sm text-fc-ink-soft">
            Le montant saisi est inférieur au reste à encaisser.
          </p>
        )}
      </div>
    </Modal>
  );
}
