"use client";

/**
 * Extrait de Vintiz `apps/web/src/components/ui/NumPad.tsx`, jetons
 * `vz-*` → `fc-*`. Virgule décimale FR (saisie affichée avec virgule, la
 * valeur interne reste un nombre à point décimal — `parseFloat` accepte
 * les deux à l'affichage près) — voir la conversion `.` → `,` ci-dessous.
 */
import React, { useEffect, useState } from "react";

interface NumPadProps {
  value: number;
  onChange: (value: number) => void;
  presets?: number[];
  /** Gabarit resserré (marges/tailles réduites, boutons toujours ≥48px
   * via `min-h-touch`) — utilisé dans les modales de paiement pour que le
   * bloc « Monnaie à rendre » reste entièrement visible sans défilement
   * sur une tablette 1024×768 (correctif persona vendeuse). */
  compact?: boolean;
}

export default function NumPad({ value, onChange, presets, compact = false }: NumPadProps) {
  const [raw, setRaw] = useState(value > 0 ? value.toFixed(2) : "");

  useEffect(() => {
    const parsed = parseFloat(raw) || 0;
    const stable = !raw.endsWith(".") && !raw.endsWith(".0") && !raw.endsWith(".00");
    if (stable && Math.abs(parsed - value) > 0.001) {
      setRaw(value > 0 ? value.toFixed(2) : "");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const commit = (r: string) => {
    setRaw(r);
    onChange(parseFloat(r) || 0);
  };

  const press = (k: string) => {
    if (k === "⌫") {
      commit(raw.length > 1 ? raw.slice(0, -1) : "");
      return;
    }
    if (k === ",") {
      if (!raw.includes(".")) commit((raw || "0") + ".");
      return;
    }
    if (raw.includes(".") && (raw.split(".")[1]?.length ?? 0) >= 2) return;
    commit(!raw || raw === "0" ? k : raw + k);
  };

  const display = raw.replace(".", ",");

  return (
    <div className={compact ? "space-y-1.5" : "space-y-2"}>
      <div className={`text-right bg-fc-bg-alt rounded-fc-lg ${compact ? "px-3 py-1.5" : "px-4 py-3"}`}>
        <span className={`font-bold text-fc-ink tabular-nums ${compact ? "text-2xl" : "text-3xl"}`}>
          {display || "0"} <span className={`font-normal text-fc-ink-mute ${compact ? "text-lg" : "text-xl"}`}>€</span>
        </span>
      </div>

      {presets && presets.length > 0 && (
        <div className="flex gap-1.5 flex-wrap">
          {presets.map((p, i) => (
            <button
              key={i}
              type="button"
              onClick={() => {
                setRaw(p.toFixed(2));
                onChange(p);
              }}
              className={`flex-1 min-w-[52px] min-h-touch bg-fc-primary-soft hover:opacity-90 active:opacity-80 text-fc-primary-deep rounded-fc text-sm font-bold transition-colors ${
                compact ? "py-1.5" : "py-2.5"
              }`}
            >
              {Number.isInteger(p) ? `${p} €` : `${p.toFixed(2).replace(".", ",")} €`}
            </button>
          ))}
        </div>
      )}

      <div className={`grid grid-cols-3 ${compact ? "gap-1.5" : "gap-2"}`}>
        {["7", "8", "9", "4", "5", "6", "1", "2", "3", ",", "0", "⌫"].map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => press(k)}
            className={`min-h-touch rounded-fc font-bold select-none transition-all active:scale-95 ${
              compact ? "py-2 text-xl" : "py-4 text-2xl"
            } ${
              k === "⌫"
                ? "bg-red-50 text-fc-danger hover:bg-red-100 active:bg-red-200"
                : "bg-fc-surface border border-fc-line text-fc-ink hover:bg-fc-primary-soft shadow-sm"
            }`}
          >
            {k}
          </button>
        ))}
      </div>
    </div>
  );
}
