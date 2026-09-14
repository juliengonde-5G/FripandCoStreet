"use client";

/**
 * Panneau « Tickets du jour » (§6 PR2) — écrit pour ce dépôt (pas
 * d'équivalent direct réutilisable côté Vintiz sans le catalogue/fidélité).
 * Liste du jour → détail → « Annuler ce ticket » avec motif obligatoire.
 */
import React, { useEffect, useState } from "react";

import Modal from "@/components/ui/Modal";
import { api, ApiError } from "@/lib/api";
import { formatCurrency, formatDateTime } from "@/lib/format";
import type { TransactionOut, TransactionSummary } from "@/lib/types";

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

  useEffect(() => {
    if (!open) return;
    setDetail(null);
    setReason("");
    setCancelError(null);
    setLoading(true);
    setError(null);
    api
      .get<{ transactions: TransactionSummary[] }>("/api/pos/transactions")
      .then((data) => setList(data.transactions))
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger les tickets du jour."))
      .finally(() => setLoading(false));
  }, [open]);

  const openDetail = async (id: string): Promise<void> => {
    setCancelError(null);
    setReason("");
    try {
      const tx = await api.get<TransactionOut>(`/api/pos/transactions/${id}`);
      setDetail(tx);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Impossible de charger ce ticket.");
    }
  };

  const handleCancel = async (): Promise<void> => {
    if (!detail || reason.trim().length < 3) return;
    setCancelling(true);
    setCancelError(null);
    try {
      await api.post(`/api/pos/transactions/${detail.id}/cancel`, { reason: reason.trim() });
      // Rafraîchir la liste AVANT de revenir dessus, pour ne jamais montrer
      // un ticket "actif" déjà annulé, même le temps d'un aller-retour réseau.
      const data = await api.get<{ transactions: TransactionSummary[] }>("/api/pos/transactions");
      setList(data.transactions);
      setDetail(null);
      onCancelled();
    } catch (err) {
      setCancelError(err instanceof ApiError ? err.detail : "Échec de l'annulation du ticket.");
    } finally {
      setCancelling(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Tickets du jour" closeOnBackdrop={!cancelling}>
      {error && <div role="alert" className="mb-3 rounded-fc-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700">{error}</div>}

      {!detail ? (
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
                <div className="text-sm font-semibold text-fc-ink">
                  Ticket n° {t.transaction_number}
                  {t.transaction_type === "refund" && <span className="ml-2 text-xs text-fc-warn">Annulation</span>}
                  {t.cancelled && <span className="ml-2 text-xs text-fc-ink-mute">(annulé)</span>}
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

          {detail.transaction_type === "sale" && (
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
