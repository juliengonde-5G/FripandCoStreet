"use client";

/**
 * Sémantique + piège de focus pour les panneaux plein écran qui ne
 * passent pas par `components/ui/Modal.tsx` (CashDrawerOpenModal,
 * CashDrawerCloseModal — des `div fixed` dédiés, pas des dialogues
 * centrés). Même logique que `Modal.tsx` (ESC, Tab/Shift+Tab bouclés dans
 * le panneau, focus posé à l'ouverture puis restauré à la fermeture),
 * extraite ici pour être partagée sans dupliquer le piège de focus dans
 * chaque composant.
 */
import { useEffect, useRef } from "react";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

export function useDialogA11y<T extends HTMLElement>(active: boolean, onEscape?: () => void) {
  const containerRef = useRef<T | null>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!active) return;
    document.body.style.overflow = "hidden";
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    queueMicrotask(() => {
      const focusables = containerRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
      focusables?.[0]?.focus();
    });
    return () => {
      document.body.style.overflow = "";
      const prev = previouslyFocused.current;
      if (prev && document.body.contains(prev)) prev.focus();
    };
  }, [active]);

  useEffect(() => {
    if (!active) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (onEscape) {
          e.stopPropagation();
          onEscape();
        }
        return;
      }
      if (e.key !== "Tab") return;
      const focusables = containerRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
      if (!focusables || focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const activeEl = document.activeElement as HTMLElement | null;
      if (e.shiftKey && activeEl === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && activeEl === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [active, onEscape]);

  return containerRef;
}
