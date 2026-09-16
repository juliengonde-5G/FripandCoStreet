"use client";

/**
 * Cahier du jour (PR11, docs/ARCHITECTURE_PR11.md §1, M5).
 *
 * Le cahier qu'on tenait au comptoir sur un carnet : l'objectif de la
 * journée, ce qui a été fait, le temps qu'il fait, l'an dernier à la même
 * date, le message du jour, l'opération en cours et les deux signatures.
 * Une page par journée, feuilletée avec ‹ et ›.
 *
 * Rien n'est calculé ici : l'objectif du jour, la progression et la météo
 * viennent du serveur, qui les fige à la première lecture d'une journée —
 * un objectif mensuel modifié en cours de route ne réécrit donc pas les
 * pages déjà tournées. Le front se contente d'afficher, d'enregistrer les
 * deux textes libres et d'apposer les signatures.
 *
 * Journée révolue : les textes et les signatures sont fermés (le serveur
 * répond 409 `day_closed`), l'écran le dit et désactive les champs plutôt
 * que de laisser saisir pour rien.
 *
 * Contraintes de rendu : lisible à 400 px comme à 1024 px, aucun
 * défilement horizontal, cibles tactiles confortables, et une impression
 * A4 propre — sans barre latérale ni boutons (`@media print` ci-dessous).
 */
import React, { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api";
import {
  CAHIER_TEXT_MAX_LENGTH,
  cahierAmount,
  fetchCahierDay,
  formatCahierLongDate,
  shiftCahierDay,
  signCahier,
  todayCahierDay,
  updateCahierText,
  weatherFamily,
  type CahierDay,
  type CahierHour,
  type CahierWeather,
} from "@/lib/cahier";
import { formatCurrency, formatDateTime } from "@/lib/format";

// ---------------------------------------------------------------------------
// Impression — feuille A4 lisible (M5)
// ---------------------------------------------------------------------------

/**
 * L'impression tombe sur la page telle qu'elle est affichée : on retire
 * donc la navigation (barre latérale, hamburger), les boutons et les
 * champs de saisie inutiles sur papier, et on rend au contenu toute la
 * largeur de la feuille. Les règles vivent avec le composant plutôt que
 * dans la feuille globale : elles ne concernent que cette page.
 */
const PRINT_CSS = `
@media print {
  @page { size: A4 portrait; margin: 14mm; }
  html, body { background: #fff !important; }
  nav[aria-label="Navigation principale"],
  button[aria-label="Ouvrir le menu"] { display: none !important; }
  main { margin-left: 0 !important; padding: 0 !important; }
  [data-cahier-noprint] { display: none !important; }
  [data-cahier-sheet] { max-width: none !important; }
  [data-cahier-panel] {
    break-inside: avoid;
    border-color: #999 !important;
    box-shadow: none !important;
  }
  [data-cahier-text] {
    min-height: 22mm;
    white-space: pre-wrap;
    background: #fff !important;
    color: #000 !important;
    resize: none;
  }
}
`;

// ---------------------------------------------------------------------------
// Aides de présentation — mêmes conventions que le tableau de bord d'accueil
// ---------------------------------------------------------------------------

/** Montant du serveur (chaîne à deux décimales) → euros affichables. */
function euros(value: string | number | null | undefined): string {
  const parsed = cahierAmount(value);
  return parsed === null ? "—" : formatCurrency(parsed);
}

function percentLabel(pct: number | null | undefined): string {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return "—";
  return `${pct.toFixed(1).replace(".", ",")} %`;
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return count > 1 ? pluralForm : singular;
}

/** « 14 h » — libellé d'une colonne du mini-graphique. */
function hourLabel(hour: number): string {
  return `${hour} h`;
}

// ---------------------------------------------------------------------------
// Briques
// ---------------------------------------------------------------------------

function Panel({
  title,
  subtitle,
  children,
  className = "",
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      data-cahier-panel
      className={`rounded-fc-lg border border-fc-line bg-fc-surface p-4 sm:p-6 ${className}`}
    >
      <div className="mb-4">
        <h2 className="text-lg font-semibold leading-tight text-fc-ink">{title}</h2>
        {subtitle && <p className="mt-0.5 text-sm text-fc-ink-soft">{subtitle}</p>}
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

/** Barre de progression — `pct` vient du serveur, on ne fait que plafonner
 * le remplissage visuel à 100 %. */
function Progress({ pct, label }: { pct: number | null; label: string }) {
  const value = pct ?? 0;
  const filled = Math.max(0, Math.min(100, value));
  const reached = value >= 100;
  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="text-sm text-fc-ink-soft">{label}</span>
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
// Mini-graphique par heure — SVG inline, aucune bibliothèque
// ---------------------------------------------------------------------------

const HOUR_COL = 100;
const HOUR_HEIGHT = 120;
const HOUR_BAR = 60;
const HOUR_BAR_MIN = 3;

/**
 * Fenêtre d'heures affichée : la journée de boutique (8 h → 20 h), élargie
 * aux heures où quelque chose a été encaissé. Afficher les 24 heures
 * rendrait les colonnes illisibles sur un téléphone.
 */
function hourWindow(byHour: CahierHour[]): CahierHour[] {
  const net = new Map<number, number>();
  for (const entry of byHour) {
    net.set(entry.hour, cahierAmount(entry.net) ?? 0);
  }
  const busy = [...net.entries()].filter(([, value]) => value > 0).map(([hour]) => hour);
  const first = Math.min(8, ...(busy.length ? busy : [8]));
  const last = Math.max(20, ...(busy.length ? busy : [20]));
  const out: CahierHour[] = [];
  for (let hour = first; hour <= last; hour++) {
    out.push({ hour, net: (net.get(hour) ?? 0).toFixed(2) });
  }
  return out;
}

function HourChart({ byHour }: { byHour: CahierHour[] }) {
  const hours = hourWindow(byHour);
  const values = hours.map((h) => cahierAmount(h.net) ?? 0);
  const max = Math.max(...values, 0);
  const total = values.reduce((sum, value) => sum + value, 0);

  if (total <= 0) {
    return (
      <p className="rounded-fc bg-fc-bg-alt px-3 py-2 text-sm text-fc-ink-soft">
        Aucune vente encaissée pour l&apos;instant sur cette journée.
      </p>
    );
  }

  return (
    <div>
      <svg
        viewBox={`0 0 ${HOUR_COL * Math.max(hours.length, 1)} ${HOUR_HEIGHT}`}
        preserveAspectRatio="none"
        className="h-24 w-full sm:h-28"
        role="list"
        aria-label="Ventes nettes heure par heure"
      >
        {hours.map((entry, index) => {
          const value = values[index];
          const ratio = max > 0 ? value / max : 0;
          const height = Math.max(HOUR_BAR_MIN, Math.round(ratio * (HOUR_HEIGHT - 8)));
          const x = index * HOUR_COL + (HOUR_COL - HOUR_BAR) / 2;
          const description = `${hourLabel(entry.hour)} : ${euros(entry.net)}`;
          return (
            <g key={entry.hour} role="listitem" aria-label={description}>
              <title>{description}</title>
              <rect
                x={x}
                y={HOUR_HEIGHT - height}
                width={HOUR_BAR}
                height={height}
                className={value > 0 ? "fill-fc-primary/70" : "fill-fc-line"}
              />
            </g>
          );
        })}
      </svg>
      <div
        className="mt-2 grid"
        style={{ gridTemplateColumns: `repeat(${Math.max(hours.length, 1)}, minmax(0, 1fr))` }}
      >
        {hours.map((entry) => (
          <div key={entry.hour} className="px-0.5 text-center">
            <div className="truncate text-[10px] text-fc-ink-soft sm:text-[11px]">{entry.hour}</div>
          </div>
        ))}
      </div>
      <p className="mt-1 text-xs text-fc-ink-mute">Heure de la journée.</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Météo du jour (M3) — instantané figé au premier affichage de la journée
// ---------------------------------------------------------------------------

/**
 * Glyphe météo dessiné sur place d'après le code d'icône OpenWeather
 * (« 01d », « 10n »…) — jamais l'image distante : la page doit s'afficher
 * entière sans accès sortant, à l'impression comprise. Même dessin que le
 * widget de l'accueil ; chaque écran porte sa copie plutôt qu'une
 * dépendance croisée, comme les autres petites aides d'affichage.
 */
function WeatherGlyph({ code }: { code: string | null | undefined }) {
  const { family, night } = weatherFamily(code);
  const sun = (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </>
  );
  const moon = <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />;
  const cloud = <path d="M7 19h10a4 4 0 0 0 .3-8A6 6 0 0 0 5.7 12 3.5 3.5 0 0 0 7 19z" />;
  const drops = <path d="M9 20.5 8 22.5M13 20.5 12 22.5M17 20.5 16 22.5" />;

  let shape: React.ReactNode;
  if (family === "01") shape = night ? moon : sun;
  else if (family === "02" || family === "03" || family === "04") shape = cloud;
  else if (family === "09" || family === "10")
    shape = (
      <>
        {cloud}
        {drops}
      </>
    );
  else if (family === "11")
    shape = (
      <>
        {cloud}
        <path d="M13 13l-3 4h4l-3 4" />
      </>
    );
  else if (family === "13")
    shape = (
      <>
        {cloud}
        <path d="M9 21h.01M13 21h.01M17 21h.01" />
      </>
    );
  else if (family === "50") shape = <path d="M3 8h18M3 12h18M6 16h12M8 20h8" />;
  else shape = cloud;

  return (
    <svg
      width={44}
      height={44}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className="shrink-0 text-fc-primary"
    >
      {shape}
    </svg>
  );
}

function WeatherLine({ weather }: { weather: CahierWeather | null }) {
  if (!weather || weather.unavailable) {
    return (
      <p className="text-sm text-fc-ink-soft">
        Météo indisponible
        {weather?.reason ? <span className="text-fc-ink-mute"> — {weather.reason}</span> : null}
      </p>
    );
  }
  const temp = typeof weather.temp === "number" ? `${Math.round(weather.temp)} °C` : "—";
  return (
    <div className="flex items-center gap-3">
      <WeatherGlyph code={weather.icon} />
      <div className="min-w-0">
        <div className="font-mono text-2xl tabular-nums leading-none text-fc-ink">{temp}</div>
        <div className="mt-1 truncate text-sm text-fc-ink-soft first-letter:uppercase">
          {weather.description ?? "—"}
          {weather.city ? <span className="text-fc-ink-mute"> · {weather.city}</span> : null}
        </div>
        <div className="mt-0.5 text-xs text-fc-ink-mute">
          {typeof weather.temp_min === "number" && typeof weather.temp_max === "number"
            ? `De ${Math.round(weather.temp_min)} à ${Math.round(weather.temp_max)} °C`
            : null}
          {typeof weather.wind_speed === "number"
            ? `${typeof weather.temp_min === "number" ? " · " : ""}Vent ${Math.round(weather.wind_speed)} m/s`
            : null}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

type TextField = "message" | "operation";

export default function CahierDuJour() {
  const [day, setDay] = useState<string>(() => todayCahierDay());
  const [data, setData] = useState<CahierDay | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Brouillons des deux textes libres : ce qui est dans les champs tant
  // que la personne écrit. L'enregistrement part à la perte de focus.
  const [drafts, setDrafts] = useState<Record<TextField, string>>({ message: "", operation: "" });
  const [savingField, setSavingField] = useState<TextField | null>(null);
  const [savedField, setSavedField] = useState<TextField | null>(null);
  const [textError, setTextError] = useState<string | null>(null);

  const [teamName, setTeamName] = useState("");
  const [signing, setSigning] = useState<"manager" | "team" | null>(null);
  const [signError, setSignError] = useState<string | null>(null);

  const requestId = useRef(0);

  const load = useCallback(async (target: string): Promise<void> => {
    const id = ++requestId.current;
    setLoading(true);
    try {
      const result = await fetchCahierDay(target);
      if (id !== requestId.current) return;
      setData(result);
      setDrafts({ message: result.message ?? "", operation: result.operation ?? "" });
      setError(null);
    } catch (err) {
      if (id !== requestId.current) return;
      setData(null);
      setError(err instanceof ApiError ? err.detail : "Impossible d'ouvrir le cahier de cette journée.");
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    setTextError(null);
    setSignError(null);
    setSavedField(null);
    void load(day);
  }, [day, load]);

  const isToday = day === todayCahierDay();
  const closed = Boolean(data?.is_past);

  /** Enregistre un texte libre s'il a changé (perte de focus). */
  const saveText = async (field: TextField): Promise<void> => {
    if (!data || closed) return;
    const value = drafts[field];
    const current = (field === "message" ? data.message : data.operation) ?? "";
    if (value === current) return;
    setSavingField(field);
    setTextError(null);
    try {
      const updated = await updateCahierText(day, { [field]: value });
      setData(updated);
      setDrafts({ message: updated.message ?? "", operation: updated.operation ?? "" });
      setSavedField(field);
      window.setTimeout(() => setSavedField((f) => (f === field ? null : f)), 2500);
    } catch (err) {
      setTextError(err instanceof ApiError ? err.detail : "Impossible d'enregistrer ce texte.");
    } finally {
      setSavingField(null);
    }
  };

  const sign = async (role: "manager" | "team"): Promise<void> => {
    if (!data || closed) return;
    setSigning(role);
    setSignError(null);
    try {
      const updated = await signCahier(day, role, role === "team" ? teamName : undefined);
      setData(updated);
      if (role === "team") setTeamName("");
    } catch (err) {
      setSignError(err instanceof ApiError ? err.detail : "Impossible d'apposer la signature.");
    } finally {
      setSigning(null);
    }
  };

  const target = data?.target;
  const realized = data?.realized;

  return (
    <div data-cahier-sheet className="mx-auto w-full max-w-5xl">
      <style>{PRINT_CSS}</style>

      {/* ------------------------------------------------------- En-tête */}
      <div className="mb-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-2xl font-bold text-fc-ink">Cahier du jour</h1>
            <p className="mt-1 text-sm text-fc-ink-soft">{formatCahierLongDate(day)}</p>
          </div>
          <button
            type="button"
            data-cahier-noprint
            onClick={() => window.print()}
            className="inline-flex min-h-touch items-center justify-center rounded-fc border border-fc-line bg-fc-surface px-4 py-2 text-sm font-semibold text-fc-ink transition-colors hover:bg-fc-bg-alt focus:outline-none focus:ring-2 focus:ring-fc-primary focus:ring-offset-2"
          >
            Imprimer
          </button>
        </div>

        <div data-cahier-noprint className="mt-4 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setDay((d) => shiftCahierDay(d, -1))}
            aria-label="Journée précédente"
            className="inline-flex min-h-touch min-w-touch items-center justify-center rounded-fc border border-fc-line bg-fc-surface px-4 text-lg text-fc-ink transition-colors hover:bg-fc-bg-alt focus:outline-none focus:ring-2 focus:ring-fc-primary"
          >
            ‹
          </button>
          <button
            type="button"
            onClick={() => setDay(todayCahierDay())}
            disabled={isToday}
            className={`inline-flex min-h-touch items-center justify-center rounded-fc px-4 py-2 text-sm font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-fc-primary ${
              isToday
                ? "border border-fc-line bg-fc-bg-alt text-fc-ink-mute"
                : "border border-fc-line bg-fc-surface text-fc-ink hover:bg-fc-bg-alt"
            }`}
          >
            Aujourd&apos;hui
          </button>
          <button
            type="button"
            onClick={() => setDay((d) => shiftCahierDay(d, 1))}
            aria-label="Journée suivante"
            className="inline-flex min-h-touch min-w-touch items-center justify-center rounded-fc border border-fc-line bg-fc-surface px-4 text-lg text-fc-ink transition-colors hover:bg-fc-bg-alt focus:outline-none focus:ring-2 focus:ring-fc-primary"
          >
            ›
          </button>
          <label className="ml-auto flex items-center gap-2 text-sm text-fc-ink-soft">
            <span className="sr-only sm:not-sr-only">Aller au</span>
            <input
              type="date"
              value={day}
              onChange={(e) => {
                if (e.target.value) setDay(e.target.value);
              }}
              className="min-h-touch rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-sm text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary"
            />
          </label>
        </div>

        {data && !data.is_open && (
          <p className="mt-3 rounded-fc bg-fc-bg-alt px-3 py-2 text-sm text-fc-ink-soft">
            Journée de fermeture : aucun objectif n&apos;est réparti sur ce jour.
          </p>
        )}
        {closed && (
          <p className="mt-3 rounded-fc bg-fc-bg-alt px-3 py-2 text-sm text-fc-ink-soft">
            Journée close : elle se relit, elle ne se modifie plus.
          </p>
        )}
      </div>

      {error && (
        <div
          role="alert"
          className="mb-5 rounded-fc border border-fc-danger/30 bg-fc-danger-soft px-3 py-2 text-sm text-fc-danger"
        >
          {error}
        </div>
      )}

      {loading && !data ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : !data || !target || !realized ? (
        !error && <p className="text-sm text-fc-ink-soft">Aucune donnée pour cette journée.</p>
      ) : (
        <div className="space-y-5">
          <div className="grid gap-5 lg:grid-cols-2">
            {/* --------------------------------------------------- Objectif */}
            <Panel title="Objectif" subtitle="Part du mois revenant à cette journée.">
              <div className="space-y-4">
                <div>
                  <div className="font-mono text-4xl font-semibold tabular-nums leading-none text-fc-ink sm:text-5xl">
                    {euros(target.daily)}
                  </div>
                  <div className="mt-1.5 text-sm text-fc-ink-soft">
                    {cahierAmount(target.monthly)
                      ? <>Objectif du mois : {euros(target.monthly)}</>
                      : <>Aucun objectif du mois n&apos;est fixé.</>}
                  </div>
                </div>

                <Progress pct={target.month_progress_pct} label="Avancement du mois" />

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
                  <Stat label="Réalisé du mois" value={euros(target.month_realized)} />
                  <Stat label="Reste à faire" value={euros(target.month_remaining)} />
                  <Stat
                    label="Requis par jour"
                    value={euros(target.required_daily_rest_of_month)}
                    hint="Sur les jours d'ouverture restants"
                  />
                </div>
              </div>
            </Panel>

            {/* --------------------------------------------------- Réalisé */}
            <Panel title="Réalisé" subtitle="Ventes nettes de la journée (annulations déduites).">
              <div className="space-y-4">
                <div>
                  <div className="font-mono text-4xl font-semibold tabular-nums leading-none text-fc-ink sm:text-5xl">
                    {euros(realized.net)}
                  </div>
                  <div className="mt-1.5 text-sm text-fc-ink-soft">
                    {realized.sales_count} {plural(realized.sales_count, "vente")} · panier moyen{" "}
                    <span className="font-mono tabular-nums text-fc-ink">{euros(realized.average_basket)}</span>
                  </div>
                </div>

                {cahierAmount(target.daily) ? (
                  <Progress pct={realized.progress_pct} label="Avancement de la journée" />
                ) : (
                  <p className="rounded-fc bg-fc-bg-alt px-3 py-2 text-sm text-fc-ink-soft">
                    Aucun objectif sur cette journée : seul le réalisé est affiché.
                  </p>
                )}

                <HourChart byHour={realized.by_hour} />
              </div>
            </Panel>
          </div>

          <div className="grid gap-5 lg:grid-cols-2">
            {/* ----------------------------------------------------- Météo */}
            <Panel title="Météo" subtitle="Relevé du matin, conservé avec la journée.">
              <WeatherLine weather={data.weather} />
            </Panel>

            {/* ------------------------------------------------------- N-1 */}
            <Panel title="L'an dernier" subtitle="La même journée, un an plus tôt.">
              {data.previous_year ? (
                <div>
                  <div className="font-mono text-2xl tabular-nums text-fc-ink">
                    {euros(data.previous_year.net)}
                  </div>
                  <p className="mt-1 text-sm text-fc-ink-soft">{formatCahierLongDate(data.previous_year.day)}</p>
                </div>
              ) : (
                <p className="text-sm text-fc-ink-soft">
                  Pas de repère pour cette date : la boutique n&apos;avait pas encore un an d&apos;historique.
                </p>
              )}
            </Panel>
          </div>

          {/* ------------------------------------------- Message et opération */}
          <Panel title="Le mot du jour" subtitle="Ce qu'on veut retenir de cette journée, et ce qui tourne en boutique.">
            <div className="space-y-4">
              {textError && (
                <div
                  role="alert"
                  className="rounded-fc border border-fc-danger/30 bg-fc-danger-soft px-3 py-2 text-sm text-fc-danger"
                >
                  {textError}
                </div>
              )}

              {(
                [
                  {
                    field: "message" as TextField,
                    label: "Message du jour",
                    placeholder: "Une consigne, une humeur, un mot pour l'équipe…",
                  },
                  {
                    field: "operation" as TextField,
                    label: "Opération en cours",
                    placeholder: "Braderie, − 20 % sur les manteaux, collecte…",
                  },
                ]
              ).map(({ field, label, placeholder }) => (
                <div key={field}>
                  <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
                    <label htmlFor={`cahier-${field}`} className="text-sm font-medium text-fc-ink">
                      {label}
                    </label>
                    <span className="text-xs text-fc-ink-mute" aria-live="polite">
                      {savingField === field
                        ? "Enregistrement…"
                        : savedField === field
                          ? "Enregistré"
                          : `${drafts[field].length} / ${CAHIER_TEXT_MAX_LENGTH}`}
                    </span>
                  </div>
                  <textarea
                    id={`cahier-${field}`}
                    data-cahier-text
                    value={drafts[field]}
                    maxLength={CAHIER_TEXT_MAX_LENGTH}
                    rows={3}
                    disabled={closed}
                    placeholder={closed ? "" : placeholder}
                    onChange={(e) => setDrafts((d) => ({ ...d, [field]: e.target.value }))}
                    onBlur={() => void saveText(field)}
                    className="w-full rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-sm text-fc-ink placeholder:text-fc-ink-mute focus:outline-none focus:ring-2 focus:ring-fc-primary disabled:bg-fc-bg-alt disabled:text-fc-ink-soft"
                  />
                </div>
              ))}

              {!closed && (
                <p data-cahier-noprint className="text-xs text-fc-ink-mute">
                  Chaque texte s&apos;enregistre dès que vous quittez le champ.
                </p>
              )}
            </div>
          </Panel>

          {/* ------------------------------------------------------ Signatures */}
          <Panel title="Signatures" subtitle="La journée est relue et signée, comme sur le cahier papier.">
            <div className="space-y-4">
              {signError && (
                <div
                  role="alert"
                  className="rounded-fc border border-fc-danger/30 bg-fc-danger-soft px-3 py-2 text-sm text-fc-danger"
                >
                  {signError}
                </div>
              )}

              <div className="grid gap-4 sm:grid-cols-2">
                {/* Manager */}
                <div className="rounded-fc border border-fc-line p-4">
                  <div className="text-[11px] font-medium uppercase tracking-[0.12em] text-fc-ink-soft">
                    Manager
                  </div>
                  {data.signatures.manager ? (
                    <p className="mt-2 text-sm text-fc-ink">
                      Signé par{" "}
                      <span className="font-semibold">{data.signatures.manager.username}</span> le{" "}
                      <span className="font-mono tabular-nums">
                        {formatDateTime(data.signatures.manager.at)}
                      </span>
                    </p>
                  ) : (
                    <>
                      <p className="mt-2 text-sm text-fc-ink-soft">Pas encore signé.</p>
                      <button
                        type="button"
                        data-cahier-noprint
                        onClick={() => void sign("manager")}
                        disabled={closed || signing !== null}
                        className="mt-3 inline-flex min-h-touch items-center justify-center rounded-fc bg-fc-primary px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-fc-primary-deep focus:outline-none focus:ring-2 focus:ring-fc-primary focus:ring-offset-2 disabled:bg-fc-line disabled:text-fc-ink-mute"
                      >
                        {signing === "manager" ? "Signature…" : "Signer (manager)"}
                      </button>
                    </>
                  )}
                </div>

                {/* Équipe */}
                <div className="rounded-fc border border-fc-line p-4">
                  <div className="text-[11px] font-medium uppercase tracking-[0.12em] text-fc-ink-soft">
                    Équipe
                  </div>
                  {data.signatures.team ? (
                    <p className="mt-2 text-sm text-fc-ink">
                      Signé par <span className="font-semibold">{data.signatures.team.name}</span> le{" "}
                      <span className="font-mono tabular-nums">{formatDateTime(data.signatures.team.at)}</span>
                    </p>
                  ) : (
                    <>
                      <p className="mt-2 text-sm text-fc-ink-soft">Pas encore signé.</p>
                      <div data-cahier-noprint className="mt-3 space-y-2">
                        <label htmlFor="cahier-team-name" className="block text-xs text-fc-ink-mute">
                          Nom (si aucune vendeuse n&apos;est identifiée sur le tiroir)
                        </label>
                        <input
                          id="cahier-team-name"
                          type="text"
                          value={teamName}
                          maxLength={60}
                          disabled={closed}
                          onChange={(e) => setTeamName(e.target.value)}
                          placeholder="Prénom de la personne"
                          className="w-full min-h-touch rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-sm text-fc-ink placeholder:text-fc-ink-mute focus:outline-none focus:ring-2 focus:ring-fc-primary disabled:bg-fc-bg-alt"
                        />
                        <button
                          type="button"
                          onClick={() => void sign("team")}
                          disabled={closed || signing !== null}
                          className="inline-flex min-h-touch items-center justify-center rounded-fc bg-fc-primary px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-fc-primary-deep focus:outline-none focus:ring-2 focus:ring-fc-primary focus:ring-offset-2 disabled:bg-fc-line disabled:text-fc-ink-mute"
                        >
                          {signing === "team" ? "Signature…" : "Signer (équipe)"}
                        </button>
                      </div>
                    </>
                  )}
                </div>
              </div>
            </div>
          </Panel>
        </div>
      )}
    </div>
  );
}
