"use client";

/**
 * Panneau « Tickets du jour » (§6 PR2) — écrit pour ce dépôt (pas
 * d'équivalent direct réutilisable côté Vintiz sans le catalogue/fidélité).
 * Liste du jour → détail → « Annuler ce ticket » avec motif obligatoire.
 *
 * Correctifs testeur/persona vendeuse : badge « Annulé » dans la liste ;
 * détail d'une vente déjà annulée → mention + pas de formulaire ; détail
 * d'une annulation → référence le ticket d'origine ; confirmation explicite
 * après annulation (« Remettez X € en espèces… » / « … renvoyé sur la
 * carte par le terminal »).
 */
import React, { useEffect, useState } from "react";

import Modal from "@/components/ui/Modal";
import { api, ApiError } from "@/lib/api";
import { formatCurrency, formatDateTime, isValidEmail, maskEmail } from "@/lib/format";
import type { SendReceiptEmailResponse, TransactionOut, TransactionSummary } from "@/lib/types";

interface Props {
  open: boolean;
  onClose: () => void;
  /** Le parent rafraîchit l'état de caisse après une annulation. */
  onCancelled: () => void;
}

export default function TicketsPanel({ open, onClose, onCancelled }: Props) {
  const [list, setList] = useState<TransactionSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<TransactionOut | null>(null);
  const [reason, setReason] = useState("");
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  /** Résultat (la transaction `refund`) juste après une annulation réussie
   * — affiché avant de revenir à la liste, avec les consignes de
   * remboursement par moyen de paiement. */
  const [cancelledResult, setCancelledResult] = useState<TransactionOut | null>(null);

  // PR3 — renvoi du ticket par e-mail depuis le détail.
  const [emailDraft, setEmailDraft] = useState("");
  const [emailSending, setEmailSending] = useState(false);
  const [emailError, setEmailError] = useState<string | null>(null);
  const [emailSentTo, setEmailSentTo] = useState<string | null>(null);

  const loadList = async (): Promise<TransactionSummary[]> => {
    const data = await api.get<{ transactions: TransactionSummary[] }>("/api/pos/transactions");
    setList(data.transactions);
    return data.transactions;
  };

  useEffect(() => {
    if (!open) return;
    setDetail(null);
    setReason("");
    setCancelError(null);
    setCancelledResult(null);
    setEmailDraft("");
    setEmailError(null);
    setEmailSentTo(null);
    setLoading(true);
    setError(null);
    loadList()
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger les tickets du jour."))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  /** Numéro de ticket lisible à partir d'un id, résolu depuis la liste du
   * jour déjà chargée (vente et son annulation sont toujours le même
   * jour) — évite une requête réseau supplémentaire rien que pour un
   * numéro d'affichage. */
  const numberFor = (id: string | null | undefined): number | null => {
    if (!id) return null;
    return list.find((t) => t.id === id)?.transaction_number ?? null;
  };

  const openDetail = async (id: string): Promise<void> => {
    setCancelError(null);
    setReason("");
    setEmailError(null);
    setEmailSentTo(null);
    try {
      const tx = await api.get<TransactionOut>(`/api/pos/transactions/${id}`);
      setDetail(tx);
      setEmailDraft(tx.client?.email ?? "");
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Impossible de charger ce ticket.");
    }
  };

  const handleSendEmail = async (): Promise<void> => {
    if (!detail || !isValidEmail(emailDraft) || emailSending) return;
    setEmailSending(true);
    setEmailError(null);
    try {
      await api.post<SendReceiptEmailResponse>(`/api/pos/transactions/${detail.id}/receipt/email`, {
        email: emailDraft.trim(),
      });
      setEmailSentTo(emailDraft.trim());
    } catch (err) {
      setEmailError(err instanceof ApiError ? err.detail : "Échec de l'envoi du ticket.");
    } finally {
      setEmailSending(false);
    }
  };

  const handleCancel = async (): Promise<void> => {
    if (!detail || reason.trim().length < 3) return;
    setCancelling(true);
    setCancelError(null);
    try {
      const refund = await api.post<TransactionOut>(`/api/pos/transactions/${detail.id}/cancel`, { reason: reason.trim() });
      // Rafraîchir la liste AVANT de revenir dessus, pour ne jamais montrer
      // un ticket "actif" déjà annulé, même le temps d'un aller-retour réseau.
      await loadList();
      setDetail(null);
      setCancelledResult(refund);
      onCancelled();
    } catch (err) {
      setCancelError(err instanceof ApiError ? err.detail : "Échec de l'annulation du ticket.");
    } finally {
      setCancelling(false);
    }
  };

  const backToList = (): void => {
    setCancelledResult(null);
    setDetail(null);
  };

  return (
    <Modal open={open} onClose={onClose} title="Tickets du jour" closeOnBackdrop={!cancelling}>
      {error && <div role="alert" className="mb-3 rounded-fc-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700">{error}</div>}

      {cancelledResult ? (
        <div className="space-y-4">
          <div className="rounded-fc-lg bg-fc-primary-soft p-4">
            <p className="text-base font-semibold text-fc-primary-deep mb-1">Ticket annulé</p>
            <p className="text-sm text-fc-ink-soft">
              Annulation n° {cancelledResult.transaction_number} — {formatCurrency(cancelledResult.total_ttc)}
            </p>
          </div>
          <div className="rounded-fc-lg border border-fc-line bg-fc-surface p-4 space-y-2">
            <p className="text-sm font-medium text-fc-ink">À faire au comptoir</p>
            {cancelledResult.payments.map((p, i) =>
              p.method === "cash" ? (
                <p key={i} className="text-sm text-fc-ink-soft">
                  Remettez <span className="font-mono font-semibold text-fc-ink">{formatCurrency(p.amount)}</span> en espèces à
                  la cliente.
                </p>
              ) : (
                <p key={i} className="text-sm text-fc-ink-soft">
                  Le remboursement de <span className="font-mono font-semibold text-fc-ink">{formatCurrency(p.amount)}</span> a
                  été renvoyé sur la carte par le terminal.
                </p>
              ),
            )}
          </div>
          <button
            type="button"
            onClick={backToList}
            className="w-full min-h-touch rounded-fc-lg bg-fc-primary px-4 py-3 text-base font-semibold text-white hover:bg-fc-primary-deep"
          >
            Retour à la liste
          </button>
        </div>
      ) : !detail ? (
        <div className="space-y-2">
          {loading && <p className="text-sm text-fc-ink-soft">Chargement…</p>}
          {!loading && list.length === 0 && <p className="text-sm text-fc-ink-soft">Aucun ticket aujourd&apos;hui.</p>}
          {list.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => void openDetail(t.id)}
              className="w-full min-h-touch flex items-center justify-between rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-left hover:bg-fc-bg-alt transition-colors"
            >
              <div>
                <div className="flex items-center gap-2 text-sm font-semibold text-fc-ink">
                  <span>Ticket n° {t.transaction_number}</span>
                  {t.transaction_type === "refund" && <Badge tone="warn">Annulation</Badge>}
                  {t.cancelled && <Badge tone="mute">Annulé</Badge>}
                </div>
                <div className="text-xs text-fc-ink-mute">{formatDateTime(t.created_at)}</div>
              </div>
              <span className="font-mono text-base font-semibold tabular-nums text-fc-ink">{formatCurrency(t.total_ttc)}</span>
            </button>
          ))}
        </div>
      ) : (
        <div className="space-y-4">
          <button type="button" onClick={() => setDetail(null)} className="text-sm text-fc-primary hover:underline">
            ← Retour à la liste
          </button>

          <div className="rounded-fc-lg border border-fc-line bg-fc-surface p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-base font-semibold text-fc-ink">Ticket n° {detail.transaction_number}</span>
              <span className="font-mono text-lg font-bold tabular-nums text-fc-ink">{formatCurrency(detail.total_ttc)}</span>
            </div>
            <p className="text-xs text-fc-ink-mute mb-3">{formatDateTime(detail.created_at)}</p>
            <ul className="space-y-1 text-sm text-fc-ink-soft mb-3">
              {detail.items.map((it) => (
                <li key={it.position} className="flex items-center justify-between">
                  <span>
                    {it.label} × {it.quantity}
                  </span>
                  <span className="font-mono tabular-nums">{formatCurrency(it.line_total)}</span>
                </li>
              ))}
            </ul>
            <ul className="space-y-1 text-sm text-fc-ink-soft">
              {detail.payments.map((p, i) => (
                <li key={i} className="flex items-center justify-between">
                  <span>{p.method === "cash" ? "Espèces" : "Carte bancaire"}</span>
                  <span className="font-mono tabular-nums">{formatCurrency(p.amount)}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="rounded-fc-lg border border-fc-line bg-fc-surface p-4 space-y-2">
            <p className="text-sm font-medium text-fc-ink">Ticket par e-mail</p>
            {detail.client && (
              <p className="text-xs text-fc-ink-mute">Client lié : {maskEmail(detail.client.email)}</p>
            )}
            {emailSentTo ? (
              <p className="text-sm font-medium text-fc-primary-deep">Ticket envoyé à {emailSentTo}.</p>
            ) : (
              <>
                {emailError && (
                  <div role="alert" className="rounded-fc bg-red-50 border border-red-200 p-2 flex items-center justify-between gap-3">
                    <span className="text-sm text-red-700">Envoi impossible : {emailError}</span>
                    <button type="button" onClick={() => void handleSendEmail()} className="text-xs font-semibold text-red-700 underline flex-shrink-0">
                      Réessayer
                    </button>
                  </div>
                )}
                <div className="flex gap-2">
                  <input
                    type="email"
                    inputMode="email"
                    autoComplete="email"
                    value={emailDraft}
                    onChange={(e) => setEmailDraft(e.target.value)}
                    placeholder="adresse@exemple.fr"
                    aria-label="Adresse e-mail du ticket"
                    className="flex-1 min-h-touch px-3 py-2 rounded-fc border border-fc-line bg-fc-surface text-fc-ink placeholder-fc-ink-mute text-sm focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
                  />
                  <button
                    type="button"
                    disabled={!isValidEmail(emailDraft) || emailSending}
                    onClick={() => void handleSendEmail()}
                    className="min-h-touch rounded-fc-lg bg-fc-primary px-4 text-sm font-semibold text-white hover:bg-fc-primary-deep disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {emailSending ? "Envoi…" : "Envoyer par e-mail"}
                  </button>
                </div>
              </>
            )}
          </div>

          {detail.transaction_type === "refund" && (
            <div className="rounded-fc-lg bg-fc-bg-alt p-4">
              <p className="text-sm text-fc-ink-soft">
                Annulation du ticket n°{" "}
                <span className="font-semibold text-fc-ink">{numberFor(detail.original_transaction_id) ?? "—"}</span>.
              </p>
            </div>
          )}

          {detail.transaction_type === "sale" && detail.cancelled && (
            <div className="rounded-fc-lg bg-fc-bg-alt p-4">
              <p className="text-sm text-fc-ink-soft">
                Déjà annulé — ticket n°{" "}
                <span className="font-semibold text-fc-ink">{numberFor(detail.refund_transaction_id) ?? "—"}</span>.
              </p>
            </div>
          )}

          {detail.transaction_type === "sale" && !detail.cancelled && (
            <div className="rounded-fc-lg border border-fc-line bg-fc-surface p-4 space-y-3">
              <p className="text-sm font-medium text-fc-ink">Annuler ce ticket</p>
              {cancelError && <div role="alert" className="rounded-fc bg-red-50 border border-red-200 p-2 text-sm text-red-700">{cancelError}</div>}
              <textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                rows={2}
                placeholder="Motif de l'annulation (obligatoire)"
                className="w-full rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-sm text-fc-ink focus:border-fc-primary focus:outline-none focus:ring-1 focus:ring-fc-primary"
              />
              <button
                type="button"
                disabled={reason.trim().length < 3 || cancelling}
                onClick={() => void handleCancel()}
                className="w-full min-h-touch rounded-fc-lg bg-fc-danger px-4 py-3 text-base font-semibold text-white hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {cancelling ? "Annulation…" : "Annuler ce ticket"}
              </button>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

function Badge({ tone, children }: { tone: "warn" | "mute"; children: React.ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-fc px-2 py-0.5 text-xs font-medium ${
        tone === "warn" ? "bg-fc-warn-soft text-fc-warn" : "bg-fc-bg-alt text-fc-ink-mute"
      }`}
    >
      {children}
    </span>
  );
}
