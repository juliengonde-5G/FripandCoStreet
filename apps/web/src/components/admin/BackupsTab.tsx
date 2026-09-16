"use client";

/**
 * Onglet Sauvegardes (PR5, docs/ARCHITECTURE_PR5.md §1, G6) : état de la
 * base (moteur, taille, volumes par table, espace disque libre, dernière
 * sauvegarde), réglages de la sauvegarde nocturne (rétention, activation,
 * e-mail d'alerte) et liste des sauvegardes (déclenchement manuel,
 * téléchargement, suppression). Vocabulaire sans jargon : « sauvegarde »,
 * « empreinte » — jamais « dump » côté UI (le mot reste réservé aux
 * commentaires techniques du service `database_backup.py`).
 */
import React, { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import Input from "@/components/ui/Input";
import ErrorNotice from "@/components/ui/ErrorNotice";
import { api, ApiError } from "@/lib/api";
import { describeError, describeErrorAs, type DisplayableError } from "@/lib/apiError";
import { copyToClipboard, downloadFile, shortHash } from "@/lib/download";
import { formatBytes, formatDateTime, formatRelativeTime } from "@/lib/format";
import type { DatabaseBackup, DatabaseBackupConfig, DatabaseBackupListResponse, DatabaseState } from "@/lib/types";

function SavedNotice({ show }: { show: boolean }) {
  if (!show) return null;
  return <span className="text-sm text-fc-success font-medium">Enregistré.</span>;
}

const TRIGGER_LABELS: Record<string, string> = {
  nightly: "Nocturne",
  manual: "Manuelle",
};

const STATUS_LABELS: Record<string, string> = {
  success: "Réussie",
  failed: "Échouée",
  missing: "Fichier absent",
};

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === "success"
      ? "bg-fc-success-soft text-fc-success"
      : status === "failed"
        ? "bg-fc-danger-soft text-fc-danger"
        : "bg-fc-warn-soft text-fc-warn";
  const dot = status === "success" ? "bg-fc-success" : status === "failed" ? "bg-fc-danger" : "bg-fc-warn";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-fc px-2.5 py-1 text-xs font-medium ${cls}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} aria-hidden />
      {STATUS_LABELS[status] ?? status}
    </span>
  );
}

export default function BackupsTab() {
  // Compteur incrémenté après une sauvegarde lancée/supprimée — force
  // `DatabaseStateCard` à recharger (sa carte affiche la dernière
  // sauvegarde, qui change avec la liste).
  const [refreshToken, setRefreshToken] = useState(0);

  return (
    <div className="space-y-6">
      <DatabaseStateCard refreshToken={refreshToken} />
      <BackupConfigCard />
      <BackupsListCard onChanged={() => setRefreshToken((n) => n + 1)} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// État de la base
// ---------------------------------------------------------------------------

function DatabaseStateCard({ refreshToken }: { refreshToken: number }) {
  const [state, setState] = useState<DatabaseState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<DisplayableError>(null);

  const load = (): void => {
    setLoading(true);
    setError(null);
    api
      .get<DatabaseState>("/api/admin/database/state")
      .then(setState)
      .catch((err) => setError(describeError(err, "Impossible de charger l'état de la base.")))
      .finally(() => setLoading(false));
  };

  useEffect(load, [refreshToken]); // eslint-disable-line react-hooks/exhaustive-deps

  const tables = [...(state?.tables ?? [])].sort((a, b) => b.rows_estimate - a.rows_estimate);

  return (
    <Card
      title="État de la base"
      subtitle="Moteur, volumétrie et dernière sauvegarde connue."
      action={
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          Actualiser
        </Button>
      }
    >
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-5">
          <ErrorNotice message={error} />
          {state && (
            <>
              <div className="grid gap-4 sm:grid-cols-3">
                <div className="rounded-fc-lg border border-fc-line p-3">
                  <div className="text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft">Moteur</div>
                  <div className="mt-1 font-mono text-sm text-fc-ink">{state.engine_version ?? "—"}</div>
                </div>
                <div className="rounded-fc-lg border border-fc-line p-3">
                  <div className="text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft">Taille de la base</div>
                  <div className="mt-1 font-mono text-sm text-fc-ink tabular-nums">{formatBytes(state.database_size_bytes)}</div>
                </div>
                <div className="rounded-fc-lg border border-fc-line p-3">
                  <div className="text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft">
                    Espace libre (dossier de sauvegarde)
                  </div>
                  <div className="mt-1 font-mono text-sm text-fc-ink tabular-nums">{formatBytes(state.backup_dir_free_bytes)}</div>
                </div>
              </div>

              <div>
                <h4 className="text-sm font-semibold text-fc-ink mb-2">Dernière sauvegarde</h4>
                {state.last_backup ? (
                  <div className="flex flex-wrap items-center gap-3 rounded-fc-lg border border-fc-line p-3">
                    <StatusBadge status={state.last_backup.status} />
                    <span className="text-sm text-fc-ink" title={formatDateTime(state.last_backup.created_at)}>
                      {formatRelativeTime(state.last_backup.created_at)}
                    </span>
                    <span className="text-xs text-fc-ink-mute">({formatDateTime(state.last_backup.created_at)})</span>
                    {state.last_backup.status === "success" && (
                      <span className="text-xs text-fc-ink-mute font-mono tabular-nums">
                        {formatBytes(state.last_backup.size_bytes)}
                      </span>
                    )}
                  </div>
                ) : (
                  <p className="text-sm text-fc-ink-soft">Aucune sauvegarde pour l&apos;instant.</p>
                )}
              </div>

              <div>
                <h4 className="text-sm font-semibold text-fc-ink mb-2">Volumes par table</h4>
                {tables.length === 0 ? (
                  <p className="text-sm text-fc-ink-soft">Aucune donnée de volumétrie.</p>
                ) : (
                  <div className="max-h-72 overflow-y-auto overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead className="sticky top-0 bg-fc-surface">
                        <tr className="text-left text-fc-ink-mute uppercase text-xs tracking-wide">
                          <th className="py-2 pr-4">Table</th>
                          <th className="py-2 pr-4">Lignes (estimation)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {tables.map((t) => (
                          <tr key={t.name} className="border-t border-fc-line">
                            <td className="py-1.5 pr-4 font-mono text-xs text-fc-ink">{t.name}</td>
                            <td className="py-1.5 pr-4 font-mono tabular-nums">{t.rows_estimate.toLocaleString("fr-FR")}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Réglages
// ---------------------------------------------------------------------------

const EMPTY_CONFIG: DatabaseBackupConfig = { retention_days: 60, nightly_enabled: true, alert_email: "" };

function BackupConfigCard() {
  const [form, setForm] = useState<DatabaseBackupConfig>(EMPTY_CONFIG);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<DisplayableError>(null);

  useEffect(() => {
    api
      .get<DatabaseBackupConfig>("/api/admin/database/config")
      .then((data) => setForm({ ...EMPTY_CONFIG, ...data }))
      .catch((err) => setError(describeError(err, "Impossible de charger les réglages de sauvegarde.")))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async (): Promise<void> => {
    setSaving(true);
    setError(null);
    try {
      // Remplacement complet (G3) : toujours les 3 champs.
      const data = await api.put<DatabaseBackupConfig>("/api/admin/database/config", form);
      setForm({ ...EMPTY_CONFIG, ...data });
      setSaved(true);
    } catch (err) {
      setError(describeError(err, "Échec de l'enregistrement."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card title="Réglages" subtitle="Rétention, sauvegarde nocturne automatique et alerte en cas d'échec.">
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-4">
          <ErrorNotice message={error} />
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block max-w-[220px]">
              <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">
                Rétention (jours)
              </span>
              <input
                type="number"
                min={7}
                max={3650}
                value={form.retention_days}
                onChange={(e) => {
                  setForm((f) => ({ ...f, retention_days: Number(e.target.value) || 0 }));
                  setSaved(false);
                }}
                className="w-full min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary font-mono"
              />
              <span className="mt-1 block text-xs text-fc-ink-mute">Entre 7 et 3650 jours.</span>
            </label>
            <Input
              label="E-mail d'alerte en cas d'échec"
              type="email"
              value={form.alert_email}
              onChange={(e) => {
                setForm((f) => ({ ...f, alert_email: e.target.value }));
                setSaved(false);
              }}
              placeholder="Laissez vide pour utiliser l'e-mail de la boutique"
            />
          </div>
          <label className="flex items-center gap-2 text-sm font-medium text-fc-ink">
            <input
              type="checkbox"
              checked={form.nightly_enabled}
              onChange={(e) => {
                setForm((f) => ({ ...f, nightly_enabled: e.target.checked }));
                setSaved(false);
              }}
              className="h-5 w-5 rounded border-fc-line text-fc-primary focus:ring-fc-primary"
            />
            Activer la sauvegarde nocturne (chaque nuit à 3h00)
          </label>
          <div className="flex items-center gap-3">
            <Button onClick={() => void handleSave()} disabled={saving}>
              {saving ? "Enregistrement…" : "Enregistrer"}
            </Button>
            <SavedNotice show={saved} />
          </div>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Sauvegardes — déclenchement manuel, téléchargement, suppression
// ---------------------------------------------------------------------------

function runErrorMessage(err: ApiError): string {
  if (err.code === "backup_running") return "Une sauvegarde est déjà en cours — réessayez dans quelques instants.";
  if (err.code === "backup_failed") return "La sauvegarde a échoué. Consultez la ligne correspondante dans la liste ci-dessous pour le détail.";
  return err.detail;
}

function BackupsListCard({ onChanged }: { onChanged: () => void }) {
  const [list, setList] = useState<DatabaseBackup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<DisplayableError>(null);

  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<DisplayableError>(null);
  const [runSuccess, setRunSuccess] = useState<string | null>(null);

  const [copiedId, setCopiedId] = useState<string | null>(null);

  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<DisplayableError>(null);
  const [downloadedHash, setDownloadedHash] = useState<{ id: string; hash: string } | null>(null);

  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<DisplayableError>(null);

  const [expandedErrorId, setExpandedErrorId] = useState<string | null>(null);

  const load = (): void => {
    setLoading(true);
    setError(null);
    api
      .get<DatabaseBackupListResponse>("/api/admin/database/backups?limit=50")
      .then((data) => setList(data.backups))
      .catch((err) => setError(describeError(err, "Impossible de charger les sauvegardes.")))
      .finally(() => setLoading(false));
  };

  useEffect(load, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleRun = async (): Promise<void> => {
    setRunning(true);
    setRunError(null);
    setRunSuccess(null);
    try {
      const backup = await api.post<DatabaseBackup>("/api/admin/database/backups/run");
      setRunSuccess(`Sauvegarde ${STATUS_LABELS[backup.status]?.toLowerCase() ?? backup.status}.`);
      load();
      onChanged();
    } catch (err) {
      setRunError(
        err instanceof ApiError
          ? describeErrorAs(err, runErrorMessage(err))
          : describeError(err, "Échec du lancement de la sauvegarde."),
      );
      // Une sauvegarde en échec écrit quand même une ligne (G1) : on
      // recharge la liste pour qu'elle apparaisse.
      load();
      onChanged();
    } finally {
      setRunning(false);
    }
  };

  const handleCopy = async (backup: DatabaseBackup): Promise<void> => {
    if (!backup.sha256) return;
    const ok = await copyToClipboard(backup.sha256);
    setCopiedId(ok ? backup.id : null);
    if (ok) setTimeout(() => setCopiedId(null), 2000);
  };

  const handleDownload = async (backup: DatabaseBackup): Promise<void> => {
    setDownloadingId(backup.id);
    setDownloadError(null);
    setDownloadedHash(null);
    try {
      const { headers } = await downloadFile(`/api/admin/database/backups/${backup.id}/download`, backup.filename);
      const hash = headers["x-backup-sha256"];
      if (hash) setDownloadedHash({ id: backup.id, hash });
    } catch (err) {
      setDownloadError(describeError(err, "Échec du téléchargement de la sauvegarde."));
    } finally {
      setDownloadingId(null);
    }
  };

  const handleDelete = async (backup: DatabaseBackup): Promise<void> => {
    setDeletingId(backup.id);
    setDeleteError(null);
    try {
      await api.delete(`/api/admin/database/backups/${backup.id}`);
      setConfirmDeleteId(null);
      load();
      onChanged();
    } catch (err) {
      setDeleteError(describeError(err, "Échec de la suppression."));
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <Card title="Sauvegardes" subtitle="Historique des sauvegardes de la base, du plus récent au plus ancien.">
      <div className="space-y-4">
        <div className="space-y-2">
          <ErrorNotice message={runError} />
          {runSuccess && !runError && <p className="text-sm text-fc-success font-medium">{runSuccess}</p>}
          <Button onClick={() => void handleRun()} disabled={running} aria-busy={running}>
            {running ? "Sauvegarde en cours…" : "Lancer une sauvegarde maintenant"}
          </Button>
        </div>

        {loading ? (
          <p className="text-sm text-fc-ink-soft">Chargement…</p>
        ) : (
          <div className="space-y-2">
            <ErrorNotice message={error} />
            <ErrorNotice message={downloadError} />
            <ErrorNotice message={deleteError} />
            {list.length === 0 && <p className="text-sm text-fc-ink-soft">Aucune sauvegarde pour l&apos;instant.</p>}
            {list.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-fc-ink-mute uppercase text-xs tracking-wide">
                      <th className="py-2 pr-4">Date</th>
                      <th className="py-2 pr-4">Déclencheur</th>
                      <th className="py-2 pr-4">Taille</th>
                      <th className="py-2 pr-4">Statut</th>
                      <th className="py-2 pr-4">Empreinte</th>
                      <th className="py-2 pr-4" />
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((b) => (
                      <React.Fragment key={b.id}>
                        <tr className="border-t border-fc-line align-top">
                          <td className="py-2 pr-4 whitespace-nowrap">{formatDateTime(b.created_at)}</td>
                          <td className="py-2 pr-4">{TRIGGER_LABELS[b.trigger] ?? b.trigger}</td>
                          <td className="py-2 pr-4 font-mono tabular-nums whitespace-nowrap">{formatBytes(b.size_bytes)}</td>
                          <td className="py-2 pr-4">
                            <div className="flex items-center gap-2">
                              <StatusBadge status={b.status} />
                              {b.status === "failed" && b.error && (
                                <button
                                  type="button"
                                  onClick={() => setExpandedErrorId(expandedErrorId === b.id ? null : b.id)}
                                  className="min-h-touch inline-flex items-center rounded-fc text-xs text-fc-primary hover:bg-fc-primary-soft px-2"
                                >
                                  {expandedErrorId === b.id ? "Masquer l'erreur" : "Voir l'erreur"}
                                </button>
                              )}
                            </div>
                          </td>
                          <td className="py-2 pr-4">
                            {b.sha256 ? (
                              <div className="flex items-center gap-2">
                                <span className="font-mono text-xs text-fc-ink-soft" title={b.sha256}>
                                  {shortHash(b.sha256)}…
                                </span>
                                <button
                                  type="button"
                                  onClick={() => void handleCopy(b)}
                                  className="min-h-touch min-w-touch inline-flex items-center justify-center rounded-fc text-xs text-fc-primary hover:bg-fc-primary-soft px-2"
                                >
                                  {copiedId === b.id ? "Copiée" : "Copier"}
                                </button>
                              </div>
                            ) : (
                              <span className="text-fc-ink-mute">—</span>
                            )}
                          </td>
                          <td className="py-2 pr-4">
                            <div className="flex flex-wrap gap-2">
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={() => void handleDownload(b)}
                                disabled={b.status !== "success" || downloadingId === b.id}
                              >
                                {downloadingId === b.id ? "Préparation…" : "Télécharger"}
                              </Button>
                              {confirmDeleteId === b.id ? (
                                <>
                                  <Button
                                    variant="danger"
                                    size="sm"
                                    onClick={() => void handleDelete(b)}
                                    disabled={deletingId === b.id}
                                    aria-busy={deletingId === b.id}
                                  >
                                    {deletingId === b.id ? "Suppression…" : "Confirmer"}
                                  </Button>
                                  <Button variant="outline" size="sm" onClick={() => setConfirmDeleteId(null)} disabled={deletingId === b.id}>
                                    Annuler
                                  </Button>
                                </>
                              ) : (
                                <Button variant="outline" size="sm" onClick={() => setConfirmDeleteId(b.id)}>
                                  Supprimer
                                </Button>
                              )}
                            </div>
                          </td>
                        </tr>
                        {expandedErrorId === b.id && b.error && (
                          <tr className="border-t border-fc-line bg-fc-danger-soft">
                            <td colSpan={6} className="py-2 px-4 text-xs text-fc-danger font-mono whitespace-pre-wrap">
                              {b.error}
                            </td>
                          </tr>
                        )}
                        {downloadedHash?.id === b.id && (
                          <tr className="border-t border-fc-line">
                            <td colSpan={6} className="py-2 px-4 text-xs text-fc-ink-soft">
                              Empreinte reçue à l&apos;instant : <span className="font-mono text-fc-ink">{shortHash(downloadedHash.hash)}…</span>
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
