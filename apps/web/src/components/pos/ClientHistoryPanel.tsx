"use client";

/**
 * Panneau « Historique » d'une cliente en caisse (PR10,
 * docs/ARCHITECTURE_PR10.md §1, L7).
 *
 * Répond à une seule question, posée au comptoir, la cliente en face :
 * « elle est déjà venue ? » — et sa réponse tient en une ligne d'en-tête
 * (« Déjà venue 3 fois · dernière visite il y a 8 j · 142,00 € en tout »),
 * suivie des cinq derniers tickets avec leurs articles.
 *
 * Panneau latéral à partir de 768 px (le ticket en cours reste visible à
 * côté, on ne perd pas la vente), plein écran en dessous : sur une petite
 * tablette, une colonne de 380 px ne laisserait rien de lisible.
 *
 * Rien n'est imprimé : ce panneau ne touche ni au panier, ni au ticket.
 * Il ne sert qu'à reconnaître une habituée et à enchaîner sur la bonne
 * fiche.
 *
 * Accessibilité : `role="dialog"`, focus piégé et restauré
 * (`useDialogA11y`), Échap et « Retour » ferment.
 */
import React, { useEffect, useId, useState } from "react";

import { ApiError } from "@/lib/api";
import { fetchClientHistory, parseAmount, type ClientHistory } from "@/lib/clients";
import { formatCurrency, formatDateTime, formatRelativeTime } from "@/lib/format";
import { useDialogA11y } from "@/lib/useDialogA11y";

interface Props {
  open: boolean;
  /** Fiche à raconter. `null` quand aucune n'est choisie : le panneau ne
   * s'ouvre pas. */
  clientId: string | null;
  /** « Prénom Nom » (ou une coordonnée masquée) — affiché en sous-titre
   * pour qu'on sache de quelle fiche on parle. */
  clientName?: string;
  onClose: () => void;
}

export default function ClientHistoryPanel({ open, clientId, clientName, onClose }: Props) {
  const titleId = useId();
  const containerRef = useDialogA11y<HTMLDivElement>(open && !!clientId, onClose);

  const [history, setHistory] = useState<ClientHistory | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Une ouverture = une lecture. Changer de fiche pendant le chargement
  // annule la requête précédente : une réponse lente ne peut pas raconter
  // l'histoire d'une autre cliente.
  useEffect(() => {
    if (!open || !clientId) {
      setHistory(null);
      setError(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setHistory(null);
    setError(null);
    setLoading(true);
    fetchClientHistory(clientId, 5, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setHistory(data);
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setError(err instanceof ApiError ? err.detail : "Historique indisponible.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [open, clientId]);

  if (!open || !clientId) return null;

  const visits = history?.visits_count ?? 0;
  const totalSpent = parseAmount(history?.total_spent);

  return (
    <div
      ref={containerRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-[80] flex bg-fc-bg md:justify-end md:bg-fc-ink/30"
    >
      <div className="flex h-full w-full flex-col bg-fc-bg md:max-w-md md:border-l md:border-fc-line md:shadow-xl">
        <header className="flex h-14 flex-shrink-0 items-center gap-3 bg-fc-primary px-2 text-white sm:px-3">
          <button
            type="button"
            onClick={onClose}
            className="inline-flex min-h-touch flex-shrink-0 items-center rounded-fc px-2 text-sm font-semibold text-white transition-colors hover:bg-white/15"
          >
            <span aria-hidden className="mr-1.5 text-base leading-none">
              ←
            </span>
            Retour
          </button>
          <div className="min-w-0 flex-1 text-center">
            <h2 id={titleId} className="truncate text-sm font-semibold">
              Historique
            </h2>
            {clientName && <p className="truncate text-xs text-white/80">{clientName}</p>}
          </div>
          {/* Contrepoids du bouton « Retour » : garde le titre centré. */}
          <span aria-hidden className="w-20 flex-shrink-0" />
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {loading ? (
            <p className="py-6 text-center text-sm text-fc-ink-soft">Chargement de l&apos;historique…</p>
          ) : error ? (
            <div role="alert" className="rounded-fc-lg border border-fc-danger/30 bg-fc-danger-soft p-3 text-sm text-fc-danger">
              {error}
            </div>
          ) : !history || visits === 0 || history.transactions.length === 0 ? (
            <div className="rounded-fc-lg border border-fc-line bg-fc-surface p-6 text-center">
              <p className="text-sm font-semibold text-fc-ink">Première visite</p>
              <p className="mt-1 text-sm text-fc-ink-soft">
                Aucun achat rattaché à cette fiche pour le moment.
              </p>
            </div>
          ) : (
            <>
              <p className="rounded-fc-lg border border-fc-line bg-fc-surface p-3 text-sm font-medium text-fc-ink">
                {`Déjà venue ${visits} fois`}
                {history.last_visit_at ? ` · dernière visite ${formatRelativeTime(history.last_visit_at)}` : ""}
                {totalSpent !== null ? ` · ${formatCurrency(totalSpent)} en tout` : ""}
              </p>

              <ul className="mt-3 space-y-2">
                {history.transactions.map((tx) => {
                  const amount = parseAmount(tx.total_ttc);
                  // Le serveur liste 5 articles au maximum, puis une ligne
                  // « … » ; on la retire et on dit combien d'articles
                  // manquent, ce qui est plus parlant qu'un simple point
                  // de suspension au comptoir.
                  const shownItems = tx.items.filter((item) => item.label !== "…");
                  const hiddenItems = Math.max(0, tx.items_count - shownItems.length);
                  return (
                    <li
                      key={tx.id}
                      className="rounded-fc-lg border border-fc-line bg-fc-surface p-3"
                    >
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="min-w-0 truncate text-sm font-semibold text-fc-ink">
                          Ticket n° {tx.transaction_number}
                        </span>
                        <span
                          className={`flex-shrink-0 text-sm font-semibold tabular-nums ${
                            tx.refunded ? "text-fc-ink-mute line-through" : "text-fc-ink"
                          }`}
                        >
                          {formatCurrency(amount)}
                        </span>
                      </div>
                      <p className="mt-0.5 text-xs text-fc-ink-mute">{formatDateTime(tx.created_at)}</p>
                      {tx.refunded && (
                        <p className="mt-1 inline-block rounded-fc bg-fc-danger-soft px-2 py-0.5 text-xs font-semibold text-fc-danger">
                          Annulé
                        </p>
                      )}
                      <ul className="mt-2 space-y-0.5">
                        {shownItems.map((item, index) => (
                          <li key={`${tx.id}-${index}`} className="flex justify-between gap-2 text-xs text-fc-ink-soft">
                            <span className="min-w-0 truncate">
                              {item.quantity > 1 ? `${item.quantity} × ` : ""}
                              {item.label}
                            </span>
                            <span className="flex-shrink-0 tabular-nums">
                              {formatCurrency(parseAmount(item.unit_price))}
                            </span>
                          </li>
                        ))}
                        {hiddenItems > 0 && (
                          <li className="text-xs text-fc-ink-mute">
                            … et {hiddenItems} autre{hiddenItems > 1 ? "s" : ""} article
                            {hiddenItems > 1 ? "s" : ""}
                          </li>
                        )}
                      </ul>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
