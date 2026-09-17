"use client";

/**
 * Onglet Paiements CB (PR9, docs/ARCHITECTURE_PR9.md §1, K4 côté
 * administration).
 *
 * Trois cartes, dans l'ordre où l'on s'en sert quand une carte a échoué :
 * ce qui reste à encaisser (la file), ce qui échoue le plus souvent
 * (l'analyse), puis le détail technique des échanges avec le terminal
 * (le journal, lu seulement quand on cherche une panne).
 *
 * Vocabulaire du comptoir : « terminal », « échange », « cause », jamais
 * le vocabulaire des développeurs — le contenu brut d'un échange
 * s'appelle « détail » à l'écran.
 *
 * Deux gestes sont irréversibles et demandent donc une confirmation : vider
 * le journal, et renoncer à un encaissement en file (motif obligatoire, il
 * part au journal des événements).
 *
 * On ne relance PAS un encaissement depuis ici : seule la caisse a le
 * panier, suit le terminal et écrit la vente une fois le paiement accepté.
 * Un réessai lancé depuis l'administration ferait payer la cliente sans que
 * l'application le voie. La reprise se fait donc en caisse, en resélectionnant
 * Carte pour le même montant — l'encaissement en file y repart tout seul.
 */
import React, { useCallback, useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import Input from "@/components/ui/Input";
import ErrorNotice from "@/components/ui/ErrorNotice";
import { describeError, type DisplayableError, type ErrorDetails } from "@/lib/apiError";
import { formatCurrency, formatDateTime, formatRelativeTime } from "@/lib/format";
import {
  ABANDON_REASON_MAX_LENGTH,
  ERROR_TYPE_LABELS,
  OPERATION_LABELS,
  abandonFailedPayment,
  fetchFailedPayments,
  fetchPaymentFailures,
  fetchSumupExchanges,
  purgeSumupExchanges,
  type FailedPayment,
  type FailedPaymentErrorType,
  type PaymentFailuresReport,
  type SumupErrorType,
  type SumupExchange,
  type SumupExchangeFilters,
  type SumupOperation,
} from "@/lib/payments";

// ---------------------------------------------------------------------------
// Éléments communs
// ---------------------------------------------------------------------------

function causeLabel(errorType: FailedPaymentErrorType | SumupErrorType | null): string {
  if (!errorType) return "Cause inconnue";
  return ERROR_TYPE_LABELS[errorType] ?? errorType;
}

function operationLabel(operation: SumupOperation): string {
  return OPERATION_LABELS[operation] ?? operation;
}

/** Erreur d'appel → message affichable, sans jamais « [object Object] »,
 * et sa référence quand la panne vient du serveur ou du réseau (N5). */
function messageOf(err: unknown, fallback: string): ErrorDetails {
  return describeError(err, fallback);
}

function StatLine({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-fc-lg border border-fc-line p-3">
      <div className="text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft">{label}</div>
      <div className="mt-1 font-mono text-sm text-fc-ink tabular-nums">{value}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Onglet
// ---------------------------------------------------------------------------

export default function PaymentsTab() {
  return (
    <div className="space-y-6">
      <PendingFailuresCard />
      <FailuresAnalysisCard />
      <ExchangeLogCard />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Carte 1 — Échecs en attente
// ---------------------------------------------------------------------------

function PendingFailuresCard() {
  const [list, setList] = useState<FailedPayment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<DisplayableError>(null);

  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<DisplayableError>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [abandonId, setAbandonId] = useState<string | null>(null);
  const [abandonReason, setAbandonReason] = useState("");

  const load = useCallback((): void => {
    setError(null);
    fetchFailedPayments("pending")
      .then(setList)
      .catch((err) => setError(messageOf(err, "Impossible de charger les encaissements en attente.")))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  const handleAbandon = async (payment: FailedPayment): Promise<void> => {
    const reason = abandonReason.trim();
    if (!reason) {
      setActionError("Indiquez un motif avant de renoncer à cet encaissement.");
      return;
    }
    setBusyId(payment.id);
    setActionError(null);
    setNotice(null);
    try {
      await abandonFailedPayment(payment.id, reason);
      setAbandonId(null);
      setAbandonReason("");
      setNotice("Encaissement abandonné.");
      load();
    } catch (err) {
      setActionError(messageOf(err, "L'abandon n'a pas pu être enregistré."));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Card
      title="Échecs en attente"
      subtitle="Encaissements carte qui n'ont pas abouti et que la caisse peut encore reprendre."
      action={
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          Actualiser
        </Button>
      }
    >
      <div className="space-y-4">
        <ErrorNotice message={error} />
        <ErrorNotice message={actionError} />
        {notice && !actionError && <p className="text-sm text-fc-success font-medium">{notice}</p>}

        {loading ? (
          <p className="text-sm text-fc-ink-soft">Chargement…</p>
        ) : list.length === 0 ? (
          <p className="text-sm text-fc-ink-soft">Aucun encaissement en attente.</p>
        ) : (
          <ul className="divide-y divide-fc-line">
            {list.map((payment) => (
              <li key={payment.id} className="py-3 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="font-mono text-base font-semibold text-fc-ink tabular-nums">
                      {formatCurrency(payment.amount)}
                    </div>
                    <div className="mt-0.5 text-sm text-fc-ink-soft" title={formatDateTime(payment.created_at)}>
                      {formatRelativeTime(payment.created_at)} — {formatDateTime(payment.created_at)}
                    </div>
                    <div className="mt-1 text-sm text-fc-ink">
                      Cause : {causeLabel(payment.error_type)}
                      {payment.last_error && (
                        <span className="text-fc-ink-mute"> — {payment.last_error}</span>
                      )}
                    </div>
                    <div className="mt-1 text-xs text-fc-ink-mute font-mono tabular-nums">
                      Réessais : {payment.retry_count}/{payment.max_retries}
                    </div>
                    <div className="mt-1 text-xs text-fc-ink-mute">
                      À reprendre en caisse : sélectionner Carte pour le même montant.
                    </div>
                  </div>

                  <div className="flex flex-wrap gap-2">
                    {abandonId !== payment.id && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setAbandonId(payment.id);
                          setAbandonReason("");
                          setActionError(null);
                        }}
                      >
                        Abandonner
                      </Button>
                    )}
                  </div>
                </div>

                {abandonId === payment.id && (
                  <div className="mt-3 rounded-fc-lg border border-fc-line bg-fc-bg p-3 space-y-3">
                    <p className="text-sm text-fc-ink">
                      Renoncer à cet encaissement de {formatCurrency(payment.amount)} ? Il ne sera plus proposé en
                      caisse. Indiquez le motif, il est conservé au journal des événements.
                    </p>
                    <Input
                      label="Motif"
                      value={abandonReason}
                      maxLength={ABANDON_REASON_MAX_LENGTH}
                      onChange={(e) => setAbandonReason(e.target.value)}
                      placeholder="La cliente a payé en espèces"
                    />
                    <div className="flex flex-wrap gap-2">
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => void handleAbandon(payment)}
                        disabled={busyId === payment.id || !abandonReason.trim()}
                        aria-busy={busyId === payment.id}
                      >
                        {busyId === payment.id ? "Abandon…" : "Confirmer l'abandon"}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setAbandonId(null);
                          setAbandonReason("");
                        }}
                        disabled={busyId === payment.id}
                      >
                        Annuler
                      </Button>
                    </div>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Carte 2 — Analyse des échecs
// ---------------------------------------------------------------------------

const PERIODS = [7, 30] as const;

function FailuresAnalysisCard() {
  const [days, setDays] = useState<number>(PERIODS[0]);
  const [report, setReport] = useState<PaymentFailuresReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<DisplayableError>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchPaymentFailures(days)
      .then(setReport)
      .catch((err) => setError(messageOf(err, "Impossible de charger l'analyse des échecs.")))
      .finally(() => setLoading(false));
  }, [days]);

  const byErrorType = Object.entries(report?.exchanges_by_error_type ?? {}).filter(([, n]) => Number(n) > 0);

  return (
    <Card
      title="Analyse des échecs"
      subtitle="Ce qui a échoué sur la période, et pourquoi."
      action={
        <div className="flex items-center gap-1 rounded-fc bg-fc-bg-alt p-1" role="group" aria-label="Période analysée">
          {PERIODS.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setDays(p)}
              aria-pressed={days === p}
              className={`min-h-[40px] rounded-fc px-3 text-sm font-medium transition-colors ${
                days === p ? "bg-fc-surface text-fc-ink shadow-sm" : "text-fc-ink-soft hover:text-fc-ink"
              }`}
            >
              {p} jours
            </button>
          ))}
        </div>
      }
    >
      <div className="space-y-5">
        <ErrorNotice message={error} />
        {loading ? (
          <p className="text-sm text-fc-ink-soft">Chargement…</p>
        ) : (
          report && (
            <>
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <StatLine label="Encaissements acceptés" value={report.attempts.paid} />
                <StatLine label="Encaissements échoués" value={report.attempts.failed} />
                <StatLine label="En cours" value={report.attempts.pending} />
                <StatLine label="Annulés" value={report.attempts.cancelled} />
              </div>

              <div>
                <h4 className="text-sm font-semibold text-fc-ink mb-2">Réessais</h4>
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                  <StatLine label="Mis en file" value={report.retries.queued} />
                  <StatLine label="Aboutis" value={report.retries.succeeded} />
                  <StatLine label="Épuisés" value={report.retries.exhausted} />
                  <StatLine label="Abandonnés" value={report.retries.abandoned} />
                </div>
              </div>

              <div>
                <h4 className="text-sm font-semibold text-fc-ink mb-2">Causes les plus fréquentes</h4>
                {report.top_errors.length === 0 ? (
                  <p className="text-sm text-fc-ink-soft">Aucun échec sur la période.</p>
                ) : (
                  <ul className="divide-y divide-fc-line">
                    {report.top_errors.map((e) => (
                      <li key={e.error_message} className="flex items-start justify-between gap-4 py-2">
                        <span className="min-w-0 text-sm text-fc-ink break-words">{e.error_message}</span>
                        <span className="font-mono text-sm text-fc-ink-soft tabular-nums">{e.count}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div>
                <h4 className="text-sm font-semibold text-fc-ink mb-2">Répartition par type</h4>
                {byErrorType.length === 0 ? (
                  <p className="text-sm text-fc-ink-soft">Aucun échange en erreur sur la période.</p>
                ) : (
                  <ul className="flex flex-wrap gap-2">
                    {byErrorType.map(([type, count]) => (
                      <li
                        key={type}
                        className="inline-flex items-center gap-2 rounded-fc bg-fc-bg-alt px-3 py-1.5 text-sm text-fc-ink"
                      >
                        {causeLabel(type as SumupErrorType)}
                        <span className="font-mono tabular-nums text-fc-ink-soft">{String(count)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div>
                <h4 className="text-sm font-semibold text-fc-ink mb-2">Par opération</h4>
                {report.by_operation.length === 0 ? (
                  <p className="text-sm text-fc-ink-soft">Aucun échange sur la période.</p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="text-left text-fc-ink-mute uppercase text-xs tracking-wide">
                          <th className="py-2 pr-4">Opération</th>
                          <th className="py-2 pr-4">Échanges</th>
                          <th className="py-2 pr-4">Dont en erreur</th>
                        </tr>
                      </thead>
                      <tbody>
                        {report.by_operation.map((row) => (
                          <tr key={row.operation} className="border-t border-fc-line">
                            <td className="py-2 pr-4">{operationLabel(row.operation)}</td>
                            <td className="py-2 pr-4 font-mono tabular-nums">{row.count}</td>
                            <td className="py-2 pr-4 font-mono tabular-nums">{row.errors}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </>
          )
        )}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Carte 3 — Journal des échanges avec le terminal
// ---------------------------------------------------------------------------

const OPERATION_OPTIONS: SumupOperation[] = [
  "ping_reader",
  "push_to_reader",
  "checkout_status",
  "reader_checkout_status",
  "cancel_checkout",
  "terminate_reader",
  "refund",
  "get_transaction",
];

/** Contenu d'un échange, mis en forme pour la lecture. Le serveur l'a déjà
 * rédigé (ni clé, ni donnée personnelle) : on ne fait que l'indenter. */
function formatDetail(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function ExchangeLogCard() {
  const [filters, setFilters] = useState<SumupExchangeFilters>({ only_failed: false, operation: "", checkout_id: "" });
  const [exchanges, setExchanges] = useState<SumupExchange[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<DisplayableError>(null);

  const [expandedId, setExpandedId] = useState<string | null>(null);

  const [confirmPurge, setConfirmPurge] = useState(false);
  const [purging, setPurging] = useState(false);
  const [purgeError, setPurgeError] = useState<DisplayableError>(null);
  const [purgeNotice, setPurgeNotice] = useState<string | null>(null);

  const load = useCallback(
    (next: SumupExchangeFilters): void => {
      setLoading(true);
      setError(null);
      fetchSumupExchanges({ ...next, limit: 100 })
        .then((data) => {
          setExchanges(data.exchanges);
          setTotal(data.total);
        })
        .catch((err) => setError(messageOf(err, "Impossible de charger le journal des échanges.")))
        .finally(() => setLoading(false));
    },
    [],
  );

  useEffect(() => {
    load(filters);
  }, [filters, load]);

  const handlePurge = async (): Promise<void> => {
    setPurging(true);
    setPurgeError(null);
    setPurgeNotice(null);
    try {
      const deleted = await purgeSumupExchanges();
      setConfirmPurge(false);
      setPurgeNotice(deleted === 0 ? "Le journal était déjà vide." : `Journal vidé : ${deleted} échange(s) effacé(s).`);
      setExpandedId(null);
      load(filters);
    } catch (err) {
      setPurgeError(messageOf(err, "Le journal n'a pas pu être vidé."));
    } finally {
      setPurging(false);
    }
  };

  return (
    <Card
      title="Journal des échanges SumUp"
      subtitle="Chaque échange entre la caisse et le terminal, du plus récent au plus ancien."
    >
      <div className="space-y-4">
        {/* Filtres */}
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex min-h-touch items-center gap-2 text-sm font-medium text-fc-ink">
            <input
              type="checkbox"
              checked={!!filters.only_failed}
              onChange={(e) => setFilters((f) => ({ ...f, only_failed: e.target.checked }))}
              className="h-5 w-5 rounded border-fc-line text-fc-primary focus:ring-fc-primary"
            />
            Erreurs seulement
          </label>

          <label className="block">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">
              Opération
            </span>
            <select
              value={filters.operation ?? ""}
              onChange={(e) => setFilters((f) => ({ ...f, operation: e.target.value as SumupOperation | "" }))}
              className="min-h-touch rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary"
            >
              <option value="">Toutes</option>
              {OPERATION_OPTIONS.map((op) => (
                <option key={op} value={op}>
                  {operationLabel(op)}
                </option>
              ))}
            </select>
          </label>

          <div className="w-full sm:w-64">
            <Input
              label="N° d'encaissement"
              value={filters.checkout_id ?? ""}
              onChange={(e) => setFilters((f) => ({ ...f, checkout_id: e.target.value }))}
              placeholder="Coller un numéro"
            />
          </div>

          <Button variant="outline" size="sm" onClick={() => load(filters)} disabled={loading}>
            Actualiser
          </Button>
        </div>

        <ErrorNotice message={error} />
        <ErrorNotice message={purgeError} />
        {purgeNotice && !purgeError && <p className="text-sm text-fc-success font-medium">{purgeNotice}</p>}

        {loading ? (
          <p className="text-sm text-fc-ink-soft">Chargement…</p>
        ) : exchanges.length === 0 ? (
          <p className="text-sm text-fc-ink-soft">Aucun échange à afficher.</p>
        ) : (
          <>
            <p className="text-xs text-fc-ink-mute">
              {exchanges.length} échange(s) affiché(s) sur {total}.
            </p>
            <ul className="divide-y divide-fc-line rounded-fc-lg border border-fc-line">
              {exchanges.map((x) => {
                const open = expandedId === x.id;
                return (
                  <li key={x.id}>
                    <button
                      type="button"
                      onClick={() => setExpandedId(open ? null : x.id)}
                      aria-expanded={open}
                      aria-label={`${open ? "Masquer" : "Afficher"} le détail de l'échange ${operationLabel(x.operation)} du ${formatDateTime(x.created_at)}`}
                      className="flex w-full min-h-touch flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5 text-left hover:bg-fc-bg focus:outline-none focus:ring-2 focus:ring-fc-primary"
                    >
                      <span
                        aria-hidden
                        className={`h-2 w-2 flex-shrink-0 rounded-full ${x.is_error ? "bg-fc-danger" : "bg-fc-success"}`}
                      />
                      <span className="font-mono text-xs text-fc-ink-soft tabular-nums whitespace-nowrap">
                        {formatDateTime(x.created_at)}
                      </span>
                      <span className="text-sm font-medium text-fc-ink">{operationLabel(x.operation)}</span>
                      <span className="font-mono text-xs text-fc-ink-mute tabular-nums">
                        {x.response_status ?? "—"} · {x.duration_ms} ms
                      </span>
                      {x.retry_count > 0 && (
                        <span className="font-mono text-xs text-fc-ink-mute tabular-nums">
                          {x.retry_count} essai(s)
                        </span>
                      )}
                      {x.is_error && (
                        <span className="rounded-fc bg-fc-danger-soft px-2 py-0.5 text-xs font-medium text-fc-danger">
                          {causeLabel(x.error_type)}
                        </span>
                      )}
                      <span aria-hidden className="ml-auto text-xs text-fc-primary">
                        {open ? "Masquer" : "Détail"}
                      </span>
                    </button>

                    {open && (
                      <div className="border-t border-fc-line bg-fc-bg px-3 py-3 space-y-3">
                        <dl className="grid gap-2 sm:grid-cols-2">
                          <div>
                            <dt className="text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft">
                              Adresse appelée
                            </dt>
                            <dd className="font-mono text-xs text-fc-ink break-all">
                              {x.method} {x.url_path}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft">
                              N° d&apos;encaissement
                            </dt>
                            <dd className="font-mono text-xs text-fc-ink break-all">{x.checkout_id ?? "—"}</dd>
                          </div>
                          <div>
                            <dt className="text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft">
                              Référence caisse
                            </dt>
                            <dd className="font-mono text-xs text-fc-ink break-all">
                              {x.client_transaction_id ?? "—"}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft">
                              N° de requête
                            </dt>
                            <dd className="font-mono text-xs text-fc-ink break-all">{x.request_id ?? "—"}</dd>
                          </div>
                        </dl>

                        {x.error_message && (
                          <div>
                            <h5 className="text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1">
                              Message d&apos;erreur
                            </h5>
                            <p className="text-sm text-fc-danger break-words">{x.error_message}</p>
                          </div>
                        )}

                        <div>
                          <h5 className="text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1">
                            Détail envoyé
                          </h5>
                          <pre className="max-h-64 overflow-auto rounded-fc border border-fc-line bg-fc-surface p-2 font-mono text-xs text-fc-ink whitespace-pre-wrap break-words">
                            {formatDetail(x.request_payload)}
                          </pre>
                        </div>

                        <div>
                          <h5 className="text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1">
                            Détail reçu
                          </h5>
                          <pre className="max-h-64 overflow-auto rounded-fc border border-fc-line bg-fc-surface p-2 font-mono text-xs text-fc-ink whitespace-pre-wrap break-words">
                            {formatDetail(x.response_payload)}
                          </pre>
                        </div>
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </>
        )}

        {/* Purge */}
        <div className="flex flex-wrap items-center gap-2 border-t border-fc-line pt-4">
          {confirmPurge ? (
            <>
              <p className="w-full text-sm text-fc-ink">
                Vider le journal efface tous les échanges enregistrés. Les ventes, les encaissements et le journal des
                événements ne sont pas touchés.
              </p>
              <Button
                variant="danger"
                size="sm"
                onClick={() => void handlePurge()}
                disabled={purging}
                aria-busy={purging}
              >
                {purging ? "Vidage…" : "Confirmer le vidage"}
              </Button>
              <Button variant="outline" size="sm" onClick={() => setConfirmPurge(false)} disabled={purging}>
                Annuler
              </Button>
            </>
          ) : (
            <Button variant="outline" size="sm" onClick={() => setConfirmPurge(true)}>
              Vider le journal
            </Button>
          )}
        </div>
      </div>
    </Card>
  );
}
