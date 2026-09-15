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
import Image from "next/image";
import Link from "next/link";

import RequireAuth from "@/components/layout/RequireAuth";
import { api, ApiError } from "@/lib/api";
import { formatCurrency, formatDate } from "@/lib/format";
import type { DashboardDay, DashboardResponse, ShopSettings } from "@/lib/types";

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
  const [shopName, setShopName] = useState<string>("Frip & Co Street");
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

  useEffect(() => {
    // Nom de la boutique pour l'en-tête — chargement non bloquant : un échec
    // laisse simplement le nom par défaut plutôt que de casser l'écran.
    api
      .get<ShopSettings>("/api/admin/settings/shop")
      .then((s) => {
        if (s?.name) setShopName(s.name);
      })
      .catch(() => undefined);
  }, []);

  const today = data?.today;
  const month = data?.month;
  const days = data?.last_7_days ?? [];
  const dailyTarget = amount(today?.target);
  const monthTarget = amount(month?.target);

  return (
    <RequireAuth>
      <div className="flex min-h-screen flex-col bg-fc-bg">
        <header className="flex-shrink-0 border-b border-fc-line bg-fc-surface">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-3 px-4 py-3">
            <div className="flex min-w-0 items-center gap-2">
              <Image
                src="/brand/logo-mark.png"
                alt=""
                aria-hidden
                width={40}
                height={40}
                className="h-10 w-10 flex-shrink-0 rounded-fc"
              />
              <span className="truncate font-semibold text-fc-ink">{shopName}</span>
            </div>

            <div className="flex-1" />

            <Link
              href="/admin"
              className="inline-flex min-h-touch items-center rounded-fc border border-fc-line bg-fc-surface px-4 py-2 text-sm font-medium text-fc-ink hover:bg-fc-bg-alt"
            >
              Administration
            </Link>
            <Link
              href="/caisse"
              className="inline-flex min-h-[56px] flex-1 items-center justify-center rounded-fc bg-fc-primary px-6 py-3 text-lg font-semibold text-white transition-colors hover:bg-fc-primary-deep focus:outline-none focus:ring-2 focus:ring-fc-primary focus:ring-offset-2 focus:ring-offset-fc-bg sm:flex-none"
            >
              Aller à la caisse
            </Link>
          </div>
        </header>

        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-5 sm:py-6">
          <h1 className="sr-only">Tableau de bord</h1>

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
        </main>
      </div>
    </RequireAuth>
  );
}
