"use client";

/**
 * Réécriture, tenant du même rôle que dans l'application source
 * (`apps/web/src/components/pos/MultiStepPaymentWizard.tsx`) — orchestrateur
 * du paiement 3 gestes (§6 PR2) : Espèces / Carte bancaire / Mixte.
 * Sans lien de paiement, sans coupon, sans fidélité, sans avoir/chèque
 * (hors modèle `payments.method` du contrat, §2). Assure elle-même le
 * cycle CB (`initiate` → polling 1,5 s → `status`, `DELETE` pour
 * Annuler, `POST /retry` pour Réessayer) — §4.5 du contrat, en appelant
 * directement `lib/api.ts` (transparent réel/mock selon
 * NEXT_PUBLIC_MOCK_API, cf. lib/mockApi.ts).
 *
 * PR9 (K4) : quand le terminal n'a pas répondu pour une cause récupérable,
 * le serveur met l'encaissement en file et renvoie une 409 `payment_failed`
 * portant `recoverable` et `failed_payment_id`. La caisse le dit en clair
 * (« la vente n'est pas perdue »), propose **Réessayer** (qui relance un
 * encaissement sur le terminal puis reprend le polling existant) et
 * **Autre moyen de paiement** — le panier reste intact dans les deux cas,
 * et aucune vente n'est créée tant que le paiement n'est pas accepté.
 */
import React, { useEffect, useRef, useState } from "react";

import ErrorReference from "@/components/ui/ErrorReference";
import Modal from "@/components/ui/Modal";
import { api, ApiError } from "@/lib/api";
import { describeError, errorRef, errorReference, errorText, type DisplayableError } from "@/lib/apiError";
import { formatCurrency } from "@/lib/format";
import { retryFailedPayment, type FailedPayment } from "@/lib/payments";
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
  | {
      kind: "card-pending";
      amount: number;
      status: PaymentStatus;
      detail?: string;
      /** Référence de l'échec (PR12 N5) : posée pour une panne serveur ou
       * réseau seulement, jamais pour un refus du terminal. */
      reference?: string;
      /** Échec récupérable : l'encaissement est en file, on peut le relancer. */
      recovery?: CardRecovery;
    }
  | { kind: "confirm" };

/** Ce que la caisse retient d'un encaissement carte mis en file (K3) —
 * de quoi le relancer et savoir combien de réessais restent. */
interface CardRecovery {
  failedPaymentId: FailedPayment["id"] | null;
  retryCount: FailedPayment["retry_count"] | null;
  maxRetries: FailedPayment["max_retries"] | null;
  /** Plus aucun réessai possible (409 `retries_exhausted`). */
  exhausted: boolean;
}

const POLL_MS = 1500;

const RECOVERABLE_LABEL = "Le terminal n'a pas répondu, la vente n'est pas perdue";
const EXHAUSTED_LABEL = "Réessais épuisés, choisissez un autre moyen de paiement";

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
  const [commitError, setCommitError] = useState<DisplayableError>(null);

  const checkoutIdRef = useRef<string | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);
  /** Encaissement carte mis en file pour ce panier (K3), avec son montant :
   * repris tel quel si la vendeuse revient sur la carte après avoir regardé
   * un autre moyen de paiement — le serveur refuse un nouvel `initiate`
   * tant que l'essai précédent n'a pas été relancé. */
  const queuedCardRef = useRef<(CardRecovery & { amount: number }) | null>(null);

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
    queuedCardRef.current = null;
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
          // Paiement accepté : le serveur solde la ligne de file (K3), la
          // caisse n'a plus rien à relancer pour ce panier.
          queuedCardRef.current = null;
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

  /** Construit l'écran d'échec : soit l'échec récupérable de PR9 (le
   * terminal n'a pas répondu, l'encaissement est en file), soit le refus
   * de carte historique. `previous` sert au 409 `retries_exhausted`, qui
   * ne répète pas l'identifiant de la ligne en file. */
  const failureStep = (amount: number, err: unknown, previous: CardRecovery | null): Step => {
    const detail = err instanceof ApiError ? err.detail : "Impossible de joindre le terminal de paiement.";
    const reference = errorReference(err);
    const recovery = readCardRecovery(err, previous);
    if (recovery) {
      queuedCardRef.current = recovery.failedPaymentId ? { ...recovery, amount } : null;
      return { kind: "card-pending", amount, status: "failed", detail, reference, recovery };
    }
    return { kind: "card-pending", amount, status: "failed", detail, reference };
  };

  const startCardCheckout = async (amount: number): Promise<void> => {
    // Un encaissement du même montant attend déjà dans la file : on le
    // relance au lieu d'en ouvrir un nouveau (le serveur refuserait).
    const queued = queuedCardRef.current;
    if (queued && !queued.exhausted && queued.failedPaymentId && Math.abs(queued.amount - amount) < 0.005) {
      await runCardRetry(amount, queued, () => retryFailedPayment(queued.failedPaymentId as string));
      return;
    }
    setStep({ kind: "card-pending", amount, status: "pending" });
    try {
      const data = await api.post<CbInitiateResponse>("/api/pos/payments/cb/initiate", {
        amount,
        client_uuid: clientUuid,
      });
      checkoutIdRef.current = data.checkout_id;
      void pollLoop(amount);
    } catch (err) {
      setStep(failureStep(amount, err, null));
    }
  };

  /** Relance commune : on repasse en « en attente », on appelle le
   * serveur, puis on reprend le polling existant sur le nouvel
   * encaissement. */
  const runCardRetry = async (
    amount: number,
    previous: CardRecovery | null,
    call: () => Promise<{ checkout_id: string; retry_count?: number }>,
  ): Promise<void> => {
    setStep({ kind: "card-pending", amount, status: "pending" });
    try {
      const data = await call();
      checkoutIdRef.current = data.checkout_id;
      if (previous?.failedPaymentId) {
        queuedCardRef.current = {
          ...previous,
          retryCount: data.retry_count ?? (previous.retryCount ?? 0) + 1,
          amount,
        };
      }
      void pollLoop(amount);
    } catch (err) {
      setStep(failureStep(amount, err, previous));
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
    await runCardRetry(amount, null, () =>
      id
        ? api.post<CbInitiateResponse>(`/api/pos/payments/cb/${id}/retry`, {})
        : api.post<CbInitiateResponse>("/api/pos/payments/cb/initiate", { amount, client_uuid: clientUuid }),
    );
  };

  /** PR9 (K4) : réessai d'un encaissement mis en file. */
  const handleRetryQueuedCard = async (): Promise<void> => {
    if (step.kind !== "card-pending") return;
    const recovery = step.recovery;
    const failedPaymentId = recovery?.failedPaymentId;
    if (!recovery || !failedPaymentId) return;
    await runCardRetry(step.amount, recovery, () => retryFailedPayment(failedPaymentId));
  };

  /** « Autre moyen de paiement » : retour au choix, panier intact — la
   * ligne reste en file côté serveur, rien n'est encaissé. */
  const handleOtherMethod = (): void => {
    stopPolling();
    setStep({ kind: "select" });
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
      setCommitError(describeError(err, "Échec de l'enregistrement de la vente."));
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
              label={step.recovery ? (step.recovery.exhausted ? EXHAUSTED_LABEL : RECOVERABLE_LABEL) : undefined}
              detail={step.recovery ? recoveryDetail(step.recovery, step.detail) : step.detail}
              reference={step.reference}
              actionLabel={step.status === "pending" ? "Annuler" : undefined}
              onAction={step.status === "pending" ? () => void handleCancelCard() : undefined}
              secondaryActionLabel={
                !step.recovery && (step.status === "failed" || step.status === "cancelled") ? "Réessayer" : undefined
              }
              onSecondaryAction={
                !step.recovery && (step.status === "failed" || step.status === "cancelled")
                  ? () => void handleRetryCard()
                  : undefined
              }
            />
            {step.recovery ? (
              // Échec récupérable (K4) : deux issues, jamais de panier perdu.
              <div
                className={
                  canRetryQueued(step.recovery) ? "grid gap-2 sm:grid-cols-2" : "grid gap-2"
                }
              >
                {canRetryQueued(step.recovery) && (
                  <button
                    type="button"
                    onClick={() => void handleRetryQueuedCard()}
                    className="min-h-touch w-full rounded-fc-lg bg-fc-primary px-4 py-3 text-base font-semibold text-white transition-colors hover:bg-fc-primary-deep"
                  >
                    Réessayer
                  </button>
                )}
                <button
                  type="button"
                  onClick={handleOtherMethod}
                  className="min-h-touch w-full rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-base font-medium text-fc-ink hover:bg-fc-bg-alt"
                >
                  Autre moyen de paiement
                </button>
              </div>
            ) : (
              (step.status === "failed" || step.status === "cancelled") && (
                <button
                  type="button"
                  onClick={handleOtherMethod}
                  className="min-h-touch w-full rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-base font-medium text-fc-ink hover:bg-fc-bg-alt"
                >
                  Choisir un autre moyen de paiement
                </button>
              )
            )}
          </div>
        )}

        {step.kind === "confirm" && (
          <div className="space-y-3">
            {errorText(commitError) && (
              <div role="alert" className="rounded-fc-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700">
                {errorText(commitError)}
                <ErrorReference reference={errorRef(commitError)} />
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

/** Un réessai n'est proposé que si la file en accepte encore un. */
function canRetryQueued(recovery: CardRecovery): boolean {
  return !recovery.exhausted && Boolean(recovery.failedPaymentId);
}

/** Lit les champs de reprise d'une 409 : `recoverable` + `failed_payment_id`
 * sur `payment_failed`, ou le code `retries_exhausted` (qui ne répète pas
 * l'identifiant — on garde alors celui de l'échec précédent). Toute autre
 * erreur, dont un refus de carte, rend `null` : comportement inchangé. */
function readCardRecovery(err: unknown, previous: CardRecovery | null): CardRecovery | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const body = err.body && typeof err.body === "object" ? (err.body as Record<string, unknown>) : {};
  const bodyId = typeof body.failed_payment_id === "string" && body.failed_payment_id ? body.failed_payment_id : null;
  const failedPaymentId = bodyId ?? previous?.failedPaymentId ?? null;
  const retryCount = readNumber(body.retry_count) ?? previous?.retryCount ?? null;
  const maxRetries = readNumber(body.max_retries) ?? previous?.maxRetries ?? null;
  if (err.code === "retries_exhausted") {
    return { failedPaymentId, retryCount, maxRetries, exhausted: true };
  }
  if (err.code === "payment_failed" && body.recoverable === true && failedPaymentId) {
    return { failedPaymentId, retryCount, maxRetries, exhausted: false };
  }
  return null;
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Message sous le bandeau : la cause rédigée par le serveur, puis le
 * compte des réessais déjà faits quand le serveur le donne. */
function recoveryDetail(recovery: CardRecovery, detail?: string): string | undefined {
  const parts: string[] = [];
  if (detail) parts.push(detail);
  if (!recovery.exhausted && recovery.retryCount && recovery.maxRetries) {
    parts.push(`Réessais déjà faits : ${recovery.retryCount} sur ${recovery.maxRetries}.`);
  }
  return parts.length > 0 ? parts.join(" ") : undefined;
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
