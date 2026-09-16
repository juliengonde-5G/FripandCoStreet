"use client";

/**
 * Tableau de bord d'accueil (PR6, docs/ARCHITECTURE_PR6.md §1, H4).
 *
 * Écran d'arrivée après connexion : ce que la boutique a fait aujourd'hui,
 * où elle en est de son objectif du mois, et la tendance des 7 derniers
 * jours. Lecture seule — aucune écriture, aucun impact sur la chaîne
 * fiscale. Les objectifs se fixent dans Administration → Réglages (H5).
 *
 * Contraintes de rendu :
 *   - lisible à 400 px (téléphone) comme à 1024 px (tablette de caisse) ;
 *   - graphique 7 jours en SVG inline, sans bibliothèque ;
 *   - rafraîchissement automatique toutes les 60 s et au retour sur l'onglet
 *     (la vendeuse laisse l'écran ouvert toute la journée).
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import { api, ApiError } from "@/lib/api";
import { formatCurrency, formatDate } from "@/lib/format";
import { fetchWeather, isWeatherAvailable, type Weather } from "@/lib/reports";
import type { DashboardDay, DashboardResponse } from "@/lib/types";

/** Intervalle de rafraîchissement automatique (H4). */
const REFRESH_MS = 60_000;

// ---------------------------------------------------------------------------
// Aides de présentation
// ---------------------------------------------------------------------------

/** Les montants du contrat H3 sont des chaînes ("0.00") : jamais de
 * `Number()` implicite ailleurs que par cette fonction. */
function amount(value: string | null | undefined): number {
  const n = Number.parseFloat(value ?? "");
  return Number.isFinite(n) ? n : 0;
}

function euros(value: string | null | undefined): string {
  return formatCurrency(amount(value));
}

/** `"2026-09-15"` → `Date` locale (jamais `new Date("YYYY-MM-DD")`, qui est
 * interprété en UTC et décale l'affichage d'un jour en soirée). */
function parseDay(iso: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

/** Libellé court sous une barre : « lun. 8 ». */
function shortDayLabel(iso: string): string {
  const d = parseDay(iso);
  if (!d) return iso;
  return d.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric" });
}

/** Libellé long pour l'infobulle et l'`aria-label` : « lundi 8 septembre ». */
function longDayLabel(iso: string): string {
  const d = parseDay(iso);
  if (!d) return iso;
  return d.toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" });
}

/** `"2026-09"` → « septembre 2026 ». */
function monthLabel(iso: string): string {
  const m = /^(\d{4})-(\d{2})$/.exec(iso);
  if (!m) return iso;
  return new Date(Number(m[1]), Number(m[2]) - 1, 1).toLocaleDateString("fr-FR", {
    month: "long",
    year: "numeric",
  });
}

function percentLabel(pct: number): string {
  if (!Number.isFinite(pct)) return "—";
  return `${pct.toFixed(1).replace(".", ",")} %`;
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return count > 1 ? pluralForm : singular;
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
    <section className={`rounded-fc-lg border border-fc-line bg-fc-surface p-4 sm:p-6 ${className}`}>
      <div className="mb-4">
        <h2 className="text-lg font-semibold leading-tight text-fc-ink">{title}</h2>
        {subtitle && <p className="mt-0.5 text-sm text-fc-ink-soft">{subtitle}</p>}
      </div>
      {children}
    </section>
  );
}

/** Bandeau affiché à la place d'une barre de progression quand aucun
 * objectif n'est fixé (H4) — renvoie vers l'écran qui permet de le poser. */
function NoTargetNotice({ what }: { what: string }) {
  return (
    <p className="rounded-fc bg-fc-bg-alt px-3 py-2 text-sm text-fc-ink-soft">
      Aucun objectif {what} défini —{" "}
      <Link href="/admin" className="font-medium text-fc-primary underline underline-offset-2 hover:text-fc-primary-deep">
        le fixer dans Administration → Réglages
      </Link>
      .
    </p>
  );
}

/** Barre de progression vers un objectif. `pct` vient du serveur (H3,
 * 0–999) : on n'en recalcule jamais la valeur côté front, on se contente de
 * plafonner le remplissage visuel à 100 %. */
function TargetProgress({ pct, target, label }: { pct: number; target: string; label: string }) {
  const filled = Math.max(0, Math.min(100, pct));
  const reached = pct >= 100;
  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="text-sm text-fc-ink-soft">
          Objectif : <span className="font-mono tabular-nums text-fc-ink">{euros(target)}</span>
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
// Météo du jour (PR11, M3) — à côté du chiffre du jour
// ---------------------------------------------------------------------------

/**
 * Glyphe météo dessiné sur place, choisi d'après le code d'icône
 * OpenWeather (« 01d », « 10n »…).
 *
 * Volontairement pas l'image distante `openweathermap.org/img/wn/…` : le
 * back-office tourne sur la tablette du comptoir, parfois sans accès
 * sortant vers ce domaine, et une icône manquante vaudrait un carré vide
 * au milieu du chiffre du jour. Le dessin local s'affiche toujours, à
 * l'impression comprise, et ne demande aucune configuration de domaine.
 */
function WeatherGlyph({ code }: { code: string }) {
  const family = (code || "").slice(0, 2);
  const night = code.endsWith("n");
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
      width={36}
      height={36}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className="flex-shrink-0 text-fc-primary"
    >
      {shape}
    </svg>
  );
}

function temperatureLabel(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${Math.round(value)} °C`;
}

/**
 * Météo locale du jour (M3). Un agrément, jamais une donnée métier :
 * indisponible, elle se replie sur une ligne discrète avec la raison
 * courte renvoyée par le serveur, et n'empêche rien d'autre de s'afficher.
 */
function WeatherWidget({ weather }: { weather: Weather | null }) {
  if (!weather) return null;
  if (!isWeatherAvailable(weather)) {
    return (
      <p className="text-xs text-fc-ink-mute">
        Météo indisponible
        {weather.reason ? ` — ${weather.reason}` : ""}
      </p>
    );
  }
  return (
    <div className="flex items-center gap-3 rounded-fc border border-fc-line px-3 py-2">
      <WeatherGlyph code={weather.icon} />
      <div className="min-w-0">
        <div className="font-mono text-lg tabular-nums leading-none text-fc-ink">
          {temperatureLabel(weather.temp)}
        </div>
        <div className="mt-1 truncate text-xs text-fc-ink-soft first-letter:uppercase">{weather.description}</div>
        <div className="mt-0.5 truncate text-[11px] text-fc-ink-mute">
          {weather.city} · {temperatureLabel(weather.temp_min)} / {temperatureLabel(weather.temp_max)}
        </div>
      </div>
    </div>
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

// ---------------------------------------------------------------------------
// Graphique 7 jours — SVG inline, aucune bibliothèque
// ---------------------------------------------------------------------------

/** Géométrie du `viewBox` : 7 colonnes de 100 unités, hauteur 160. Le SVG
 * s'étire sur toute la largeur disponible (`preserveAspectRatio="none"`,
 * sans effet de déformation sur des rectangles pleins) et reste aligné
 * colonne par colonne avec la grille HTML des libellés posée dessous. */
const CHART_COLS = 100;
const CHART_HEIGHT = 160;
const BAR_WIDTH = 56;
/** Hauteur minimale d'une barre : un jour à 0 € reste visible (7 barres
 * toujours présentes), sans jamais suggérer un chiffre d'affaires. */
const BAR_MIN = 3;

function SevenDayChart({ days }: { days: DashboardDay[] }) {
  const values = days.map((d) => amount(d.net));
  const max = Math.max(...values, 0);
  const todayIso = days.length > 0 ? days[days.length - 1].date : "";

  return (
    <div>
      <svg
        viewBox={`0 0 ${CHART_COLS * Math.max(days.length, 1)} ${CHART_HEIGHT}`}
        preserveAspectRatio="none"
        className="h-28 w-full sm:h-36"
        role="list"
        aria-label="Ventes nettes jour par jour sur les 7 derniers jours"
      >
        {days.map((day, i) => {
          const value = values[i];
          const ratio = max > 0 ? value / max : 0;
          const height = Math.max(BAR_MIN, Math.round(ratio * (CHART_HEIGHT - 8)));
          const x = i * CHART_COLS + (CHART_COLS - BAR_WIDTH) / 2;
          const isToday = day.date === todayIso;
          const description = `${longDayLabel(day.date)} : ${euros(day.net)}, ${day.sales_count} ${plural(
            day.sales_count,
            "vente",
          )}`;
          return (
            <g key={day.date} role="listitem" aria-label={description}>
              {/* <title> = infobulle native au survol (H4). */}
              <title>{description}</title>
              <rect
                x={x}
                y={CHART_HEIGHT - height}
                width={BAR_WIDTH}
                height={height}
                className={value > 0 ? (isToday ? "fill-fc-primary" : "fill-fc-primary/55") : "fill-fc-line"}
              />
            </g>
          );
        })}
      </svg>
      {/* Libellés sous chaque barre — même découpage en colonnes que le SVG,
          donc alignés quelle que soit la largeur de l'écran. */}
      <div className="mt-2 grid" style={{ gridTemplateColumns: `repeat(${Math.max(days.length, 1)}, minmax(0, 1fr))` }}>
        {days.map((day) => (
          <div key={day.date} className="px-0.5 text-center">
            <div className="truncate text-[11px] text-fc-ink-soft">{shortDayLabel(day.date)}</div>
            <div className="truncate font-mono text-[11px] tabular-nums text-fc-ink sm:text-xs">
              {formatCurrency(amount(day.net), { decimals: 0 })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function DashboardHome() {
  const [data, setData] = useState<DashboardResponse | null>(null);
  // PR11 (M3) — météo locale, chargée à part : une panne météo ne doit
  // jamais empêcher le tableau de bord de s'afficher.
  const [weather, setWeather] = useState<Weather | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Évite de repasser l'écran en « Chargement… » à chaque rafraîchissement
  // automatique : seul le tout premier chargement affiche l'état vide.
  const loadedOnce = useRef(false);

  const load = useCallback(async (): Promise<void> => {
    try {
      const result = await api.get<DashboardResponse>("/api/reports/dashboard");
      setData(result);
      setError(null);
      // Jamais dans le `try` du tableau de bord : une météo en échec
      // (route absente d'un backend plus ancien, réseau coupé) se solde par
      // l'absence de widget, pas par un bandeau d'erreur sur la page.
      void fetchWeather()
        .then(setWeather)
        .catch(() => setWeather(null));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Impossible de charger le tableau de bord.");
    } finally {
      loadedOnce.current = true;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);

  const today = data?.today;
  const month = data?.month;
  const days = data?.last_7_days ?? [];
  const dailyTarget = amount(today?.target);
  const monthTarget = amount(month?.target);

  return (
    <div className="mx-auto w-full max-w-6xl">
      {/* PR7 (I1) : l'en-tête maison a disparu au profit de la barre
          latérale ; il ne reste que le titre de page et l'accès direct à
          la caisse, geste le plus fréquent de la journée. */}
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-fc-ink">Accueil</h1>
        <Link
          href="/caisse"
          className="inline-flex min-h-[56px] items-center justify-center rounded-fc bg-fc-primary px-6 py-3 text-lg font-semibold text-white transition-colors hover:bg-fc-primary-deep focus:outline-none focus:ring-2 focus:ring-fc-primary focus:ring-offset-2 focus:ring-offset-fc-bg"
        >
          Aller à la caisse
        </Link>
      </div>

      {error && (
        <div
          role="alert"
          className="mb-5 rounded-fc border border-fc-danger/30 bg-fc-danger-soft px-3 py-2 text-sm text-fc-danger"
        >
          {error}
        </div>
      )}

      {loading && !loadedOnce.current ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : !data || !today || !month ? (
        <p className="text-sm text-fc-ink-soft">Aucune donnée à afficher pour le moment.</p>
      ) : (
        <div className="space-y-5">
          <div className="grid gap-5 lg:grid-cols-2">
            {/* ----------------------------------------------- Aujourd'hui */}
            <Panel title="Aujourd'hui" subtitle={formatDate(parseDay(today.date))}>
              <div className="space-y-4">
                {/* PR11 (M3) : la météo se pose à côté du chiffre du jour,
                    et passe dessous sur un écran étroit. */}
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="font-mono text-4xl font-semibold tabular-nums leading-none text-fc-ink sm:text-5xl">
                      {euros(today.net)}
                    </div>
                    <div className="mt-1.5 text-sm text-fc-ink-soft">
                      {today.sales_count} {plural(today.sales_count, "vente")}
                      {today.refunds_count > 0 && (
                        <>
                          {" · "}
                          {today.refunds_count} {plural(today.refunds_count, "annulation")}
                        </>
                      )}
                    </div>
                  </div>
                  <WeatherWidget weather={weather} />
                </div>

                {dailyTarget > 0 ? (
                  <TargetProgress
                    pct={today.progress_pct}
                    target={today.target}
                    label="Progression vers l'objectif du jour"
                  />
                ) : (
                  <NoTargetNotice what="journalier" />
                )}

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                  <Stat label="Panier moyen" value={euros(today.average_basket)} />
                  <Stat label="Espèces" value={euros(today.cash)} />
                  <Stat label="Carte" value={euros(today.card)} />
                </div>
              </div>
            </Panel>

            {/* -------------------------------------------------- Ce mois */}
            <Panel title="Ce mois" subtitle={monthLabel(month.month)}>
              <div className="space-y-4">
                <div>
                  <div className="font-mono text-4xl font-semibold tabular-nums leading-none text-fc-ink sm:text-5xl">
                    {euros(month.net)}
                  </div>
                  <div className="mt-1.5 text-sm text-fc-ink-soft">
                    {month.sales_count} {plural(month.sales_count, "vente")} · {month.days_open}{" "}
                    {plural(month.days_open, "jour")} d&apos;ouverture
                  </div>
                </div>

                {monthTarget > 0 ? (
                  <TargetProgress
                    pct={month.progress_pct}
                    target={month.target}
                    label="Progression vers l'objectif du mois"
                  />
                ) : (
                  <NoTargetNotice what="mensuel" />
                )}

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <Stat
                    label="Jours restants"
                    value={`${month.remaining_days} ${plural(month.remaining_days, "jour")}`}
                    hint="Jour courant inclus"
                  />
                  <Stat
                    label="Rythme nécessaire"
                    value={monthTarget > 0 ? `${euros(month.required_daily)} / jour` : "—"}
                    hint={monthTarget > 0 ? "Pour tenir l'objectif du mois" : "Aucun objectif du mois"}
                  />
                </div>
              </div>
            </Panel>
          </div>

          {/* --------------------------------------------- 7 derniers jours */}
          <Panel title="7 derniers jours" subtitle="Ventes nettes par jour (annulations déduites).">
            <SevenDayChart days={days} />
            <p className="mt-4 text-sm text-fc-ink-soft">
              {month.best_day.date ? (
                <>
                  Meilleur jour du mois :{" "}
                  <span className="font-medium text-fc-ink">{longDayLabel(month.best_day.date)}</span> —{" "}
                  <span className="font-mono tabular-nums text-fc-ink">{euros(month.best_day.net)}</span>
                </>
              ) : (
                <>Aucune vente enregistrée ce mois-ci pour l&apos;instant.</>
              )}
            </p>
          </Panel>
        </div>
      )}
    </div>
  );
}
