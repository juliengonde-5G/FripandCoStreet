"use client";

/**
 * Écran « Qui encaisse ? » (PR8, docs/ARCHITECTURE_PR8.md §1, J4).
 *
 * Plein écran, même barre sombre que `ClientSelectionScreen` : au
 * comptoir, on touche son prénom puis on tape quatre chiffres, et c'est
 * tout. Deux temps délibérément séparés — choisir la vendeuse, puis
 * saisir le code — pour qu'un code tapé ne parte jamais au nom de
 * quelqu'un d'autre.
 *
 * Le code ne s'affiche jamais : quatre pastilles se remplissent au fur et
 * à mesure. Un code faux dit « Code incorrect » et vide la saisie sans
 * faire sortir de l'écran ; après cinq essais, le serveur bloque la
 * vendeuse et l'écran affiche le temps restant, à la seconde, plutôt
 * qu'un message figé que personne ne sait interpréter.
 *
 * Aucun état n'est gardé ici : l'identité retenue est celle du tiroir
 * (`current_cashier`), la page de caisse la relit après coup.
 *
 * Accessibilité : `role="dialog"`, focus piégé et restauré
 * (`useDialogA11y`), Échap = Retour (retour à la liste depuis le pavé,
 * sortie de l'écran depuis la liste). L'arrière-plan est rendu inert par
 * la page de caisse, comme pour les autres écrans plein écran.
 */
import React, { useCallback, useEffect, useId, useRef, useState } from "react";

import { ApiError } from "@/lib/api";
import {
  PIN_LENGTH,
  fetchPosCashiers,
  formatCountdown,
  identifyCashier,
  isPinComplete,
  retryAfterSeconds,
} from "@/lib/cashier";
import { useDialogA11y } from "@/lib/useDialogA11y";
import type { CashierRef, PosCashier } from "@/lib/types";

interface Props {
  open: boolean;
  /** Retour à la caisse, sans identifier personne. */
  onClose: () => void;
  /** Vendeuse identifiée — le tiroir la porte désormais. */
  onIdentified: (cashier: CashierRef) => void;
  /** Pourquoi l'écran s'ouvre (« pour ouvrir la caisse », « pour
   * encaisser ») : affiché sous le titre quand la caisse a demandé
   * l'identification d'elle-même. */
  reason?: string | null;
}

/** Rebours sans dérive : on stocke l'instant de déblocage, pas un compteur
 * décrémenté (un onglet en arrière-plan ralentit les `setInterval`). */
function secondsUntil(unblockAt: number): number {
  return Math.max(0, Math.ceil((unblockAt - Date.now()) / 1000));
}

export default function CashierIdentifyScreen({ open, onClose, onIdentified, reason }: Props) {
  const titleId = useId();

  const [cashiers, setCashiers] = useState<PosCashier[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selected, setSelected] = useState<PosCashier | null>(null);
  const [pin, setPin] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** Instant (ms epoch) de fin du blocage serveur, `null` si libre. */
  const [unblockAt, setUnblockAt] = useState<number | null>(null);
  const [remaining, setRemaining] = useState(0);

  const blocked = unblockAt !== null && remaining > 0;

  const handleBack = useCallback((): void => {
    // Échap et le bouton « Retour » font la même chose : revenir d'un
    // cran (pavé → liste, liste → caisse).
    if (selected) {
      setSelected(null);
      setPin("");
      setError(null);
      return;
    }
    onClose();
  }, [selected, onClose]);

  const containerRef = useDialogA11y<HTMLDivElement>(open, handleBack);

  // Remise à zéro complète à chaque ouverture : on ne rouvre jamais sur la
  // vendeuse précédente ni sur un code à moitié tapé.
  useEffect(() => {
    if (!open) return;
    setSelected(null);
    setPin("");
    setError(null);
    setSubmitting(false);
    setUnblockAt(null);
    setRemaining(0);
    setLoading(true);
    setLoadError(null);
    let cancelled = false;
    fetchPosCashiers()
      .then((list) => {
        if (cancelled) return;
        setCashiers(list);
        setLoadError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setCashiers([]);
        setLoadError(err instanceof ApiError ? err.detail : "Liste des vendeuses indisponible.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // Compte à rebours du blocage — une seconde de tic, arrêté dès que le
  // temps est écoulé (le pavé redevient alors utilisable sans recharger).
  useEffect(() => {
    if (unblockAt === null) return;
    setRemaining(secondsUntil(unblockAt));
    const timer = setInterval(() => {
      const left = secondsUntil(unblockAt);
      setRemaining(left);
      if (left <= 0) {
        setUnblockAt(null);
        setError(null);
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [unblockAt]);

  const submit = useCallback(
    async (candidate: string, cashier: PosCashier): Promise<void> => {
      setSubmitting(true);
      try {
        const identified = await identifyCashier(cashier.id, candidate);
        setPin("");
        onIdentified(identified);
      } catch (err) {
        setPin("");
        if (err instanceof ApiError && err.status === 429) {
          const seconds = retryAfterSeconds(err);
          if (seconds && seconds > 0) {
            setUnblockAt(Date.now() + seconds * 1000);
            setRemaining(seconds);
          }
          setError(err.detail || "Trop de codes incorrects.");
        } else if (err instanceof ApiError && err.status === 401) {
          setError("Code incorrect");
        } else {
          setError(err instanceof ApiError ? err.detail : "Identification impossible.");
        }
      } finally {
        setSubmitting(false);
      }
    },
    [onIdentified],
  );

  /** Une frappe = un chiffre. L'envoi, lui, est déclenché par l'effet
   * ci-dessous : une mise à jour d'état React peut être rejouée (mode
   * strict), et un code ne doit jamais partir deux fois — ce serait deux
   * essais consommés sur cinq. */
  const pressDigit = useCallback(
    (digit: string): void => {
      if (!selected || submitting || blocked) return;
      setError(null);
      setPin((prev) => (prev.length >= PIN_LENGTH ? prev : prev + digit));
    },
    [selected, submitting, blocked],
  );

  // Le code part tout seul au quatrième chiffre.
  useEffect(() => {
    if (!selected || submitting || blocked) return;
    if (!isPinComplete(pin)) return;
    void submit(pin, selected);
  }, [pin, selected, submitting, blocked, submit]);

  const pressBackspace = useCallback((): void => {
    if (submitting || blocked) return;
    setError(null);
    setPin((prev) => prev.slice(0, -1));
  }, [submitting, blocked]);

  // Clavier physique : les tablettes du comptoir ont parfois un clavier
  // branché, et la recette se fait au clavier.
  useEffect(() => {
    if (!open || !selected) return;
    const onKeyDown = (e: KeyboardEvent): void => {
      if (/^\d$/.test(e.key)) {
        e.preventDefault();
        pressDigit(e.key);
      } else if (e.key === "Backspace") {
        e.preventDefault();
        pressBackspace();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, selected, pressDigit, pressBackspace]);

  if (!open) return null;

  return (
    <div
      ref={containerRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-[62] flex flex-col bg-fc-bg"
    >
      <header className="flex h-14 flex-shrink-0 items-center gap-3 bg-fc-primary px-2 text-white sm:px-3">
        <button
          type="button"
          onClick={handleBack}
          className="inline-flex min-h-touch flex-shrink-0 items-center rounded-fc px-2 text-sm font-semibold text-white transition-colors hover:bg-white/15"
        >
          <span aria-hidden className="mr-1.5 text-base leading-none">
            ←
          </span>
          Retour
        </button>
        <h2 id={titleId} className="min-w-0 flex-1 truncate text-center text-sm font-semibold">
          {selected ? `Code de ${selected.display_name}` : "Qui encaisse ?"}
        </h2>
        {/* Contrepoids du bouton « Retour » : garde le titre centré. */}
        <span aria-hidden className="w-20 flex-shrink-0" />
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mx-auto w-full max-w-3xl space-y-4">
          {reason && !selected && (
            <p className="rounded-fc-lg bg-fc-primary-soft px-4 py-3 text-center text-sm font-medium text-fc-primary-deep">
              {reason}
            </p>
          )}

          {selected ? (
            <PinPad
              cashier={selected}
              pin={pin}
              error={error}
              submitting={submitting}
              blocked={blocked}
              remaining={remaining}
              onDigit={pressDigit}
              onBackspace={pressBackspace}
              onChangeCashier={handleBack}
            />
          ) : (
            <CashierList
              cashiers={cashiers}
              loading={loading}
              loadError={loadError}
              onSelect={(cashier) => {
                setPin("");
                setError(cashier.has_pin ? null : "Aucun code n'est défini pour cette vendeuse (Administration > Vendeuses).");
                setSelected(cashier);
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Étape 1 — choisir la vendeuse
// ---------------------------------------------------------------------------

function CashierList({
  cashiers,
  loading,
  loadError,
  onSelect,
}: {
  cashiers: PosCashier[];
  loading: boolean;
  loadError: string | null;
  onSelect: (cashier: PosCashier) => void;
}) {
  if (loading) {
    return <p className="py-6 text-center text-sm text-fc-ink-soft">Chargement des vendeuses…</p>;
  }

  if (loadError) {
    return (
      <div role="alert" className="rounded-fc-lg border border-fc-danger/30 bg-fc-danger-soft p-3 text-sm text-fc-danger">
        {loadError}
      </div>
    );
  }

  if (cashiers.length === 0) {
    return (
      <div className="rounded-fc-lg border border-fc-line bg-fc-surface p-6 text-center">
        <p className="text-sm font-medium text-fc-ink">Aucune vendeuse enregistrée.</p>
        <p className="mt-1 text-sm text-fc-ink-soft">
          Ajoutez-les dans <span className="font-medium">Administration &gt; Vendeuses</span>, avec un code à quatre
          chiffres pour chacune.
        </p>
      </div>
    );
  }

  return (
    <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {cashiers.map((cashier) => (
        <li key={cashier.id}>
          <button
            type="button"
            onClick={() => onSelect(cashier)}
            className="flex min-h-[88px] w-full items-center gap-3 rounded-fc-lg border border-fc-line bg-fc-surface p-4 text-left transition-colors hover:border-fc-primary hover:bg-fc-primary-soft"
          >
            <span
              aria-hidden
              className="flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-full bg-fc-primary text-xl font-semibold text-white"
            >
              {cashier.display_name.trim().charAt(0).toUpperCase() || "?"}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-base font-semibold text-fc-ink">{cashier.display_name}</span>
              <span className="block truncate text-xs text-fc-ink-mute">
                {cashier.has_pin ? "Code à 4 chiffres" : "Code non défini"}
              </span>
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Étape 2 — le code
// ---------------------------------------------------------------------------

const PAD_KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9"];

function PinPad({
  cashier,
  pin,
  error,
  submitting,
  blocked,
  remaining,
  onDigit,
  onBackspace,
  onChangeCashier,
}: {
  cashier: PosCashier;
  pin: string;
  error: string | null;
  submitting: boolean;
  blocked: boolean;
  remaining: number;
  onDigit: (digit: string) => void;
  onBackspace: () => void;
  onChangeCashier: () => void;
}) {
  const padRef = useRef<HTMLDivElement | null>(null);

  // Le premier chiffre prend le focus : la tabulation reste dans le pavé
  // (piège de focus du dialogue) et la frappe clavier fonctionne tout de
  // suite, sans avoir à viser un champ.
  useEffect(() => {
    const timer = setTimeout(() => padRef.current?.querySelector<HTMLButtonElement>("button")?.focus(), 0);
    return () => clearTimeout(timer);
  }, []);

  const disabled = submitting || blocked;

  return (
    <div className="mx-auto w-full max-w-sm space-y-4 rounded-fc-lg border border-fc-line bg-fc-surface p-5">
      <div className="text-center">
        <p className="text-base font-semibold text-fc-ink">{cashier.display_name}</p>
        <p className="mt-0.5 text-xs text-fc-ink-mute">Tape ton code à quatre chiffres</p>
      </div>

      {/* Pastilles du code — le code lui-même ne s'affiche jamais. */}
      <div className="flex items-center justify-center gap-3" aria-live="polite" aria-label={`${pin.length} chiffre sur ${PIN_LENGTH}`}>
        {Array.from({ length: PIN_LENGTH }).map((_, index) => (
          <span
            key={index}
            aria-hidden
            className={`h-4 w-4 rounded-full border-2 transition-colors ${
              index < pin.length ? "border-fc-primary bg-fc-primary" : "border-fc-line bg-fc-surface"
            }`}
          />
        ))}
      </div>

      {blocked ? (
        <p role="alert" className="rounded-fc-lg border border-fc-danger/30 bg-fc-danger-soft px-3 py-2 text-center text-sm font-medium text-fc-danger">
          Trop de codes incorrects — réessaie dans {formatCountdown(remaining)}.
        </p>
      ) : error ? (
        <p role="alert" className="rounded-fc-lg border border-fc-danger/30 bg-fc-danger-soft px-3 py-2 text-center text-sm font-medium text-fc-danger">
          {error}
        </p>
      ) : (
        <p className="text-center text-sm text-fc-ink-soft">{submitting ? "Vérification…" : " "}</p>
      )}

      <div ref={padRef} className="grid grid-cols-3 gap-2">
        {PAD_KEYS.map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => onDigit(key)}
            disabled={disabled}
            className="min-h-touch rounded-fc border border-fc-line bg-fc-surface py-4 text-2xl font-bold text-fc-ink shadow-sm transition-all hover:bg-fc-primary-soft active:scale-95 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {key}
          </button>
        ))}
        <button
          type="button"
          onClick={onChangeCashier}
          className="min-h-touch rounded-fc border border-fc-line bg-fc-bg-alt py-4 text-sm font-medium text-fc-ink-soft transition-colors hover:bg-fc-line"
        >
          Changer
        </button>
        <button
          type="button"
          onClick={() => onDigit("0")}
          disabled={disabled}
          className="min-h-touch rounded-fc border border-fc-line bg-fc-surface py-4 text-2xl font-bold text-fc-ink shadow-sm transition-all hover:bg-fc-primary-soft active:scale-95 disabled:cursor-not-allowed disabled:opacity-50"
        >
          0
        </button>
        <button
          type="button"
          onClick={onBackspace}
          disabled={disabled}
          aria-label="Effacer le dernier chiffre"
          className="min-h-touch rounded-fc bg-fc-danger-soft py-4 text-xl font-bold text-fc-danger transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          ⌫
        </button>
      </div>
    </div>
  );
}
