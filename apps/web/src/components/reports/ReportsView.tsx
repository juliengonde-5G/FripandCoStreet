"use client";

/**
 * Rapports par période (PR11, docs/ARCHITECTURE_PR11.md §1, M5).
 *
 * Un seul écran pour les trois granularités du contrat M1 : jour, semaine
 * ISO (lundi → dimanche) et mois. Le sélecteur choisit la granularité, les
 * chevrons ‹ › déplacent la période d'un pas, le champ de date permet
 * d'aller directement sur une période précise.
 *
 * Lecture seule : rien ici n'écrit une vente, un Z ou une ligne de
 * journal. Le seul geste tracé est l'export CSV, et il l'est côté serveur
 * (JET `export.downloaded`).
 *
 * Contraintes de rendu :
 *   - lisible à 400 px (téléphone) comme à 1024 px (tablette de caisse) ;
 *   - graphique en barres SVG inline, sans bibliothèque, même style que
 *     `SevenDayChart` (accueil) ;
 *   - imprimable : `@media print` masque la navigation et les commandes,
 *     le contenu tient en pleine largeur sur une feuille A4.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import { ApiError } from "@/lib/api";
import { formatCurrency, formatDateTime } from "@/lib/format";
import {
  REPORT_PERIOD_KINDS,
  REPORT_PERIOD_LABELS,
  convertPeriodValue,
  defaultPeriodValue,
  downloadReportCsv,
  fetchReport,
  parseAmount,
  parseIsoDay,
  shiftPeriodValue,
  type Report,
  type ReportPeriodKind,
} from "@/lib/reports";

// ---------------------------------------------------------------------------
// Aides de présentation
// ---------------------------------------------------------------------------

function euros(value: string | null | undefined): string {
  return formatCurrency(parseAmount(value));
}

function eurosInt(value: string | null | undefined): string {
  return formatCurrency(parseAmount(value), { decimals: 0 });
}

function percentLabel(pct: number | null | undefined): string {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return "—";
  return `${pct.toFixed(1).replace(".", ",")} %`;
}

function signedPercentLabel(pct: number): string {
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(1).replace(".", ",")} %`;
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return count > 1 ? pluralForm : singular;
}

/** « lundi 14 septembre » — infobulle et `aria-label` d'une barre. */
function longDayLabel(iso: string): string {
  const d = parseIsoDay(iso);
  if (!d) return iso;
  return d.toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" });
}

/** « lun. 14 » — libellé sous une barre du graphique quotidien. */
function shortDayLabel(iso: string): string {
  const d = parseIsoDay(iso);
  if (!d) return iso;
  return d.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric" });
}

/** « 14 » — quantième seul, seul libellé qui tienne sous une barre quand
 * le mois entier en aligne trente-et-une. */
function compactDayLabel(iso: string): string {
  const d = parseIsoDay(iso);
  if (!d) return iso;
  return String(d.getDate());
}

/** Quantième horaire seul sous la barre : « 12h » ne tient pas dans un
 * vingt-quatrième de la largeur d'un téléphone. Le titre du panneau
 * (« Heure par heure ») dit déjà de quoi il s'agit. */
function hourLabel(hour: number): string {
  return String(hour);
}

/** Bornes de la période, en clair sous le titre. */
function periodRangeLabel(from: string, to: string): string {
  const a = parseIsoDay(from);
  const b = parseIsoDay(to);
  if (!a || !b) return `${from} → ${to}`;
  // « 1er », jamais « 1 » : c'est la seule irrégularité du français ici.
  const dayOf = (d: Date) => (d.getDate() === 1 ? "1er" : String(d.getDate()));
  const monthOf = (d: Date) => d.toLocaleDateString("fr-FR", { month: "long" });
  if (from === to) return `${dayOf(a)} ${monthOf(a)} ${a.getFullYear()}`;
  return `du ${dayOf(a)} ${monthOf(a)} au ${dayOf(b)} ${monthOf(b)} ${b.getFullYear()}`;
}

// ---------------------------------------------------------------------------
// Briques communes (même vocabulaire visuel que l'accueil)
// ---------------------------------------------------------------------------

function Panel({
  title,
  subtitle,
  action,
  children,
  className = "",
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    /* `min-w-0` : sans lui, une carte placée dans une grille prend la
       largeur de son contenu le plus large (le tableau « Par vendeuse » et
       ses 520 px) et fait défiler la page entière à 400 px. Avec, c'est le
       tableau seul qui défile dans son cadre. */
    <section
      className={`fc-print-panel min-w-0 rounded-fc-lg border border-fc-line bg-fc-surface p-4 sm:p-6 ${className}`}
    >
      <div className="mb-4 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold leading-tight text-fc-ink">{title}</h2>
          {subtitle && <p className="mt-0.5 text-sm text-fc-ink-soft">{subtitle}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-fc border border-fc-line p-3">
      <div className="text-[11px] font-medium uppercase tracking-[0.12em] text-fc-ink-soft">{label}</div>
      <div className="mt-1 font-mono text-base tabular-nums text-fc-ink">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-fc-ink-mute">{hint}</div>}
    </div>
  );
}

/** Barre de progression vers un objectif — `pct` vient du serveur, on ne
 * fait que plafonner le remplissage visuel à 100 %. */
function TargetProgress({ pct, amount, label }: { pct: number; amount: string; label: string }) {
  const filled = Math.max(0, Math.min(100, pct));
  const reached = pct >= 100;
  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="text-sm text-fc-ink-soft">
          Objectif : <span className="font-mono tabular-nums text-fc-ink">{euros(amount)}</span>
        </span>
        <span className={`text-sm font-semibold tabular-nums ${reached ? "text-fc-success" : "text-fc-ink"}`}>
          {percentLabel(pct)}
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(filled)}
        aria-valuetext={percentLabel(pct)}
        className="h-3 w-full overflow-hidden rounded-full bg-fc-bg-alt"
      >
        <div
          className={`h-full rounded-full transition-[width] duration-500 ${reached ? "bg-fc-success" : "bg-fc-primary"}`}
          style={{ width: `${filled}%` }}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Graphique en barres — SVG inline, aucune bibliothèque
// ---------------------------------------------------------------------------

/** Un point du graphique, quelle que soit la granularité. */
interface BarPoint {
  key: string;
  /** Libellé court sous la barre. */
  label: string;
  /** Libellé long pour l'infobulle et le lecteur d'écran. */
  longLabel: string;
  value: number;
  salesCount: number;
}

const CHART_COLS = 100;
const CHART_HEIGHT = 160;
/** Hauteur minimale d'une barre : une période à 0 € reste visible sans
 * jamais suggérer un chiffre d'affaires. */
const BAR_MIN = 3;

function BarChart({ points, highlightKey }: { points: BarPoint[]; highlightKey?: string }) {
  if (points.length === 0) {
    return <p className="text-sm text-fc-ink-soft">Aucune donnée sur la période.</p>;
  }
  const max = Math.max(...points.map((p) => p.value), 0);
  // Au-delà d'une dizaine de barres, les libellés se chevauchent : on n'en
  // garde qu'un sur deux (mois de 28 à 31 jours, journée de 24 heures).
  const labelStep = points.length > 16 ? 3 : points.length > 10 ? 2 : 1;
  const barWidth = points.length > 16 ? 70 : 56;

  return (
    <div>
      <svg
        viewBox={`0 0 ${CHART_COLS * points.length} ${CHART_HEIGHT}`}
        preserveAspectRatio="none"
        className="h-28 w-full sm:h-40"
        role="list"
        aria-label="Ventes nettes de la période"
      >
        {points.map((point, i) => {
          const ratio = max > 0 ? point.value / max : 0;
          const height = Math.max(BAR_MIN, Math.round(ratio * (CHART_HEIGHT - 8)));
          const x = i * CHART_COLS + (CHART_COLS - barWidth) / 2;
          const description = `${point.longLabel} : ${formatCurrency(point.value)}, ${point.salesCount} ${plural(
            point.salesCount,
            "vente",
          )}`;
          const highlighted = highlightKey !== undefined && point.key === highlightKey;
          return (
            <g key={point.key} role="listitem" aria-label={description}>
              <title>{description}</title>
              <rect
                x={x}
                y={CHART_HEIGHT - height}
                width={barWidth}
                height={height}
                className={
                  point.value > 0 ? (highlighted ? "fill-fc-primary" : "fill-fc-primary/55") : "fill-fc-line"
                }
              />
            </g>
          );
        })}
      </svg>
      {/* Libellés sous les barres — même découpage en colonnes que le SVG. */}
      <div className="mt-2 grid" style={{ gridTemplateColumns: `repeat(${points.length}, minmax(0, 1fr))` }}>
        {points.map((point, i) => (
          <div key={point.key} className="px-0.5 text-center">
            <div className="truncate text-[10px] text-fc-ink-soft sm:text-[11px]">
              {i % labelStep === 0 ? point.label : " "}
            </div>
            {points.length <= 10 && (
              <div className="truncate font-mono text-[11px] tabular-nums text-fc-ink">
                {formatCurrency(point.value, { decimals: 0 })}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function buildPoints(report: Report): { points: BarPoint[]; title: string; subtitle: string } {
  if (report.by_hour && report.by_hour.length > 0) {
    return {
      title: "Heure par heure",
      subtitle: "Ventes nettes de la journée, annulations déduites.",
      points: report.by_hour.map((h) => ({
        key: `h${h.hour}`,
        label: hourLabel(h.hour),
        longLabel: `${h.hour}h — ${h.hour + 1}h`,
        value: parseAmount(h.net),
        salesCount: h.sales_count,
      })),
    };
  }
  const days = report.by_day ?? [];
  const compact = days.length > 10;
  return {
    title: "Jour par jour",
    subtitle: "Ventes nettes par jour, annulations déduites.",
    points: days.map((d) => ({
      key: d.date,
      label: compact ? compactDayLabel(d.date) : shortDayLabel(d.date),
      longLabel: longDayLabel(d.date),
      value: parseAmount(d.net),
      salesCount: d.sales_count,
    })),
  };
}

// ---------------------------------------------------------------------------
// Sélecteur de période
// ---------------------------------------------------------------------------

function Chevron({ direction }: { direction: "left" | "right" }) {
  return (
    <svg
      width={18}
      height={18}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {direction === "left" ? <path d="M15 18 9 12l6-6" /> : <path d="m9 18 6-6-6-6" />}
    </svg>
  );
}

function PeriodPicker({
  kind,
  value,
  onKindChange,
  onValueChange,
  busy,
}: {
  kind: ReportPeriodKind;
  value: string;
  onKindChange: (next: ReportPeriodKind) => void;
  onValueChange: (next: string) => void;
  busy: boolean;
}) {
  return (
    <div className="fc-print-hide mb-5 flex flex-wrap items-center gap-3">
      <div
        role="group"
        aria-label="Granularité du rapport"
        className="flex flex-wrap items-center gap-1 rounded-fc-lg bg-fc-bg-alt p-1"
      >
        {REPORT_PERIOD_KINDS.map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => onKindChange(k)}
            aria-pressed={kind === k}
            className={`min-h-touch rounded-fc px-4 py-2 text-sm font-medium transition-colors ${
              kind === k ? "bg-fc-surface text-fc-primary-deep shadow-sm" : "text-fc-ink-soft hover:text-fc-ink"
            }`}
          >
            {REPORT_PERIOD_LABELS[k]}
          </button>
        ))}
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => onValueChange(shiftPeriodValue(kind, value, -1))}
          disabled={busy}
          aria-label="Période précédente"
          title="Période précédente"
          className="flex h-12 w-12 min-h-0 min-w-0 items-center justify-center rounded-fc border border-fc-line bg-fc-surface text-fc-ink-soft transition-colors hover:text-fc-primary disabled:opacity-50"
        >
          <Chevron direction="left" />
        </button>
        <label className="sr-only" htmlFor="periode">
          {kind === "monthly" ? "Mois du rapport" : "Date du rapport"}
        </label>
        <input
          id="periode"
          type={kind === "monthly" ? "month" : "date"}
          value={value}
          onChange={(e) => {
            if (e.target.value) onValueChange(e.target.value);
          }}
          className="min-h-touch rounded-fc border border-fc-line bg-fc-surface px-3 py-2 font-mono text-sm tabular-nums text-fc-ink focus:border-fc-primary focus:outline-none focus:ring-2 focus:ring-fc-primary/30"
        />
        <button
          type="button"
          onClick={() => onValueChange(shiftPeriodValue(kind, value, 1))}
          disabled={busy}
          aria-label="Période suivante"
          title="Période suivante"
          className="flex h-12 w-12 min-h-0 min-w-0 items-center justify-center rounded-fc border border-fc-line bg-fc-surface text-fc-ink-soft transition-colors hover:text-fc-primary disabled:opacity-50"
        >
          <Chevron direction="right" />
        </button>
      </div>

      <button
        type="button"
        onClick={() => onValueChange(defaultPeriodValue(kind))}
        className="min-h-touch rounded-fc px-3 py-2 text-sm font-medium text-fc-primary underline underline-offset-2 hover:text-fc-primary-deep"
      >
        {kind === "monthly" ? "Ce mois" : "Aujourd'hui"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tableaux
// ---------------------------------------------------------------------------

function CashierTable({ rows }: { rows: Report["by_cashier"] }) {
  if (rows.length === 0) {
    return <p className="text-sm text-fc-ink-soft">Aucune vente sur la période.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[520px] text-sm">
        <thead>
          <tr className="border-b border-fc-line text-left text-[11px] uppercase tracking-[0.12em] text-fc-ink-soft">
            <th scope="col" className="py-2 pr-3 font-medium">Vendeuse</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">Ventes</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">Total ventes</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">Annulations</th>
            <th scope="col" className="py-2 text-right font-medium">Net</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.cashier_id ?? `sans-vendeuse-${row.display_name}`} className="border-b border-fc-line/60 last:border-0">
              <td className="py-2 pr-3 text-fc-ink">{row.display_name}</td>
              <td className="py-2 pr-3 text-right font-mono tabular-nums text-fc-ink-soft">{row.sales_count}</td>
              <td className="py-2 pr-3 text-right font-mono tabular-nums text-fc-ink">{euros(row.sales_total)}</td>
              <td className="py-2 pr-3 text-right font-mono tabular-nums text-fc-ink-soft">
                {row.refunds_count > 0 ? `${row.refunds_count} · ${euros(row.refunds_total)}` : "—"}
              </td>
              <td className="py-2 text-right font-mono font-semibold tabular-nums text-fc-ink">{euros(row.net_total)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TopItemsTable({ rows }: { rows: Report["top_items"] }) {
  if (rows.length === 0) {
    return <p className="text-sm text-fc-ink-soft">Aucun article vendu sur la période.</p>;
  }
  const max = Math.max(...rows.map((r) => parseAmount(r.net)), 0);
  return (
    <ul className="space-y-2">
      {rows.map((row, index) => {
        const value = parseAmount(row.net);
        const width = max > 0 ? Math.max(2, Math.round((value / max) * 100)) : 0;
        return (
          <li key={`${row.label}-${index}`}>
            <div className="flex flex-wrap items-baseline justify-between gap-x-3">
              <span className="text-sm text-fc-ink">
                <span className="mr-2 font-mono text-xs text-fc-ink-mute">{index + 1}.</span>
                {row.label}
              </span>
              <span className="font-mono text-sm tabular-nums text-fc-ink">
                {row.quantity} × · {euros(row.net)}
              </span>
            </div>
            <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-fc-bg-alt">
              <div className="h-full rounded-full bg-fc-primary/55" style={{ width: `${width}%` }} />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function ZList({ rows }: { rows: Report["z_reports"] }) {
  if (rows.length === 0) {
    return <p className="text-sm text-fc-ink-soft">Aucune clôture sur la période.</p>;
  }
  return (
    <ul className="divide-y divide-fc-line/60">
      {rows.map((z) => (
        <li key={z.report_number} className="flex flex-wrap items-baseline justify-between gap-x-3 py-2">
          <span className="text-sm text-fc-ink">
            Z n° <span className="font-mono tabular-nums">{z.report_number}</span>
          </span>
          <span className="text-sm text-fc-ink-soft">{formatDateTime(z.closed_at)}</span>
          <span className="font-mono text-sm tabular-nums text-fc-ink">{euros(z.net)}</span>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Écran
// ---------------------------------------------------------------------------

export default function ReportsView() {
  const [kind, setKind] = useState<ReportPeriodKind>("daily");
  const [value, setValue] = useState<string>(() => defaultPeriodValue("daily"));
  const [report, setReport] = useState<Report | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  // Une réponse arrivée après un changement de période ne doit jamais
  // écraser la période affichée : chaque appel porte son numéro d'ordre.
  const requestSeq = useRef(0);

  useEffect(() => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setExportError(null);
    fetchReport(kind, value)
      .then((data) => {
        if (seq !== requestSeq.current) return;
        setReport(data);
        setError(null);
      })
      .catch((err: unknown) => {
        if (seq !== requestSeq.current) return;
        setReport(null);
        setError(err instanceof ApiError ? err.detail : "Impossible de charger le rapport.");
      })
      .finally(() => {
        if (seq === requestSeq.current) setLoading(false);
      });
  }, [kind, value]);

  const changeKind = useCallback(
    (next: ReportPeriodKind) => {
      setValue((current) => convertPeriodValue(kind, next, current));
      setKind(next);
    },
    [kind],
  );

  const handleExport = useCallback(async () => {
    setExporting(true);
    setExportError(null);
    try {
      await downloadReportCsv(kind, value, report?.period.from);
    } catch (err) {
      setExportError(err instanceof ApiError ? err.detail : "Le téléchargement du rapport a échoué.");
    } finally {
      setExporting(false);
    }
  }, [kind, value, report]);

  const totals = report?.totals;
  const previous = report?.previous;
  const delta = previous?.delta_pct ?? null;
  const chart = report ? buildPoints(report) : null;

  return (
    <div className="mx-auto w-full max-w-6xl">
      {/* Feuille d'impression : la navigation, les commandes et les cadres
          d'interaction disparaissent, le contenu reprend toute la largeur.
          Portée à cette page — le style n'est monté qu'avec elle. */}
      <style>{`
        @media print {
          .fixed { display: none !important; }
          nav[aria-label="Navigation principale"] { display: none !important; }
          main { margin-left: 0 !important; padding: 0 !important; }
          .fc-print-hide { display: none !important; }
          .fc-print-panel { break-inside: avoid; page-break-inside: avoid; border-color: #b9b8b2; }
          body { background: #fff !important; }
        }
      `}</style>

      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-fc-ink">Rapports</h1>
          {report && (
            <p className="mt-0.5 text-sm text-fc-ink-soft">
              <span className="font-medium text-fc-ink">{report.period.label}</span>
              {/* Le libellé du serveur porte déjà les dates d'un jour
                  (« mardi 15 septembre 2026 ») et d'une semaine (« semaine
                  du 14 au 20 septembre 2026 ») : seules les bornes d'un
                  mois valent d'être ajoutées. */}
              {report.period.kind === "monthly" && (
                <> — {periodRangeLabel(report.period.from, report.period.to)}</>
              )}
            </p>
          )}
        </div>
        <div className="fc-print-hide flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => window.print()}
            className="min-h-touch rounded-fc border border-fc-line bg-fc-surface px-4 py-2 text-sm font-medium text-fc-ink-soft transition-colors hover:text-fc-primary"
          >
            Imprimer
          </button>
          <button
            type="button"
            onClick={() => void handleExport()}
            disabled={exporting || !report}
            className="min-h-touch rounded-fc bg-fc-primary px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-fc-primary-deep focus:outline-none focus:ring-2 focus:ring-fc-primary focus:ring-offset-2 focus:ring-offset-fc-bg disabled:opacity-60"
          >
            {exporting ? "Export en cours…" : "Exporter (CSV)"}
          </button>
        </div>
      </div>

      <PeriodPicker kind={kind} value={value} onKindChange={changeKind} onValueChange={setValue} busy={loading} />

      {exportError && (
        <div
          role="alert"
          className="mb-5 rounded-fc border border-fc-danger/30 bg-fc-danger-soft px-3 py-2 text-sm text-fc-danger"
        >
          {exportError}
        </div>
      )}

      {error && (
        <div
          role="alert"
          className="mb-5 rounded-fc border border-fc-danger/30 bg-fc-danger-soft px-3 py-2 text-sm text-fc-danger"
        >
          {error}
        </div>
      )}

      {loading && !report ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : !report || !totals || !previous || !chart ? (
        !error && <p className="text-sm text-fc-ink-soft">Aucune donnée à afficher pour cette période.</p>
      ) : (
        <div className={`space-y-5 ${loading ? "opacity-60" : ""}`}>
          <div className="grid gap-5 lg:grid-cols-2">
            {/* --------------------------------------------------- Totaux */}
            <Panel title="Totaux" subtitle="Ventes nettes de la période, annulations déduites.">
              <div className="space-y-4">
                <div>
                  <div className="font-mono text-4xl font-semibold tabular-nums leading-none text-fc-ink sm:text-5xl">
                    {euros(totals.net)}
                  </div>
                  <div className="mt-1.5 text-sm text-fc-ink-soft">
                    {totals.sales_count} {plural(totals.sales_count, "vente")}
                    {totals.refunds_count > 0 && (
                      <>
                        {" · "}
                        {totals.refunds_count} {plural(totals.refunds_count, "annulation")}
                      </>
                    )}
                  </div>
                </div>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                  <Stat label="Panier moyen" value={euros(totals.average_basket)} />
                  <Stat label="Articles" value={String(totals.items_count)} hint="Lignes vendues" />
                  <Stat
                    label="Annulations"
                    value={euros(totals.refunds)}
                    hint={`Ventes brutes ${eurosInt(totals.gross)}`}
                  />
                </div>
              </div>
            </Panel>

            {/* -------------------------------- Encaissements et objectif */}
            <Panel title="Encaissements" subtitle="Répartition par moyen de paiement, annulations déduites.">
              <div className="space-y-4">
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <Stat label="Espèces" value={euros(report.payments.cash)} />
                  <Stat label="Carte" value={euros(report.payments.card)} />
                </div>

                {report.target ? (
                  <TargetProgress
                    pct={report.target.progress_pct}
                    amount={report.target.amount}
                    label="Progression vers l'objectif de la période"
                  />
                ) : (
                  <p className="rounded-fc bg-fc-bg-alt px-3 py-2 text-sm text-fc-ink-soft">
                    Aucun objectif défini pour cette période.
                    {/* Masqué à l'impression avec sa ponctuation : sur une
                        feuille, un renvoi vers un écran ne sert à rien. */}
                    <span className="fc-print-hide">
                      {" "}
                      <Link
                        href="/admin"
                        className="font-medium text-fc-primary underline underline-offset-2 hover:text-fc-primary-deep"
                      >
                        Le fixer dans Administration → Réglages
                      </Link>
                      .
                    </span>
                  </p>
                )}

                <div className="rounded-fc border border-fc-line p-3">
                  <div className="text-[11px] font-medium uppercase tracking-[0.12em] text-fc-ink-soft">
                    Période précédente
                  </div>
                  <div className="mt-1 flex flex-wrap items-baseline gap-x-3">
                    <span className="font-mono text-base tabular-nums text-fc-ink">{euros(previous.net)}</span>
                    {delta === null ? (
                      <span className="text-sm text-fc-ink-mute">variation non calculable</span>
                    ) : (
                      <span
                        className={`text-sm font-semibold tabular-nums ${
                          delta > 0 ? "text-fc-success" : delta < 0 ? "text-fc-danger" : "text-fc-ink-soft"
                        }`}
                      >
                        {signedPercentLabel(delta)}
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 text-xs text-fc-ink-mute">
                    {periodRangeLabel(previous.from, previous.to)}
                  </div>
                </div>
              </div>
            </Panel>
          </div>

          {/* ------------------------------------------------- Graphique */}
          <Panel title={chart.title} subtitle={chart.subtitle}>
            <BarChart points={chart.points} />
          </Panel>

          {/* Le tableau par vendeuse occupe toute la largeur : ses six
              colonnes ne tiennent pas dans une demi-largeur sans que la
              dernière disparaisse sous le bord de la carte. */}
          <Panel title="Par vendeuse" subtitle="Ventes, annulations et net par personne identifiée en caisse.">
            <CashierTable rows={report.by_cashier} />
          </Panel>

          <div className="grid gap-5 lg:grid-cols-2">
            <Panel title="Articles les plus vendus" subtitle="Dix libellés en tête, saisis en caisse.">
              <TopItemsTable rows={report.top_items} />
            </Panel>

            <Panel title="Clôtures de caisse" subtitle="Rapports Z scellés sur la période.">
              <ZList rows={report.z_reports} />
            </Panel>
          </div>
        </div>
      )}
    </div>
  );
}
