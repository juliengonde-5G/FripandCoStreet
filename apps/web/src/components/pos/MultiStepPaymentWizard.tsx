"use client";

/**
 * Réécriture, tenant du même rôle que Vintiz
 * `apps/web/src/components/pos/MultiStepPaymentWizard.tsx` — orchestrateur
 * du paiement 3 gestes (§6 PR2) : Espèces / Carte bancaire / Mixte.
 * Sans lien de paiement, sans coupon, sans fidélité, sans avoir/chèque
 * (hors modèle `payments.method` du contrat, §2). Assure elle-même le
 * cycle CB (`initiate` → polling 1,5 s → `status`, `DELETE` pour
 * Annuler, `POST /retry` pour Réessayer) — §4.5 du contrat, en appelant
 * directement `lib/api.ts` (transparent réel/mock selon
 * NEXT_PUBLIC_MOCK_API, cf. lib/mockApi.ts).
 */
import React, { useEffect, useRef, useState } from "react";

import Modal from "@/components/ui/Modal";
import { api, ApiError } from "@/lib/api";
import { formatCurrency } from "@/lib/format";
import type { CbCheckoutStatus, CbInitiateResponse, PaymentInput } from "@/lib/types";

import NumPadModal from "./NumPadModal";
import PaymentMethodSelector, { type PosPaymentMethod } from "./PaymentMethodSelector";
import PaymentStatusBanner, { type PaymentStatus } from "./PaymentStatusBanner";

interface Tendered {
  method: "cash" | "card";
  amount: number;
  tendered_amount?: number;
  checkout_id?: string;
}

interface Props {
  open: boolean;
  /** Total à encaisser (TTC, après remise globale). */
  totalTtc: number;
  /** Identifiant d'idempotence de la vente en cours — réutilisé pour le
   * paiement carte (§6 : "réutilisé pour le CB et la vente"). */
  clientUuid: string;
  /** Carte désactivée (TPE non configuré / hors ligne), avec la raison. */
  cardDisabled?: boolean;
  cardDisabledReason?: string;
  onClose: () => void;
  /** Appelé une fois tous les tenders réunis — le parent envoie la vente. */
  onCommit: (tenders: PaymentInput[]) => Promise<void> | void;
}

type Step =
  | { kind: "select" }
  | { kind: "amount-cash"; mixed: boolean }
  | { kind: "card-pending"; amount: number; status: PaymentStatus; detail?: string }
  | { kind: "confirm" };

const POLL_MS = 1500;

export default function MultiStepPaymentWizard({
  open,
  totalTtc,
  clientUuid,
  cardDisabled,
  cardDisabledReason,
  onClose,
  onCommit,
}: Props) {
  const [tenders, setTenders] = useState<Tendered[]>([]);
  const [step, setStep] = useState<Step>({ kind: "select" });
  const [committing, setCommitting] = useState(false);
  const [commitError, setCommitError] = useState<string | null>(null);

  const checkoutIdRef = useRef<string | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);

  const stopPolling = () => {
    stoppedRef.current = true;
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  useEffect(() => stopPolling, []);

  // `amount` (montant appliqué à la vente, ce que l'API reçoit) est déjà
  // plafonné au reste dû par `handleCashConfirmed` — le rendu se calcule
  // donc sur `tendered_amount` (ce qui a été physiquement remis), qui
  // seul peut dépasser la vente.
  const totalTendered = tenders.reduce((s, t) => s + (t.tendered_amount ?? t.amount), 0);
  const collected = tenders.reduce((s, t) => s + t.amount, 0);
  const remaining = round2(Math.max(0, totalTtc - collected));
  const change = round2(Math.max(0, totalTendered - totalTtc));

  const handleClose = (): void => {
    if (committing) return;
    stopPolling();
    checkoutIdRef.current = null;
    setTenders([]);
    setStep({ kind: "select" });
    setCommitError(null);
    onClose();
  };

  // -- CB ------------------------------------------------------------------

  const pollLoop = async (amount: number): Promise<void> => {
    stoppedRef.current = false;
    while (!stoppedRef.current) {
      await sleep(POLL_MS);
      if (stoppedRef.current || !checkoutIdRef.current) return;
      try {
        const data = await api.get<CbCheckoutStatus>(`/api/pos/payments/cb/${checkoutIdRef.current}/status`);
        if (stoppedRef.current) return;
        if (data.status === "paid") {
          stoppedRef.current = true;
          // Montre le bandeau "Paiement validé" un court instant avant
          // d'enchaîner sur la confirmation — le vendeur voit le passage à
          // vert avant que l'écran change.
          setStep({ kind: "card-pending", amount, status: "paid" });
          const tender: Tendered = { method: "card", amount, checkout_id: checkoutIdRef.current };
          await sleep(700);
          setTenders((prev) => {
            const next = [...prev, tender];
            const cover = coverage(next, totalTtc);
            setStep(cover >= totalTtc - 0.001 ? { kind: "confirm" } : { kind: "select" });
            return next;
          });
          return;
        }
        if (data.status === "failed") {
          stoppedRef.current = true;
          setStep({ kind: "card-pending", amount, status: "failed", detail: data.error || "Refusé par le terminal." });
          return;
        }
        if (data.status === "cancelled") {
          stoppedRef.current = true;
          setStep({ kind: "card-pending", amount, status: "cancelled", detail: "Paiement annulé sur le terminal." });
          return;
        }
        // toujours pending — on continue à interroger
      } catch (err) {
        // Erreur réseau transitoire : on continue le polling, sans casser
        // l'écran — seule une action explicite (Annuler) arrête le cycle.
        void err;
      }
    }
  };

  const startCardCheckout = async (amount: number): Promise<void> => {
    setStep({ kind: "card-pending", amount, status: "pending" });
    try {
      const data = await api.post<CbInitiateResponse>("/api/pos/payments/cb/initiate", {
        amount,
        client_uuid: clientUuid,
      });
      checkoutIdRef.current = data.checkout_id;
      void pollLoop(amount);
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Impossible de joindre le terminal de paiement.";
      setStep({ kind: "card-pending", amount, status: "failed", detail });
    }
  };

  const handleCancelCard = async (): Promise<void> => {
    stopPolling();
    const id = checkoutIdRef.current;
    if (id) {
      try {
        await api.delete(`/api/pos/payments/cb/${id}`);
      } catch {
        /* best-effort */
      }
    }
    setStep({ kind: "select" });
  };

  const handleRetryCard = async (): Promise<void> => {
    if (step.kind !== "card-pending") return;
    const amount = step.amount;
    const id = checkoutIdRef.current;
    setStep({ kind: "card-pending", amount, status: "pending" });
    try {
      const data = id
        ? await api.post<CbInitiateResponse>(`/api/pos/payments/cb/${id}/retry`, {})
        : await api.post<CbInitiateResponse>("/api/pos/payments/cb/initiate", { amount, client_uuid: clientUuid });
      checkoutIdRef.current = data.checkout_id;
      void pollLoop(amount);
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Impossible de joindre le terminal de paiement.";
      setStep({ kind: "card-pending", amount, status: "failed", detail });
    }
  };

  // -- Méthodes --------------------------------------------------------

  const handlePickMethod = (method: PosPaymentMethod): void => {
    if (method === "card") {
      void startCardCheckout(remaining);
      return;
    }
    if (method === "mixed") {
      setStep({ kind: "amount-cash", mixed: true });
      return;
    }
    setStep({ kind: "amount-cash", mixed: false });
  };

  const handleCashConfirmed = (amount: number, mixed: boolean): void => {
    // `amount` est le montant remis par la cliente (peut dépasser le
    // reste dû en espèces, avec rendu) — le paiement envoyé à l'API ne
    // porte, lui, que la part effectivement appliquée à la vente
    // (`amount` du contrat), le surplus restant tracé via
    // `tendered_amount` pour le calcul du rendu.
    const applied = round2(Math.min(amount, remaining));
    const tender: Tendered = { method: "cash", amount: applied, tendered_amount: amount };
    const next = [...tenders, tender];
    setTenders(next);
    const cover = coverage(next, totalTtc);
    if (cover >= totalTtc - 0.001) {
      setStep({ kind: "confirm" });
      return;
    }
    if (mixed) {
      // Mixte : on enchaîne automatiquement sur la carte pour le reste.
      const rest = round2(totalTtc - cover);
      void startCardCheckout(rest);
      return;
    }
    setStep({ kind: "select" });
  };

  const handleRemoveTender = (index: number): void => {
    setTenders((prev) => prev.filter((_, i) => i !== index));
    setStep({ kind: "select" });
  };

  const handleCommit = async (): Promise<void> => {
    setCommitting(true);
    setCommitError(null);
    try {
      const payments: PaymentInput[] = tenders.map((t) =>
        t.method === "cash"
          ? { method: "cash", amount: t.amount, tendered_amount: t.tendered_amount ?? t.amount }
          : { method: "card", amount: t.amount, checkout_id: t.checkout_id },
      );
      await onCommit(payments);
      handleClose();
    } catch (err) {
      setCommitError(err instanceof ApiError ? err.detail : "Échec de l'enregistrement de la vente.");
    } finally {
      setCommitting(false);
    }
  };

  if (!open) return null;

  if (step.kind === "amount-cash") {
    return (
      <NumPadModal
        open={open}
        onClose={() => setStep({ kind: "select" })}
        method="cash"
        title="Espèces"
        remainingAmount={remaining}
        mode={step.mixed ? "partial" : "full"}
        onConfirm={(amount) => handleCashConfirmed(amount, step.mixed)}
      />
    );
  }

  return (
    <Modal
      open={open}
      onClose={handleClose}
      closeOnBackdrop={false}
      title={
        step.kind === "card-pending" && step.status === "pending"
          ? "Paiement en cours sur le terminal…"
          : step.kind === "confirm"
            ? "Confirmer la vente"
            : `Encaisser ${formatCurrency(totalTtc)}`
      }
      actions={
        <>
          <button
            type="button"
            onClick={handleClose}
            className="min-h-touch rounded-fc-lg border border-fc-line bg-fc-surface px-5 py-3 text-base font-medium text-fc-ink hover:bg-fc-bg-alt"
            disabled={committing || (step.kind === "card-pending" && step.status === "pending")}
          >
            Annuler la vente
          </button>
          {step.kind === "confirm" && (
            <button
              type="button"
              onClick={() => void handleCommit()}
              disabled={committing}
              className="min-h-touch rounded-fc-lg bg-fc-primary px-5 py-3 text-base font-semibold text-white transition-colors hover:bg-fc-primary-deep disabled:opacity-60"
            >
              {committing ? "Enregistrement…" : "Valider la vente"}
            </button>
          )}
        </>
      }
    >
      <div className="space-y-4">
        {step.kind === "select" && (
          <PaymentMethodSelector
            remainingAmount={remaining}
            disabled={{ card: cardDisabled }}
            disabledReasons={{ card: cardDisabledReason }}
            onPick={handlePickMethod}
          />
        )}

        {step.kind === "card-pending" && (
          <div className="space-y-3">
            <PaymentStatusBanner
              status={step.status}
              detail={step.detail}
              actionLabel={step.status === "pending" ? "Annuler" : undefined}
              onAction={step.status === "pending" ? () => void handleCancelCard() : undefined}
              secondaryActionLabel={step.status === "failed" || step.status === "cancelled" ? "Réessayer" : undefined}
              onSecondaryAction={
                step.status === "failed" || step.status === "cancelled" ? () => void handleRetryCard() : undefined
              }
            />
            {(step.status === "failed" || step.status === "cancelled") && (
              <button
                type="button"
                onClick={() => setStep({ kind: "select" })}
                className="min-h-touch w-full rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-base font-medium text-fc-ink hover:bg-fc-bg-alt"
              >
                Choisir un autre moyen de paiement
              </button>
            )}
          </div>
        )}

        {step.kind === "confirm" && (
          <div className="space-y-3">
            {commitError && (
              <div role="alert" className="rounded-fc-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700">
                {commitError}
              </div>
            )}
            <div className="rounded-fc-lg border border-fc-line bg-fc-surface">
              {tenders.map((t, i) => (
                <div key={i} className="flex items-center justify-between border-b border-fc-line px-4 py-3 last:border-b-0">
                  <span className="text-sm font-medium text-fc-ink">{t.method === "cash" ? "Espèces" : "Carte bancaire"}</span>
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-base font-semibold tabular-nums text-fc-ink">{formatCurrency(t.amount)}</span>
                    {!committing && (
                      <button type="button" onClick={() => handleRemoveTender(i)} className="text-xs text-fc-danger hover:underline">
                        Retirer
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
            <div className="flex items-center justify-between rounded-fc-lg bg-fc-primary-soft px-4 py-3">
              <span className="text-sm font-medium text-fc-primary-deep">Total encaissé</span>
              <span className="font-mono text-2xl font-bold tabular-nums text-fc-primary-deep">{formatCurrency(collected)}</span>
            </div>
            {change > 0 && (
              <div className="flex flex-col items-center justify-center rounded-fc-lg bg-fc-warn-soft px-4 py-5 ring-2 ring-fc-warn shadow-sm">
                <span className="text-base font-semibold uppercase tracking-wide text-fc-ink">Monnaie à rendre</span>
                <span className="font-mono font-bold tabular-nums text-fc-ink leading-none mt-1 text-6xl md:text-7xl">
                  {formatCurrency(change)}
                </span>
              </div>
            )}
          </div>
        )}

        {tenders.length > 0 && step.kind === "select" && (
          <div className="rounded-fc-lg bg-fc-bg-alt p-3">
            <div className="mb-2 text-xs font-medium uppercase tracking-wide text-fc-ink-mute">Déjà encaissé</div>
            <ul className="space-y-1 text-sm">
              {tenders.map((t, i) => (
                <li key={i} className="flex items-center justify-between">
                  <span>{t.method === "cash" ? "Espèces" : "Carte bancaire"}</span>
                  <span className="font-mono tabular-nums">{formatCurrency(t.amount)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Modal>
  );
}

function coverage(tenders: Tendered[], total: number): number {
  let cover = 0;
  for (const t of tenders) {
    const stillDue = Math.max(0, total - cover);
    cover += Math.min(t.amount, stillDue);
  }
  return cover;
}

function round2(n: number): number {
  return Math.round((n + Number.EPSILON) * 100) / 100;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
