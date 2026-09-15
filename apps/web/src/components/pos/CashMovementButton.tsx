"use client";

/**
 * Extrait de l'application source `apps/web/src/components/pos/CashMovementButton.tsx`,
 * jetons `vz-*` → `fc-*`. Motif `personal_withdrawal` retiré (§2 modèle de
 * données PR2 : `reason` enum réduit à `bank_deposit|supplier_payment|
 * float_top_up|other`).
 */
import React, { useState } from "react";

import Modal from "@/components/ui/Modal";
import NumPad from "@/components/ui/NumPad";
import { formatCurrency } from "@/lib/format";
import type { CashMovementDirection, CashMovementReason } from "@/lib/types";

const REASONS_OUT: { id: CashMovementReason; label: string }[] = [
  { id: "bank_deposit", label: "Dépôt banque" },
  { id: "supplier_payment", label: "Paiement fournisseur" },
  { id: "other", label: "Autre sortie" },
];

const REASONS_IN: { id: CashMovementReason; label: string }[] = [
  { id: "float_top_up", label: "Réappro. fond de caisse" },
  { id: "other", label: "Autre entrée" },
];

interface Props {
  disabled?: boolean;
  /** Habillage du déclencheur — la barre haute de la caisse est sombre
   * depuis PR7 (I2) et le menu « ⋯ » affiche l'action en pleine largeur. */
  className?: string;
  /** Libellé du déclencheur — raccourci sur tablette 1024 px (PR7, I2). */
  label?: React.ReactNode;
  onSubmit: (payload: {
    direction: CashMovementDirection;
    amount: number;
    reason: CashMovementReason;
    note: string | null;
  }) => Promise<void> | void;
}

/**
 * Bouton « Mouvement de caisse » — entrée/sortie d'espèces en cours de
 * journée (dépôt banque, paiement fournisseur, réappro. fond de caisse…).
 */
export default function CashMovementButton({ disabled, className, label, onSubmit }: Props) {
  const [open, setOpen] = useState(false);
  const [direction, setDirection] = useState<CashMovementDirection>("out");
  const [amount, setAmount] = useState<number>(0);
  const [reason, setReason] = useState<CashMovementReason>("bank_deposit");
  const [note, setNote] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);

  const reasons = direction === "in" ? REASONS_IN : REASONS_OUT;
  const noteRequired = reason === "other";
  const valid = amount > 0 && (!noteRequired || note.trim().length > 0);

  const handleClose = (): void => {
    if (submitting) return;
    setOpen(false);
    setAmount(0);
    setNote("");
    setDirection("out");
    setReason("bank_deposit");
  };

  const handleSubmit = async (): Promise<void> => {
    if (!valid) return;
    setSubmitting(true);
    try {
      await onSubmit({ direction, amount, reason, note: note.trim() || null });
      handleClose();
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen(true)}
        title={disabled ? "Ouvre la caisse pour saisir un mouvement." : undefined}
        className={
          className ??
          "min-h-touch rounded-fc border border-fc-line bg-fc-surface px-4 py-2 text-sm font-medium text-fc-ink hover:bg-fc-bg-alt disabled:opacity-50"
        }
      >
        {label ?? "Mouvement de caisse"}
      </button>

      <Modal
        open={open}
        onClose={handleClose}
        title="Mouvement de caisse"
        closeOnBackdrop={false}
        actions={
          <>
            <button
              type="button"
              onClick={handleClose}
              disabled={submitting}
              className="min-h-touch rounded-fc-lg border border-fc-line bg-fc-surface px-5 py-3 text-base font-medium text-fc-ink hover:bg-fc-bg-alt disabled:opacity-60"
            >
              Annuler
            </button>
            <button
              type="button"
              disabled={!valid || submitting}
              onClick={() => void handleSubmit()}
              className="min-h-touch rounded-fc-lg bg-fc-primary px-5 py-3 text-base font-semibold text-white transition-colors hover:bg-fc-primary-deep disabled:opacity-60"
            >
              {submitting ? "Enregistrement…" : "Enregistrer"}
            </button>
          </>
        }
      >
        <div className="space-y-4">
          <div className="flex items-center justify-between rounded-fc-lg bg-fc-bg-alt p-1">
            {(["out", "in"] as CashMovementDirection[]).map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => {
                  setDirection(d);
                  setReason(d === "in" ? "float_top_up" : "bank_deposit");
                }}
                aria-pressed={direction === d}
                className={`flex-1 rounded-fc px-3 py-2 text-sm font-medium transition-colors min-h-touch ${
                  direction === d ? "bg-fc-surface text-fc-primary-deep shadow-sm" : "text-fc-ink-soft hover:text-fc-ink"
                }`}
              >
                {d === "out" ? "Sortie" : "Entrée"}
              </button>
            ))}
          </div>

          <fieldset>
            <legend className="mb-2 text-xs font-medium uppercase tracking-wide text-fc-ink-mute">Motif</legend>
            <div className="grid grid-cols-2 gap-2">
              {reasons.map((r) => (
                <button
                  key={r.id}
                  type="button"
                  onClick={() => setReason(r.id)}
                  aria-pressed={reason === r.id}
                  className={`min-h-touch rounded-fc-lg border px-3 py-2 text-sm font-medium transition-colors ${
                    reason === r.id
                      ? "border-fc-primary bg-fc-primary-soft text-fc-primary-deep"
                      : "border-fc-line bg-fc-surface text-fc-ink hover:bg-fc-bg-alt"
                  }`}
                >
                  {r.label}
                </button>
              ))}
            </div>
          </fieldset>

          <div>
            <span className="mb-2 block text-xs font-medium uppercase tracking-wide text-fc-ink-mute">Montant</span>
            <NumPad value={amount} onChange={setAmount} presets={[20, 50, 100, 200]} />
            <div className="mt-2 rounded-fc-lg bg-fc-primary-soft px-4 py-2 text-right">
              <span className="font-mono text-xl font-bold tabular-nums text-fc-primary-deep">
                {direction === "out" ? "-" : "+"}
                {formatCurrency(amount)}
              </span>
            </div>
          </div>

          <label className="block">
            <span className="mb-1 block text-xs font-medium text-fc-ink-soft">
              Commentaire{noteRequired && <span className="ml-1 text-fc-warn">*</span>}
            </span>
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={2}
              placeholder="Détail libre — référence facture, raison du retrait…"
              className="w-full rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-sm text-fc-ink focus:border-fc-primary focus:outline-none focus:ring-1 focus:ring-fc-primary"
            />
          </label>
        </div>
      </Modal>
    </>
  );
}
