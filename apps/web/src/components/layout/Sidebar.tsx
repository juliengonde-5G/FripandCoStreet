"use client";

/**
 * Barre latérale du back-office (PR7, docs/ARCHITECTURE_PR7.md §1, I1).
 *
 * Un seul composant, rendu par toutes les pages sauf la caisse (plein
 * écran, voir components/pos/PosTopBar.tsx) et la connexion. Il remplace
 * l'ancien `AppShell` et sa barre horizontale.
 *
 *   - fixe à gauche, 256 px étendue / 64 px réduite (bouton chevron) ;
 *   - état réduit persisté dans localStorage + reflété sur
 *     `document.body.dataset.sidebar`, ce qui permet à la règle globale de
 *     `globals.css` de ramener le `md:ml-64` des pages à 4 rem sans que
 *     celles-ci aient à connaître l'état de la barre ;
 *   - sous 768 px : bouton hamburger fixe, voile sombre et tiroir glissant,
 *     refermé dès qu'on touche une entrée.
 *
 * Les entrées d'administration pointent vers `/admin?tab=…` : l'onglet
 * actif est déterminé par `matchQuery` (l'absence de `?tab=` vaut
 * « settings », onglet par défaut de la page).
 */
import React, { Suspense, useCallback, useEffect, useState } from "react";
import Image from "next/image";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { getUsername } from "@/lib/auth";
import { logout } from "@/lib/logout";

/** Clé de persistance de l'état réduit/étendu (lisible, non sensible). */
const STORAGE_KEY = "fripco_sidebar_state";

// ---------------------------------------------------------------------------
// Icônes — SVG inline 22 px, trait 2, style « Feather ». Aucune dépendance.
// ---------------------------------------------------------------------------

type IconName =
  | "home"
  | "cash"
  | "users"
  | "settings"
  | "printer"
  | "accounting"
  | "archive"
  | "backup"
  | "logout"
  | "chevron-left"
  | "chevron-right"
  | "menu"
  | "close";

const ICON_PATHS: Record<IconName, React.ReactNode> = {
  home: (
    <>
      <path d="M3 9.5 12 3l9 6.5V20a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z" />
    </>
  ),
  cash: (
    <>
      <path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z" />
      <path d="M3 6h18" />
      <path d="M16 10a4 4 0 0 1-8 0" />
    </>
  ),
  users: (
    <>
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </>
  ),
  printer: (
    <>
      <path d="M6 9V2h12v7" />
      <path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2" />
      <path d="M6 14h12v8H6z" />
    </>
  ),
  accounting: (
    <>
      <path d="M18 20V10" />
      <path d="M12 20V4" />
      <path d="M6 20v-6" />
    </>
  ),
  archive: (
    <>
      <path d="M21 8v13H3V8" />
      <path d="M1 3h22v5H1z" />
      <path d="M10 12h4" />
    </>
  ),
  backup: (
    <>
      <path d="M12 8c4.97 0 9-1.34 9-3s-4.03-3-9-3-9 1.34-9 3 4.03 3 9 3z" />
      <path d="M21 12c0 1.66-4.03 3-9 3s-9-1.34-9-3" />
      <path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
    </>
  ),
  logout: (
    <>
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="M16 17l5-5-5-5" />
      <path d="M21 12H9" />
    </>
  ),
  "chevron-left": <path d="M15 18 9 12l6-6" />,
  "chevron-right": <path d="m9 18 6-6-6-6" />,
  menu: (
    <>
      <path d="M3 6h18" />
      <path d="M3 12h18" />
      <path d="M3 18h18" />
    </>
  ),
  close: (
    <>
      <path d="M18 6 6 18" />
      <path d="m6 6 12 12" />
    </>
  ),
};

function Icon({ name, size = 22, className = "" }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={`flex-shrink-0 ${className}`}
    >
      {ICON_PATHS[name]}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Entrées de navigation (I1 — ordre et libellés contractuels)
// ---------------------------------------------------------------------------

/** Onglet par défaut d'`/admin` : `?tab=` absent ⇒ « Réglages » actif. */
const DEFAULT_ADMIN_TAB = "settings";

interface NavItem {
  href: string;
  label: string;
  icon: IconName;
  /** Correspondance exacte du chemin (« / » ne doit pas tout allumer). */
  exact?: boolean;
  /** Entrées discriminées par la chaîne de requête (`/admin?tab=…`). */
  matchQuery?: { tab: string };
}

interface NavGroup {
  title: string;
  items: NavItem[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    title: "Pilotage",
    items: [{ href: "/", label: "Accueil", icon: "home", exact: true }],
  },
  {
    title: "Commerce",
    items: [
      { href: "/caisse", label: "Caisse", icon: "cash" },
      { href: "/admin?tab=clients", label: "Clients", icon: "users", matchQuery: { tab: "clients" } },
    ],
  },
  {
    title: "Administration",
    items: [
      { href: "/admin?tab=settings", label: "Réglages", icon: "settings", matchQuery: { tab: "settings" } },
      { href: "/admin?tab=hardware", label: "Matériel", icon: "printer", matchQuery: { tab: "hardware" } },
      { href: "/admin?tab=accounting", label: "Comptabilité", icon: "accounting", matchQuery: { tab: "accounting" } },
      { href: "/admin?tab=fiscal", label: "Archives fiscales", icon: "archive", matchQuery: { tab: "fiscal" } },
      { href: "/admin?tab=backups", label: "Sauvegardes", icon: "backup", matchQuery: { tab: "backups" } },
    ],
  },
];

/** Chemin (sans requête) d'une entrée. */
function pathOf(href: string): string {
  const i = href.indexOf("?");
  return i === -1 ? href : href.slice(0, i);
}

function isActive(item: NavItem, pathname: string | null, currentTab: string | null): boolean {
  const base = pathOf(item.href);
  const samePath = item.exact ? pathname === base : !!pathname && (pathname === base || pathname.startsWith(`${base}/`));
  if (!samePath) return false;
  if (!item.matchQuery) return true;
  return (currentTab ?? DEFAULT_ADMIN_TAB) === item.matchQuery.tab;
}

// ---------------------------------------------------------------------------
// Barre latérale
// ---------------------------------------------------------------------------

/**
 * Silhouette affichée pendant le rendu différé (`Suspense`) : même largeur
 * que la barre réelle, pour éviter tout saut de mise en page.
 */
export function SidebarFallback() {
  return (
    <div
      aria-hidden
      className="fixed inset-y-0 left-0 z-40 hidden w-64 flex-col border-r border-fc-line bg-fc-surface md:flex"
    />
  );
}

export default function Sidebar() {
  return (
    <Suspense fallback={<SidebarFallback />}>
      <SidebarInner />
    </Suspense>
  );
}

function SidebarInner() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const currentTab = searchParams.get("tab");

  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [username, setUsername] = useState<string | null>(null);
  const [loggingOut, setLoggingOut] = useState(false);

  // État persisté — lu après l'hydratation (localStorage n'existe pas côté
  // serveur), puis réécrit à chaque changement.
  useEffect(() => {
    setUsername(getUsername());
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(STORAGE_KEY);
    } catch {
      // Navigation privée / stockage refusé : on reste étendu.
    }
    if (stored === "collapsed") setCollapsed(true);
  }, []);

  useEffect(() => {
    const state = collapsed ? "collapsed" : "expanded";
    document.body.dataset.sidebar = state;
    try {
      localStorage.setItem(STORAGE_KEY, state);
    } catch {
      // Stockage indisponible : l'état reste valable pour la session.
    }
  }, [collapsed]);

  // Le tiroir mobile ne survit pas à un changement de page.
  useEffect(() => {
    setMobileOpen(false);
  }, [pathname, currentTab]);

  const handleLogout = useCallback(async () => {
    setLoggingOut(true);
    await logout();
    router.push("/login");
  }, [router]);

  const width = collapsed ? "md:w-16" : "md:w-64";

  return (
    <>
      {/* Ouverture du tiroir sous 768 px. */}
      <button
        type="button"
        onClick={() => setMobileOpen(true)}
        aria-label="Ouvrir le menu"
        aria-expanded={mobileOpen}
        className="fixed left-4 top-4 z-50 flex min-h-[48px] min-w-[48px] items-center justify-center rounded-lg border border-fc-line bg-white text-fc-ink shadow-md md:hidden"
      >
        <Icon name="menu" />
      </button>

      {mobileOpen && (
        <div
          role="presentation"
          onClick={() => setMobileOpen(false)}
          className="fixed inset-0 z-40 bg-black/30 md:hidden"
        />
      )}

      <nav
        aria-label="Navigation principale"
        className={`fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-fc-line bg-fc-surface transition-all duration-200 md:translate-x-0 ${width} ${
          mobileOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {/* Réduire / étendre — desktop seulement. */}
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          aria-label={collapsed ? "Étendre le menu" : "Réduire le menu"}
          title={collapsed ? "Étendre le menu" : "Réduire le menu"}
          className="absolute -right-3 top-20 z-10 hidden h-6 w-6 min-h-0 min-w-0 items-center justify-center rounded-full border border-fc-line bg-white text-fc-ink-soft shadow-sm hover:text-fc-primary md:flex"
        >
          <Icon name={collapsed ? "chevron-right" : "chevron-left"} size={14} />
        </button>

        {/* Fermeture du tiroir mobile. */}
        <button
          type="button"
          onClick={() => setMobileOpen(false)}
          aria-label="Fermer le menu"
          className="absolute right-2 top-2 flex h-10 w-10 min-h-0 min-w-0 items-center justify-center rounded-lg text-fc-ink-soft hover:bg-fc-bg md:hidden"
        >
          <Icon name="close" size={20} />
        </button>

        {/* Tête — logo + nom de la boutique. */}
        <div className="flex-shrink-0 border-b border-fc-line px-6 pb-5 pt-6 text-center">
          <Image
            src="/brand/logo-mark.png"
            alt=""
            aria-hidden
            width={56}
            height={56}
            priority
            className={`mx-auto object-contain ${collapsed ? "h-14 w-14 md:h-8 md:w-8" : "h-14 w-14"}`}
          />
          <div className={collapsed ? "md:hidden" : ""}>
            <div className="mt-3 truncate text-sm font-semibold text-fc-ink">Frip &amp; Co Street</div>
            <div className="mt-0.5 text-[10px] uppercase tracking-[0.25em] text-fc-ink-mute">Back office</div>
          </div>
        </div>

        {/* Groupes d'entrées. */}
        <div className="flex-1 overflow-y-auto px-3 py-4">
          {NAV_GROUPS.map((group) => (
            <div key={group.title} className="mb-5 last:mb-0">
              <div
                className={`px-4 pb-2 text-[10px] font-semibold uppercase tracking-[0.22em] text-fc-ink-mute ${
                  collapsed ? "md:hidden" : ""
                }`}
              >
                {group.title}
              </div>
              <ul className="space-y-0.5">
                {group.items.map((item) => {
                  const active = isActive(item, pathname, currentTab);
                  return (
                    <li key={item.href}>
                      <Link
                        href={item.href}
                        title={item.label}
                        aria-current={active ? "page" : undefined}
                        onClick={() => setMobileOpen(false)}
                        className={`relative flex min-h-[48px] items-center gap-3 rounded-xl px-4 py-2.5 text-sm font-medium transition-colors ${
                          active ? "bg-fc-bg text-fc-primary" : "text-fc-ink-soft hover:bg-fc-bg"
                        } ${collapsed ? "md:justify-center md:px-2" : ""}`}
                      >
                        {active && (
                          <span
                            aria-hidden
                            className="absolute bottom-2 left-0 top-2 w-1 rounded-r-full bg-fc-primary"
                          />
                        )}
                        <Icon name={item.icon} className={active ? "text-fc-primary" : ""} />
                        <span className={`truncate ${collapsed ? "md:hidden" : ""}`}>{item.label}</span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>

        {/* Pied — utilisateur connecté + déconnexion. */}
        <div className="flex-shrink-0 border-t border-fc-line p-3">
          <div
            className={`mb-2 flex items-center gap-3 rounded-xl bg-fc-bg px-3 py-2 ${collapsed ? "md:justify-center md:px-1" : ""}`}
            title={username ?? undefined}
          >
            <span className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-fc-primary text-sm font-semibold uppercase text-white">
              {(username ?? "?").trim().charAt(0) || "?"}
            </span>
            <span className={`truncate text-sm font-medium text-fc-ink ${collapsed ? "md:hidden" : ""}`}>
              {username ?? "Connectée"}
            </span>
          </div>
          <button
            type="button"
            onClick={() => void handleLogout()}
            disabled={loggingOut}
            title="Se déconnecter"
            className={`flex min-h-[48px] w-full items-center gap-3 rounded-xl px-4 py-2.5 text-sm font-medium text-fc-ink-mute transition-colors hover:bg-red-50 hover:text-red-600 disabled:opacity-60 ${
              collapsed ? "md:justify-center md:px-2" : ""
            }`}
          >
            <Icon name="logout" />
            <span className={`truncate ${collapsed ? "md:hidden" : ""}`}>
              {loggingOut ? "Déconnexion…" : "Se déconnecter"}
            </span>
          </button>
        </div>
      </nav>
    </>
  );
}
