"use client";

/**
 * Onglet Archives fiscales (PR4, docs/ARCHITECTURE_PR4.md §5) : liste des
 * clôtures scellées (mensuelles/annuelles + manuelles),
 * déclenchement d'une clôture manuelle avec garde-fous et double
 * confirmation, téléchargement de l'archive, contrôle d'intégrité, export
 * fiscal à la demande. Vocabulaire sans jargon : « archive fiscale »,
 * « empreinte », jamais « clôture NF525 » (auto-attestation uniquement,
 * cf. CLAUDE.md).
 */
import React, { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import { api, ApiError } from "@/lib/api";
import { copyToClipboard, downloadFile, shortHash } from "@/lib/download";
import { formatCurrency, formatDateTime } from "@/lib/format";
import {
  FISCAL_CLOSURE_TYPE_LABELS,
  type ClosuresIntegrityResponse,
  type CreateFiscalClosureRequest,
  type FiscalClosure,
  type FiscalClosureListResponse,
  type FiscalClosureType,
  type FiscalExportFormat,
  type FiscalIntegrityResponse,
} from "@/lib/types";

function ErrorNotice({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div role="alert" className="rounded-fc bg-fc-danger-soft border border-fc-danger/30 px-3 py-2 text-sm text-fc-danger">
      {message}
    </div>
  );
}

const selectClass =
  "w-full min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary";

const dateInputClass =
  "min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary";

export default function FiscalArchivesTab() {
  const [closures, setClosures] = useState<FiscalClosure[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    api
      .get<FiscalClosureListResponse>("/api/admin/fiscal-closures")
      .then((data) => setClosures(data.closures))
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger les archives fiscales."))
      .finally(() => setLoading(false));
  };

  useEffect(load, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-6">
      <FiscalClosuresListCard closures={closures} loading={loading} error={error} />
      <CreateClosureCard onCreated={load} />
      <IntegrityCheckCard />
      <FiscalExportCard />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Liste des clôtures (F5)
// ---------------------------------------------------------------------------

function FiscalClosuresListCard({
  closures,
  loading,
  error,
}: {
  closures: FiscalClosure[];
  loading: boolean;
  error: string | null;
}) {
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const handleDownload = async (closure: FiscalClosure): Promise<void> => {
    setDownloadingId(closure.id);
    setDownloadError(null);
    try {
      await downloadFile(`/api/admin/fiscal-closures/${closure.id}/archive`, `archive_cloture_${closure.sequence_number}.json.gz`);
    } catch (err) {
      setDownloadError(err instanceof ApiError ? err.detail : "Échec du téléchargement de l'archive.");
    } finally {
      setDownloadingId(null);
    }
  };

  const handleCopy = async (closure: FiscalClosure): Promise<void> => {
    const ok = await copyToClipboard(closure.archive_sha256);
    setCopiedId(ok ? closure.id : null);
    if (ok) setTimeout(() => setCopiedId(null), 2000);
  };

  return (
    <Card title="Archives fiscales" subtitle="Historique des clôtures scellées — mensuelles, annuelles et manuelles.">
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-3">
          <ErrorNotice message={error} />
          <ErrorNotice message={downloadError} />
          {closures.length === 0 && <p className="text-sm text-fc-ink-soft">Aucune archive fiscale pour l&apos;instant.</p>}
          {closures.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-fc-ink-mute uppercase text-xs tracking-wide">
                    <th className="py-2 pr-4">N°</th>
                    <th className="py-2 pr-4">Type</th>
                    <th className="py-2 pr-4">Période</th>
                    <th className="py-2 pr-4">Total période</th>
                    <th className="py-2 pr-4">Total perpétuel</th>
                    <th className="py-2 pr-4">Empreinte</th>
                    <th className="py-2 pr-4" />
                  </tr>
                </thead>
                <tbody>
                  {closures.map((c) => (
                    <tr key={c.id} className="border-t border-fc-line align-top">
                      <td className="py-2 pr-4 font-mono tabular-nums">{c.sequence_number}</td>
                      <td className="py-2 pr-4">{FISCAL_CLOSURE_TYPE_LABELS[c.closure_type]}</td>
                      <td className="py-2 pr-4 whitespace-nowrap">
                        {formatDateTime(c.period_start)}
                        <br />
                        <span className="text-fc-ink-mute">→ {formatDateTime(c.period_end)}</span>
                      </td>
                      <td className="py-2 pr-4 font-mono tabular-nums whitespace-nowrap">{formatCurrency(c.grand_total_net)}</td>
                      <td className="py-2 pr-4 font-mono tabular-nums whitespace-nowrap">{formatCurrency(c.perpetual_net)}</td>
                      <td className="py-2 pr-4">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-xs text-fc-ink-soft" title={c.archive_sha256}>
                            {shortHash(c.archive_sha256)}…
                          </span>
                          <button
                            type="button"
                            onClick={() => void handleCopy(c)}
                            className="min-h-touch min-w-touch inline-flex items-center justify-center rounded-fc text-xs text-fc-primary hover:bg-fc-primary-soft px-2"
                          >
                            {copiedId === c.id ? "Copié" : "Copier"}
                          </button>
                        </div>
                      </td>
                      <td className="py-2 pr-4">
                        <Button variant="outline" onClick={() => void handleDownload(c)} disabled={downloadingId === c.id}>
                          {downloadingId === c.id ? "Préparation…" : "Télécharger l'archive"}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Clôturer maintenant (F5) — garde-fous + double confirmation
// ---------------------------------------------------------------------------

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function monthBounds(year: number, month: number): { start: string; end: string } {
  const lastDay = new Date(year, month, 0).getDate();
  return {
    start: `${year}-${pad2(month)}-01T00:00:00`,
    end: `${year}-${pad2(month)}-${pad2(lastDay)}T23:59:59`,
  };
}

function annualBounds(year: number): { start: string; end: string } {
  return { start: `${year}-01-01T00:00:00`, end: `${year}-12-31T23:59:59` };
}

function closureErrorMessage(err: ApiError): string {
  if (err.code === "drawer_open") return "Fermez la caisse avant de clôturer.";
  return err.detail;
}

function CreateClosureCard({ onCreated }: { onCreated: () => void }) {
  const now = new Date();
  const [closureType, setClosureType] = useState<FiscalClosureType>("manual");
  const [manualStart, setManualStart] = useState(() => now.toISOString().slice(0, 10));
  const [manualEnd, setManualEnd] = useState(() => now.toISOString().slice(0, 10));
  const [year, setYear] = useState(now.getFullYear());
  const [month, setMonth] = useState(now.getMonth() + 1);

  const [confirmStep, setConfirmStep] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const period =
    closureType === "manual"
      ? { start: manualStart ? `${manualStart}T00:00:00` : "", end: manualEnd ? `${manualEnd}T23:59:59` : "" }
      : closureType === "monthly"
        ? monthBounds(year, month)
        : annualBounds(year);

  const periodValid = Boolean(period.start && period.end && period.start <= period.end);
  const periodPast = periodValid && new Date(period.end).getTime() <= now.getTime();

  const handleConfirm = async (): Promise<void> => {
    setCreating(true);
    setError(null);
    setSuccess(null);
    try {
      const body: CreateFiscalClosureRequest = {
        closure_type: closureType,
        period_start: period.start,
        period_end: period.end,
      };
      const created = await api.post<FiscalClosure>("/api/admin/fiscal-closures", body);
      setSuccess(`Clôture n° ${created.sequence_number} créée et scellée.`);
      setConfirmStep(false);
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? closureErrorMessage(err) : "Échec de la clôture.");
    } finally {
      setCreating(false);
    }
  };

  return (
    <Card title="Clôturer maintenant" subtitle="Scelle une période : au-delà, plus aucune modification n'est possible.">
      <div className="space-y-4">
        <ErrorNotice message={error} />
        {success && !error && <p className="text-sm text-fc-primary-deep">{success}</p>}

        <label className="block max-w-xs">
          <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Type de clôture</span>
          <select
            value={closureType}
            onChange={(e) => {
              setClosureType(e.target.value as FiscalClosureType);
              setConfirmStep(false);
              setSuccess(null);
            }}
            className={selectClass}
          >
            <option value="manual">Manuelle (période libre)</option>
            <option value="monthly">Mensuelle</option>
            <option value="annual">Annuelle</option>
          </select>
        </label>

        {closureType === "manual" && (
          <div className="flex flex-wrap items-end gap-3">
            <label className="block">
              <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Du</span>
              <input
                type="date"
                aria-label="Début de la période à clôturer"
                value={manualStart}
                onChange={(e) => setManualStart(e.target.value)}
                className={dateInputClass}
              />
            </label>
            <label className="block">
              <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Au</span>
              <input
                type="date"
                aria-label="Fin de la période à clôturer"
                value={manualEnd}
                onChange={(e) => setManualEnd(e.target.value)}
                className={dateInputClass}
              />
            </label>
          </div>
        )}

        {closureType === "monthly" && (
          <div className="flex flex-wrap items-end gap-3">
            <label className="block">
              <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Mois</span>
              <select value={month} onChange={(e) => setMonth(Number(e.target.value))} className={`${selectClass} min-w-[160px]`}>
                {Array.from({ length: 12 }, (_, i) => i + 1).map((m) => (
                  <option key={m} value={m}>
                    {new Date(2000, m - 1, 1).toLocaleDateString("fr-FR", { month: "long" })}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Année</span>
              <select value={year} onChange={(e) => setYear(Number(e.target.value))} className={`${selectClass} min-w-[110px]`}>
                {[now.getFullYear(), now.getFullYear() - 1].map((y) => (
                  <option key={y} value={y}>
                    {y}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}

        {closureType === "annual" && (
          <label className="block max-w-[160px]">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Année</span>
            <select value={year} onChange={(e) => setYear(Number(e.target.value))} className={selectClass}>
              {[now.getFullYear(), now.getFullYear() - 1].map((y) => (
                <option key={y} value={y}>
                  {y}
                </option>
              ))}
            </select>
          </label>
        )}

        {periodValid && !periodPast && (
          <div role="alert" className="rounded-fc bg-fc-warn-soft border border-fc-warn/40 px-3 py-2.5 text-sm font-medium text-fc-warn">
            Cette période n&apos;est pas encore terminée : elle doit être entièrement passée pour être clôturée.
          </div>
        )}

        {!confirmStep ? (
          <Button variant="danger" onClick={() => setConfirmStep(true)} disabled={!periodPast}>
            Clôturer
          </Button>
        ) : (
          <div className="space-y-3 rounded-fc-lg border border-fc-danger/40 bg-fc-danger-soft p-4">
            <p className="text-sm font-semibold text-fc-danger">Cette clôture est définitive et scellée.</p>
            <p className="text-sm text-fc-danger">
              Une fois créée, l&apos;archive ne pourra plus être modifiée ni supprimée. Confirmez-vous la clôture de cette période ?
            </p>
            <div className="flex gap-3">
              <Button variant="outline" onClick={() => setConfirmStep(false)} disabled={creating}>
                Annuler
              </Button>
              <Button variant="danger" onClick={() => void handleConfirm()} disabled={creating}>
                {creating ? "Clôture…" : "Confirmer la clôture"}
              </Button>
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Vérifier l'intégrité (F5, F6)
// ---------------------------------------------------------------------------

function IntegrityTile({ label, check }: { label: string; check?: { ok?: boolean; valid?: boolean; message?: string; count?: number } }) {
  if (!check) return null;
  const ok = check.ok ?? check.valid ?? true;
  return (
    <div className={`rounded-fc-lg border p-3 ${ok ? "border-fc-primary/30 bg-fc-primary-soft" : "border-fc-warn/40 bg-fc-warn-soft"}`}>
      <div className={`text-sm font-semibold ${ok ? "text-fc-primary-deep" : "text-fc-warn"}`}>{label}</div>
      {typeof check.count === "number" && <div className="text-xs text-fc-ink-soft">{check.count} élément(s)</div>}
      {check.message && <div className="text-xs text-fc-ink-soft mt-1">{check.message}</div>}
    </div>
  );
}

function IntegrityCheckCard() {
  const [closuresResult, setClosuresResult] = useState<ClosuresIntegrityResponse | null>(null);
  const [fullResult, setFullResult] = useState<FiscalIntegrityResponse | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleCheck = async (): Promise<void> => {
    setChecking(true);
    setError(null);
    try {
      const [closures, full] = await Promise.all([
        api.get<ClosuresIntegrityResponse>("/api/admin/fiscal-closures/integrity"),
        api.get<FiscalIntegrityResponse>("/api/admin/fiscal/integrity"),
      ]);
      setClosuresResult(closures);
      setFullResult(full);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Échec du contrôle d'intégrité.");
    } finally {
      setChecking(false);
    }
  };

  return (
    <Card title="Vérifier l'intégrité" subtitle="Contrôle la continuité des ventes, des clôtures et des écritures comptables.">
      <div className="space-y-3">
        <ErrorNotice message={error} />
        <Button onClick={() => void handleCheck()} disabled={checking}>
          {checking ? "Vérification…" : "Vérifier l'intégrité"}
        </Button>
        {(closuresResult || fullResult) && (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <IntegrityTile label="Clôtures fiscales" check={closuresResult ?? undefined} />
            <IntegrityTile label="Ventes" check={fullResult?.transactions} />
            <IntegrityTile label="Clôtures de caisse (Z)" check={fullResult?.z_reports} />
            <IntegrityTile label="Écritures comptables" check={fullResult?.accounting} />
            <IntegrityTile label="Journal des événements" check={fullResult?.jet} />
          </div>
        )}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Export fiscal à la demande (F6)
// ---------------------------------------------------------------------------

function FiscalExportCard() {
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [format, setFormat] = useState<FiscalExportFormat>("json");
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hash, setHash] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const handleGenerate = async (): Promise<void> => {
    setGenerating(true);
    setError(null);
    setHash(null);
    setCopied(false);
    try {
      const qs = new URLSearchParams({ format });
      if (from) qs.set("from", from);
      if (to) qs.set("to", to);
      const filename = `export_fiscal_${from || "debut"}_${to || "fin"}.${format}`;
      const { headers } = await downloadFile(`/api/admin/fiscal-export?${qs.toString()}`, filename);
      setHash(headers["x-export-sha256"] ?? null);
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Échec de la génération de l'export fiscal.";
      setError(
        err instanceof ApiError && err.code === "chain_invalid"
          ? "La chaîne de sécurité est rompue sur cette période : contactez le support avant de générer l'export."
          : detail,
      );
    } finally {
      setGenerating(false);
    }
  };

  const handleCopy = async (): Promise<void> => {
    if (!hash) return;
    const ok = await copyToClipboard(hash);
    setCopied(ok);
  };

  return (
    <Card title="Export fiscal à la demande" subtitle="Génère un fichier JSON ou XML des ventes et clôtures sur une période, avec son empreinte.">
      <div className="space-y-4">
        <ErrorNotice message={error} />
        <div className="flex flex-wrap items-end gap-3">
          <label className="block">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Du</span>
            <input
              type="date"
              aria-label="Début de la période à exporter"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
              className={dateInputClass}
            />
          </label>
          <label className="block">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Au</span>
            <input
              type="date"
              aria-label="Fin de la période à exporter"
              value={to}
              onChange={(e) => setTo(e.target.value)}
              className={dateInputClass}
            />
          </label>
          <label className="block">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Format</span>
            <select value={format} onChange={(e) => setFormat(e.target.value as FiscalExportFormat)} className={`${selectClass} min-w-[120px]`}>
              <option value="json">JSON</option>
              <option value="xml">XML</option>
            </select>
          </label>
          <Button onClick={() => void handleGenerate()} disabled={generating}>
            {generating ? "Génération…" : "Générer l'export fiscal"}
          </Button>
        </div>
        {hash && (
          <div className="flex flex-wrap items-center gap-2 text-sm text-fc-ink-soft">
            <span>
              Empreinte : <span className="font-mono text-fc-ink">{shortHash(hash)}…</span>
            </span>
            <button
              type="button"
              onClick={() => void handleCopy()}
              className="min-h-touch inline-flex items-center justify-center rounded-fc text-xs text-fc-primary hover:bg-fc-primary-soft px-2"
            >
              {copied ? "Copiée" : "Copier l'empreinte complète"}
            </button>
          </div>
        )}
      </div>
    </Card>
  );
}
