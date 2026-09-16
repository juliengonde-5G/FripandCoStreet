"use client";

/**
 * Onglet Supervision (PR12, docs/ARCHITECTURE_PR12.md §1, N2) — une page
 * pour répondre à « est-ce que tout va bien ? » sans ouvrir un terminal.
 *
 * Un bandeau dit l'état général, les cartes détaillent : application,
 * base, sauvegardes, tâches planifiées, intégrité des chaînes, services
 * externes, imprimante, files d'attente, dernières erreurs.
 *
 * Deux règles de lecture :
 *   - une valeur que le serveur n'a pas su calculer s'affiche « — », on
 *     n'invente jamais un zéro rassurant ;
 *   - l'intégrité n'est pas recalculée à l'ouverture de la page (c'est
 *     long et cela laisse une trace dans le journal) : tant que personne
 *     n'a cliqué « Vérifier maintenant », on affiche « jamais vérifié ».
 *
 * La page se rafraîchit toute seule chaque minute, mais uniquement quand
 * l'onglet du navigateur est au premier plan : une tablette posée sur le
 * comptoir ne doit pas interroger le serveur toute la nuit.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";

import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import ErrorReference from "@/components/ui/ErrorReference";
import { describeError, errorRef, errorText, type DisplayableError } from "@/lib/apiError";
import { copyToClipboard } from "@/lib/download";
import { formatBytes, formatDateTime, formatNumber, formatRelativeTime } from "@/lib/format";
import {
  fetchMonitoring,
  runMonitoringCheck,
  type Monitoring,
  type MonitoringIntegrityChain,
  type MonitoringJob,
  type MonitoringStatus,
} from "@/lib/monitoring";

/** Période de rafraîchissement automatique (contrat N2 : 60 s). */
const REFRESH_MS = 60_000;

const STATUS_LABELS: Record<MonitoringStatus, string> = {
  ok: "Tout va bien",
  warning: "À surveiller",
  critical: "Intervention nécessaire",
};

const STATUS_HINTS: Record<MonitoringStatus, string> = {
  ok: "Aucun point d'attention détecté sur l'installation.",
  warning: "La caisse fonctionne, mais un point demande une action dans la journée.",
  critical: "La caisse ne peut pas être tenue pour fiable — voir les cartes en rouge.",
};

/** Noms de tâches lisibles : le serveur renvoie des identifiants
 * techniques, l'écran ne les montre jamais tels quels. */
const JOB_LABELS: Record<string, string> = {
  daily_fiscal_close_guard: "Clôture de la journée (garde-fou)",
  monthly_fiscal_closure: "Clôture du mois",
  annual_fiscal_closure: "Clôture de l'année",
  nightly_database_backup: "Sauvegarde nocturne",
  daily_client_deletions: "Suppressions clientes arrivées à échéance",
};

function jobLabel(job: MonitoringJob): string {
  return JOB_LABELS[job.name] ?? job.name;
}

// ---------------------------------------------------------------------------
// Petites briques d'affichage
// ---------------------------------------------------------------------------

type Tone = "ok" | "warn" | "danger" | "neutral";

const TONE_CLASSES: Record<Tone, string> = {
  ok: "bg-fc-success-soft text-fc-success",
  warn: "bg-fc-warn-soft text-fc-warn",
  danger: "bg-fc-danger-soft text-fc-danger",
  neutral: "bg-fc-bg-alt text-fc-ink-soft",
};

const TONE_DOTS: Record<Tone, string> = {
  ok: "bg-fc-success",
  warn: "bg-fc-warn",
  danger: "bg-fc-danger",
  neutral: "bg-fc-ink-mute",
};

function Pill({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-fc px-2.5 py-1 text-xs font-medium ${TONE_CLASSES[tone]}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${TONE_DOTS[tone]}`} aria-hidden />
      {children}
    </span>
  );
}

/** Une donnée en clair : intitulé au-dessus, valeur en dessous. */
function Field({ label, value, mono = true }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="rounded-fc-lg border border-fc-line p-3">
      <div className="text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft">{label}</div>
      <div className={`mt-1 text-sm text-fc-ink ${mono ? "font-mono tabular-nums" : ""} break-words`}>{value}</div>
    </div>
  );
}

function YesNo({ value, yes = "Oui", no = "Non" }: { value: boolean | null | undefined; yes?: string; no?: string }) {
  if (value === null || value === undefined) return <span className="text-fc-ink-mute">—</span>;
  return <span>{value ? yes : no}</span>;
}

/** Durée d'activité en clair — « 3 j 4 h », jamais un nombre de secondes. */
function formatUptime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds) || seconds < 0) return "—";
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days} j ${hours} h`;
  if (hours > 0) return `${hours} h ${minutes} min`;
  return `${minutes} min`;
}

function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return formatNumber(value);
}

// ---------------------------------------------------------------------------
// Onglet
// ---------------------------------------------------------------------------

export default function MonitoringTab() {
  const [data, setData] = useState<Monitoring | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<DisplayableError>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<DisplayableError>(null);

  // Évite qu'un rafraîchissement automatique lent n'écrase un résultat
  // plus récent (vérification manuelle lancée entre-temps).
  const inFlightRef = useRef(false);

  const load = useCallback(async (): Promise<void> => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const snapshot = await fetchMonitoring();
      setData(snapshot);
      setError(null);
    } catch (err) {
      setError(describeError(err, "Impossible de lire l'état de l'installation."));
    } finally {
      inFlightRef.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Rafraîchissement périodique, suspendu quand l'onglet passe en
  // arrière-plan et relancé (avec une lecture immédiate) au retour.
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;

    const start = (): void => {
      if (timer !== null) return;
      timer = setInterval(() => void load(), REFRESH_MS);
    };
    const stop = (): void => {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
    };
    const onVisibility = (): void => {
      if (document.visibilityState === "visible") {
        void load();
        start();
      } else {
        stop();
      }
    };

    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [load]);

  const handleCheck = async (): Promise<void> => {
    setChecking(true);
    setCheckError(null);
    try {
      const snapshot = await runMonitoringCheck();
      setData(snapshot);
      setError(null);
    } catch (err) {
      setCheckError(describeError(err, "La vérification n'a pas pu être menée à son terme."));
    } finally {
      setChecking(false);
    }
  };

  if (loading) {
    return <p className="text-sm text-fc-ink-soft">Chargement…</p>;
  }

  return (
    <div className="space-y-6">
      {errorText(error) && (
        <div role="alert" className="rounded-fc bg-fc-danger-soft border border-fc-danger/30 px-3 py-2 text-sm text-fc-danger">
          {errorText(error)}
          <ErrorReference reference={errorRef(error)} />
        </div>
      )}

      {data && (
        <>
          <GlobalBanner data={data} onRefresh={() => void load()} />
          <div className="grid gap-6 lg:grid-cols-2">
            <AppCard data={data} />
            <DatabaseCard data={data} />
            <BackupsCard data={data} />
            <JobsCard data={data} />
            <IntegrityCard data={data} checking={checking} error={checkError} onCheck={() => void handleCheck()} />
            <ExternalCard data={data} />
            <PrinterCard data={data} />
            <QueuesCard data={data} />
          </div>
          <RecentErrorsCard data={data} />
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bandeau d'état global
// ---------------------------------------------------------------------------

const BANNER_CLASSES: Record<MonitoringStatus, string> = {
  ok: "bg-fc-success-soft border-fc-success/30 text-fc-success",
  warning: "bg-fc-warn-soft border-fc-warn/40 text-fc-warn",
  critical: "bg-fc-danger-soft border-fc-danger/40 text-fc-danger",
};

function GlobalBanner({ data, onRefresh }: { data: Monitoring; onRefresh: () => void }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={`rounded-fc-lg border px-4 py-4 ${BANNER_CLASSES[data.status]}`}
    >
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-lg font-semibold">{STATUS_LABELS[data.status]}</span>
        <span className="text-sm opacity-80">{STATUS_HINTS[data.status]}</span>
        <div className="ml-auto flex items-center gap-3">
          <span className="text-xs opacity-80 whitespace-nowrap">Relevé {formatRelativeTime(data.generated_at)}</span>
          <Button variant="outline" size="sm" onClick={onRefresh}>
            Actualiser
          </Button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cartes
// ---------------------------------------------------------------------------

function AppCard({ data }: { data: Monitoring }) {
  const app = data.app;
  return (
    <Card
      title="Application"
      subtitle="Version installée et schéma de base attendu."
      action={<Pill tone={app.db_revision_ok ? "ok" : "danger"}>{app.db_revision_ok ? "Schéma à jour" : "Schéma différent"}</Pill>}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Version" value={app.version} />
        <Field label="Environnement" value={app.environment} />
        <Field label="Version du code déployé" value={app.build_sha ?? "—"} />
        <Field label="Mise en service" value={app.build_date ? formatDateTime(app.build_date) : "—"} />
        <Field label="Schéma attendu" value={app.expected_db_revision} />
        <Field label="Schéma en place" value={app.current_db_revision ?? "—"} />
        <Field label="En service depuis" value={formatUptime(app.uptime_seconds)} />
      </div>
    </Card>
  );
}

function DatabaseCard({ data }: { data: Monitoring }) {
  const db = data.database;
  return (
    <Card
      title="Base de données"
      subtitle="Réponse, volumétrie et nombre de tables."
      action={<Pill tone={db.ok ? "ok" : "danger"}>{db.ok ? "Accessible" : "Injoignable"}</Pill>}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Temps de réponse" value={db.latency_ms === null ? "—" : `${db.latency_ms} ms`} />
        <Field label="Taille" value={formatBytes(db.size_bytes)} />
        <Field label="Tables" value={formatCount(db.tables_count)} />
      </div>
    </Card>
  );
}

function BackupsCard({ data }: { data: Monitoring }) {
  const backups = data.backups;
  const last = backups.last;
  return (
    <Card
      title="Sauvegardes"
      subtitle="Dernière sauvegarde connue et place disponible."
      action={<Pill tone={backups.stale ? "warn" : "ok"}>{backups.stale ? "Sauvegarde à refaire" : "À jour"}</Pill>}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label="Dernière sauvegarde"
          value={last ? `${formatRelativeTime(last.created_at)} (${formatDateTime(last.created_at)})` : "Aucune"}
          mono={false}
        />
        <Field label="Résultat" value={last ? (last.status === "success" ? "Réussie" : "Échouée") : "—"} mono={false} />
        <Field label="Taille" value={formatBytes(last?.size_bytes ?? null)} />
        <Field label="Sauvegarde nocturne" value={<YesNo value={backups.nightly_enabled} yes="Activée" no="Désactivée" />} mono={false} />
        <Field label="Espace libre" value={formatBytes(backups.dir_free_bytes)} />
      </div>
    </Card>
  );
}

function JobsCard({ data }: { data: Monitoring }) {
  const jobs = data.jobs ?? [];
  return (
    <Card title="Tâches planifiées" subtitle="Ce que la caisse fait toute seule, et comment ça s'est passé.">
      {jobs.length === 0 ? (
        <p className="text-sm text-fc-ink-soft">Aucune tâche planifiée.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-fc-ink-mute uppercase text-xs tracking-wide">
                <th className="py-2 pr-4">Tâche</th>
                <th className="py-2 pr-4">Prochaine</th>
                <th className="py-2 pr-4">Dernière</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.name} className="border-t border-fc-line align-top">
                  <td className="py-2 pr-4">
                    <div className="text-fc-ink">{jobLabel(job)}</div>
                    {job.cron && <div className="font-mono text-xs text-fc-ink-mute">{job.cron}</div>}
                  </td>
                  <td className="py-2 pr-4 whitespace-nowrap">
                    {job.next_run_at ? formatDateTime(job.next_run_at) : <span className="text-fc-ink-mute">—</span>}
                  </td>
                  <td className="py-2 pr-4">
                    {job.last_run ? (
                      <div className="space-y-1">
                        <Pill tone={job.last_run.status === "ok" ? "ok" : "danger"}>
                          {job.last_run.status === "ok" ? "Réussie" : "Échouée"}
                        </Pill>
                        <div className="text-xs text-fc-ink-soft">{formatDateTime(job.last_run.at)}</div>
                        {job.last_run.detail && (
                          <div className="text-xs text-fc-ink-mute break-words">{job.last_run.detail}</div>
                        )}
                      </div>
                    ) : (
                      <span className="text-fc-ink-mute">Jamais exécutée</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function ChainRow({ label, chain }: { label: string; chain: MonitoringIntegrityChain | null }) {
  const count = chain?.count ?? chain?.checked ?? null;
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-fc-lg border border-fc-line p-3">
      <span className="text-sm font-medium text-fc-ink">{label}</span>
      {chain ? (
        <Pill tone={chain.valid ? "ok" : "danger"}>{chain.valid ? "Intacte" : "Rompue"}</Pill>
      ) : (
        <Pill tone="neutral">Jamais vérifiée</Pill>
      )}
      {chain && count !== null && (
        <span className="ml-auto font-mono text-xs tabular-nums text-fc-ink-mute">{formatCount(count)} lignes</span>
      )}
    </div>
  );
}

function IntegrityCard({
  data,
  checking,
  error,
  onCheck,
}: {
  data: Monitoring;
  checking: boolean;
  error: DisplayableError;
  onCheck: () => void;
}) {
  const integrity = data.integrity;
  return (
    <Card
      title="Intégrité"
      subtitle="Chaînes du journal, des ventes et des clôtures."
      action={
        <Button variant="outline" size="sm" onClick={onCheck} disabled={checking} aria-busy={checking}>
          {checking ? "Vérification…" : "Vérifier maintenant"}
        </Button>
      }
    >
      <div className="space-y-3">
        {errorText(error) && (
          <div role="alert" className="rounded-fc bg-fc-danger-soft border border-fc-danger/30 px-3 py-2 text-sm text-fc-danger">
            {errorText(error)}
            <ErrorReference reference={errorRef(error)} />
          </div>
        )}
        <ChainRow label="Journal des événements" chain={integrity?.jet ?? null} />
        <ChainRow label="Ventes et rapports de clôture" chain={integrity?.fiscal ?? null} />
        <ChainRow label="Clôtures mensuelles et annuelles" chain={integrity?.closures ?? null} />
        <p className="text-xs text-fc-ink-mute">
          {integrity?.jet?.checked_at
            ? `Dernière vérification ${formatRelativeTime(integrity.jet.checked_at)} (${formatDateTime(integrity.jet.checked_at)}).`
            : "La vérification n'est pas lancée automatiquement : elle relit toutes les lignes."}
        </p>
      </div>
    </Card>
  );
}

function ExternalCard({ data }: { data: Monitoring }) {
  const { sumup, brevo, openweather } = data.external;
  const readerTone: Tone = !sumup.configured ? "neutral" : sumup.last_ping?.ready ? "ok" : "warn";
  const readerLabel = !sumup.configured
    ? "Non configuré"
    : sumup.last_ping
      ? sumup.last_ping.ready
        ? "Prêt"
        : "Non prêt"
      : "Jamais sollicité";
  return (
    <Card title="Services externes" subtitle="Ce dont la caisse dépend en dehors d'elle-même.">
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-3 rounded-fc-lg border border-fc-line p-3">
          <span className="text-sm font-medium text-fc-ink">Terminal de paiement</span>
          <Pill tone={readerTone}>{readerLabel}</Pill>
          {sumup.configured && (
            <span className="text-xs text-fc-ink-mute">
              {sumup.reader_configured ? "Envoi direct au terminal" : "Sans envoi direct"}
              {sumup.last_ping ? ` — vu ${formatRelativeTime(sumup.last_ping.at)}` : ""}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3 rounded-fc-lg border border-fc-line p-3">
          <span className="text-sm font-medium text-fc-ink">Envoi des tickets par e-mail</span>
          <Pill tone={brevo.configured ? "ok" : "neutral"}>{brevo.configured ? "Configuré" : "Non configuré"}</Pill>
        </div>
        <div className="flex flex-wrap items-center gap-3 rounded-fc-lg border border-fc-line p-3">
          <span className="text-sm font-medium text-fc-ink">Météo</span>
          <Pill tone={openweather.configured ? "ok" : "neutral"}>{openweather.configured ? "Configurée" : "Non configurée"}</Pill>
          {openweather.cache_age_seconds !== null && (
            <span className="text-xs text-fc-ink-mute">Relevé d&apos;il y a {Math.round(openweather.cache_age_seconds / 60)} min</span>
          )}
        </div>
      </div>
    </Card>
  );
}

const PRINTER_MODE_LABELS: Record<string, string> = {
  network: "Réseau",
  webusb: "USB sur la tablette",
  none: "Aucune",
  unknown: "Inconnu",
};

function PrinterCard({ data }: { data: Monitoring }) {
  const printer = data.printer;
  const tone: Tone = printer.online === null ? "neutral" : printer.online ? "ok" : "warn";
  const label = printer.online === null ? "État inconnu" : printer.online ? "En ligne" : "Hors ligne";
  return (
    <Card title="Imprimante" subtitle="Ticket de caisse." action={<Pill tone={tone}>{label}</Pill>}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Raccordement" value={PRINTER_MODE_LABELS[printer.mode] ?? printer.mode} mono={false} />
        <Field label="Temps de réponse" value={printer.latency_ms === null ? "—" : `${printer.latency_ms} ms`} />
      </div>
      {printer.mode === "webusb" && (
        <p className="mt-3 text-xs text-fc-ink-mute">
          Une imprimante branchée en USB sur la tablette n&apos;est pas visible depuis le serveur : son état se teste
          depuis l&apos;onglet Matériel.
        </p>
      )}
    </Card>
  );
}

function QueuesCard({ data }: { data: Monitoring }) {
  const queues = data.queues;
  const pending = queues.failed_payments_pending;
  return (
    <Card
      title="Files d'attente"
      subtitle="Ce qui reste à traiter à la main."
      action={<Pill tone={pending ? "warn" : "ok"}>{pending ? "À traiter" : "Rien en attente"}</Pill>}
    >
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Paiements carte à relancer" value={formatCount(queues.failed_payments_pending)} />
        <Field label="Échanges terminal en erreur (24 h)" value={formatCount(queues.sumup_exchange_errors_24h)} />
        <Field label="Suppressions clientes à exécuter" value={formatCount(queues.clients_deletion_due)} />
      </div>
    </Card>
  );
}

function RecentErrorsCard({ data }: { data: Monitoring }) {
  const errors = data.recent_errors ?? [];
  const [copied, setCopied] = useState<string | null>(null);

  const handleCopy = async (requestId: string): Promise<void> => {
    const ok = await copyToClipboard(requestId);
    setCopied(ok ? requestId : null);
    if (ok) setTimeout(() => setCopied(null), 2000);
  };

  return (
    <Card
      title="Dernières erreurs"
      subtitle="Les cinquante dernières pannes du serveur, avec la référence à rapprocher de ce qu'a vu la vendeuse."
    >
      {errors.length === 0 ? (
        <p className="text-sm text-fc-ink-soft">Aucune erreur enregistrée depuis le démarrage.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-fc-ink-mute uppercase text-xs tracking-wide">
                <th className="py-2 pr-4">Quand</th>
                <th className="py-2 pr-4">Référence</th>
                <th className="py-2 pr-4">Appel</th>
                <th className="py-2 pr-4">Code</th>
                <th className="py-2 pr-4">Cause</th>
              </tr>
            </thead>
            <tbody>
              {errors.map((item, index) => (
                <tr key={`${item.request_id}-${index}`} className="border-t border-fc-line align-top">
                  <td className="py-2 pr-4 whitespace-nowrap" title={formatDateTime(item.at)}>
                    {formatRelativeTime(item.at)}
                  </td>
                  <td className="py-2 pr-4">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs text-fc-ink">{item.request_id}</span>
                      <button
                        type="button"
                        onClick={() => void handleCopy(item.request_id)}
                        className="min-h-touch inline-flex items-center rounded-fc px-2 text-xs text-fc-primary hover:bg-fc-primary-soft"
                      >
                        {copied === item.request_id ? "Copiée" : "Copier"}
                      </button>
                    </div>
                  </td>
                  <td className="py-2 pr-4 font-mono text-xs break-all">
                    {item.method} {item.path}
                  </td>
                  <td className="py-2 pr-4 font-mono tabular-nums">{item.status}</td>
                  <td className="py-2 pr-4 text-xs text-fc-ink-soft break-words">{item.error_type ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
