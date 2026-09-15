"use client";

/**
 * Barre haute de la caisse (PR7, docs/ARCHITECTURE_PR7.md §1, I2).
 *
 * La caisse reste plein écran, sans barre latérale : c'est cette barre
 * sombre qui porte à la fois l'identité, l'état du tiroir, les actions du
 * comptoir et — nouveauté PR7 — un groupe « Sortie » explicite. Avant, on
 * pouvait rester coincée dans la caisse sans savoir comment revenir à
 * l'accueil ou fermer sa session.
 *
 * Contraste : fond `fc-primary`, texte et icônes blancs (8.09:1, AAA —
 * docs/CHARTE_GRAPHIQUE.md §1.3). Aucune couleur saturée directement sur
 * ce bleu.
 *
 * Mise en page : tout tient sur une ligne dès 768 px (libellés raccourcis
 * en dessous de 1280 px) ; sous 768 px les actions basculent dans un menu
 * « ⋯ » pour laisser la place à l'état de la caisse.
 *
 * Extrait dans son propre fichier pour que `app/caisse/page.tsx` ne soit
 * touché qu'à un seul endroit.
 */
import React, { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import CashMovementButton from "@/components/pos/CashMovementButton";
import { formatCurrency } from "@/lib/format";
import { logout } from "@/lib/logout";
import type { CashierRef, CashMovementDirection, CashMovementReason } from "@/lib/types";

interface CashMovementPayload {
  direction: CashMovementDirection;
  amount: number;
  reason: CashMovementReason;
  note: string | null;
}

interface Props {
  /** Caisse ouverte : conditionne les actions et la confirmation de sortie. */
  drawerOpen: boolean;
  /** Horodatage d'ouverture du tiroir courant (ISO), si ouvert. */
  openedAt: string | null;
  salesCount: number | null;
  salesTotal: number | null;
  /** Tiroir-caisse piloté par l'imprimante (réglages matériel). */
  drawerEnabled: boolean;
  drawerKicking: boolean;
  onKickDrawer: () => void;
  onCashMovement: (payload: CashMovementPayload) => Promise<void>;
  onOpenTickets: () => void;
  onCloseDrawer: () => void;
  /** PR8 (J4) — vendeuse identifiée sur le poste, `null` si personne. */
  cashier: CashierRef | null;
  /** Relève en cours (appel serveur) : le bouton se met en attente. */
  cashierBusy?: boolean;
  /** Ouvre l'écran d'identification (« Qui encaisse ? »). */
  onIdentifyCashier: () => void;
  /** Relève : la caisse n'a plus de vendeuse. Le panier, lui, reste. */
  onReleaseCashier: () => void;
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

/** Bouton d'action de la barre sombre. */
const ACTION_CLASS =
  "inline-flex min-h-touch items-center justify-center whitespace-nowrap rounded-fc border border-white/40 bg-white/10 px-2.5 py-1.5 text-xs font-medium text-white transition-colors hover:bg-white/20 disabled:cursor-not-allowed disabled:opacity-50 xl:px-3";

/** Action accentuée — fond blanc plein sur le bleu de la barre (11,7:1).
 * Réservée à l'appel à l'action du moment : « S'identifier » tant que
 * personne n'est identifiée (PR8, J4). */
const ACCENT_ACTION_CLASS =
  "inline-flex min-h-touch items-center justify-center whitespace-nowrap rounded-fc bg-white px-3 py-1.5 text-xs font-bold text-fc-primary-deep shadow-sm transition-colors hover:bg-white/90 disabled:cursor-not-allowed disabled:opacity-50";

/** Même action, présentée en pleine largeur dans le menu « ⋯ » (fond clair). */
const MENU_ITEM_CLASS =
  "flex min-h-touch w-full items-center rounded-fc px-3 py-2 text-left text-sm font-medium text-fc-ink transition-colors hover:bg-fc-bg-alt disabled:cursor-not-allowed disabled:opacity-50";

/** Libellé long au-delà de 1280 px, raccourci en dessous (tablette 1024). */
function Label({ short, long }: { short: string; long: string }) {
  return (
    <>
      <span className="xl:hidden">{short}</span>
      <span className="hidden xl:inline">{long}</span>
    </>
  );
}

export default function PosTopBar({
  drawerOpen,
  openedAt,
  salesCount,
  salesTotal,
  drawerEnabled,
  drawerKicking,
  onKickDrawer,
  onCashMovement,
  onOpenTickets,
  onCloseDrawer,
  cashier,
  cashierBusy = false,
  onIdentifyCashier,
  onReleaseCashier,
}: Props) {
  const router = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirmLogout, setConfirmLogout] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const barRef = useRef<HTMLElement | null>(null);

  // Un clic hors de la barre referme le menu « ⋯ » et la confirmation.
  useEffect(() => {
    if (!menuOpen && !confirmLogout) return;
    const onPointerDown = (e: MouseEvent): void => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
        setConfirmLogout(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [menuOpen, confirmLogout]);

  const doLogout = async (): Promise<void> => {
    setLoggingOut(true);
    await logout();
    router.push("/login");
  };

  /** Caisse ouverte : on prévient avant de fermer la session (I2). */
  const requestLogout = (): void => {
    setMenuOpen(false);
    if (drawerOpen) {
      setConfirmLogout(true);
      return;
    }
    void doLogout();
  };

  return (
    <header
      ref={barRef}
      className="relative z-30 flex h-14 flex-shrink-0 items-center gap-2 bg-fc-primary px-2 text-white sm:px-3"
    >
      {/* Retour à l'accueil — geste principal de sortie. */}
      <Link
        href="/"
        className="inline-flex min-h-touch flex-shrink-0 items-center rounded-fc px-2 text-sm font-semibold text-white transition-colors hover:bg-white/15"
      >
        <span aria-hidden className="mr-1.5 text-base leading-none">
          ←
        </span>
        <span className="truncate">
          Frip &amp; Co Street <span className="font-normal text-white/80">· Caisse</span>
        </span>
      </Link>

      {/* État de la caisse. */}
      <div className="min-w-0 flex-1 truncate text-center text-xs text-white/90">
        {openedAt && (
          <span className="font-medium">
            <span className="xl:hidden">Ouverte {formatTime(openedAt)}</span>
            <span className="hidden xl:inline">Caisse ouverte depuis {formatTime(openedAt)}</span>
          </span>
        )}
        {openedAt && salesCount !== null && <span aria-hidden> · </span>}
        {salesCount !== null && (
          <span>
            {salesCount} vente{salesCount > 1 ? "s" : ""}
            {salesTotal !== null && ` · ${formatCurrency(salesTotal)}`}
          </span>
        )}
      </div>

      {/* Vendeuse (PR8, J4) — toujours visible, y compris sous 768 px :
          savoir qui encaisse prime sur les autres actions, et la relève
          doit se faire en un geste sans ouvrir le menu « ⋯ ». */}
      {cashier ? (
        <div className="flex flex-shrink-0 items-center gap-1">
          <span
            title={`Vendeuse : ${cashier.display_name}`}
            className="inline-flex min-h-[36px] max-w-[8rem] items-center gap-1.5 rounded-full bg-white/15 px-2.5 text-xs font-medium text-white xl:max-w-[12rem]"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
              className="flex-shrink-0"
            >
              <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
              <circle cx="12" cy="7" r="4" />
            </svg>
            <span className="truncate">
              <span className="hidden xl:inline">Vendeuse : </span>
              {cashier.display_name}
            </span>
          </span>
          <button type="button" onClick={onReleaseCashier} disabled={cashierBusy} className={ACTION_CLASS}>
            {cashierBusy ? "Relève…" : "Relève"}
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={onIdentifyCashier}
          disabled={cashierBusy}
          className={`flex-shrink-0 ${ACCENT_ACTION_CLASS}`}
        >
          S&apos;identifier
        </button>
      )}

      {/* Actions — une seule ligne à partir de 768 px. */}
      <div className="hidden flex-shrink-0 items-center gap-1 md:flex xl:gap-1.5">
        <CashMovementButton
          disabled={!drawerOpen}
          onSubmit={onCashMovement}
          className={ACTION_CLASS}
          label={<Label short="Mouvement" long="Mouvement de caisse" />}
        />
        {drawerEnabled && (
          <button type="button" onClick={onKickDrawer} disabled={drawerKicking || !drawerOpen} className={ACTION_CLASS}>
            {drawerKicking ? "Ouverture…" : <Label short="Tiroir" long="Ouvrir le tiroir" />}
          </button>
        )}
        <button type="button" onClick={onOpenTickets} className={ACTION_CLASS}>
          <Label short="Tickets" long="Tickets du jour" />
        </button>
        <button type="button" onClick={onCloseDrawer} className={ACTION_CLASS}>
          <Label short="Clôturer" long="Clôturer la caisse" />
        </button>

        {/* Groupe Sortie. */}
        <span aria-hidden className="mx-1 h-7 w-px flex-shrink-0 bg-white/30" />
        <Link href="/" className={ACTION_CLASS}>
          Accueil
        </Link>
        <Link href="/admin" className={ACTION_CLASS}>
          <Label short="Admin" long="Administration" />
        </Link>
        <button type="button" onClick={requestLogout} disabled={loggingOut} className={ACTION_CLASS}>
          {loggingOut ? "Déconnexion…" : <Label short="Déconnexion" long="Se déconnecter" />}
        </button>
      </div>

      {/* Sous 768 px : toutes les actions dans un menu. */}
      <button
        type="button"
        onClick={() => setMenuOpen((v) => !v)}
        aria-label="Autres actions"
        aria-expanded={menuOpen}
        className="inline-flex min-h-touch flex-shrink-0 items-center justify-center rounded-fc border border-white/40 bg-white/10 px-3 text-lg leading-none text-white hover:bg-white/20 md:hidden"
      >
        ⋯
      </button>

      {menuOpen && (
        <div className="absolute right-2 top-full z-40 mt-1 w-64 rounded-fc-lg border border-fc-line bg-fc-surface p-2 shadow-lg md:hidden">
          <CashMovementButton disabled={!drawerOpen} onSubmit={onCashMovement} className={MENU_ITEM_CLASS} />
          {drawerEnabled && (
            <button
              type="button"
              onClick={() => {
                setMenuOpen(false);
                onKickDrawer();
              }}
              disabled={drawerKicking || !drawerOpen}
              className={MENU_ITEM_CLASS}
            >
              Ouvrir le tiroir
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              setMenuOpen(false);
              onOpenTickets();
            }}
            className={MENU_ITEM_CLASS}
          >
            Tickets du jour
          </button>
          <button
            type="button"
            onClick={() => {
              setMenuOpen(false);
              onCloseDrawer();
            }}
            className={MENU_ITEM_CLASS}
          >
            Clôturer la caisse
          </button>

          <div className="my-1 border-t border-fc-line" />
          <Link href="/" onClick={() => setMenuOpen(false)} className={MENU_ITEM_CLASS}>
            Accueil
          </Link>
          <Link href="/admin" onClick={() => setMenuOpen(false)} className={MENU_ITEM_CLASS}>
            Administration
          </Link>
          <button type="button" onClick={requestLogout} disabled={loggingOut} className={MENU_ITEM_CLASS}>
            Se déconnecter
          </button>
        </div>
      )}

      {/* Confirmation de déconnexion caisse ouverte (I2). */}
      {confirmLogout && (
        <div
          role="alertdialog"
          aria-label="Confirmer la déconnexion"
          className="absolute right-2 top-full z-50 mt-1 w-72 rounded-fc-lg border border-fc-line bg-fc-surface p-3 text-fc-ink shadow-lg"
        >
          <p className="text-sm">La caisse reste ouverte, vous la retrouverez à la reconnexion.</p>
          <div className="mt-3 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setConfirmLogout(false)}
              className="min-h-touch rounded-fc border border-fc-line px-3 py-1.5 text-sm font-medium text-fc-ink hover:bg-fc-bg-alt"
            >
              Annuler
            </button>
            <button
              type="button"
              onClick={() => void doLogout()}
              disabled={loggingOut}
              className="min-h-touch rounded-fc bg-fc-primary px-3 py-1.5 text-sm font-semibold text-white hover:bg-fc-primary-deep disabled:opacity-60"
            >
              {loggingOut ? "Déconnexion…" : "Se déconnecter"}
            </button>
          </div>
        </div>
      )}
    </header>
  );
}
