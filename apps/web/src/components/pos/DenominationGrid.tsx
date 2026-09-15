"use client";

/**
 * Extrait de Vintiz `apps/web/src/components/pos/DenominationGrid.tsx`,
 * jetons `vz-*` → `fc-*`, sinon inchangé.
 */
import React, { useMemo } from "react";

import { formatCurrency } from "@/lib/format";

export interface DenominationLine {
  denom: number;
  count: number;
}

export const BANKNOTES = [500, 200, 100, 50, 20, 10, 5];
export const COINS = [2, 1, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01];

interface Props {
  value: DenominationLine[];
  onChange: (next: DenominationLine[]) => void;
  banknotesOnly?: boolean;
  readOnly?: boolean;
}

function indexBy(rows: DenominationLine[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const r of rows) out[String(r.denom)] = r.count;
  return out;
}

function buildRows(idx: Record<string, number>): DenominationLine[] {
  return Object.entries(idx)
    .filter(([, count]) => count > 0)
    .map(([denom, count]) => ({ denom: Number(denom), count }))
    .sort((a, b) => b.denom - a.denom);
}

/**
 * Édition du fond de caisse par dénomination, avec sous-totaux en direct.
 * Utilisé par CashDrawerOpenModal et CashDrawerCloseModal.
 */
export default function DenominationGrid({ value, onChange, banknotesOnly = false, readOnly = false }: Props) {
  const idx = useMemo(() => indexBy(value), [value]);

  const update = (denom: number, count: number): void => {
    if (readOnly) return;
    const next = { ...idx };
    if (count > 0) {
      next[String(denom)] = count;
    } else {
      delete next[String(denom)];
    }
    onChange(buildRows(next));
  };

  const total = value.reduce((sum, r) => sum + r.denom * r.count, 0);

  const renderRow = (denom: number) => {
    const count = idx[String(denom)] ?? 0;
    const subtotal = denom * count;
    const label = denom >= 1 ? `${denom} €` : `${Math.round(denom * 100)} cts`;
    return (
      <div key={denom} className="flex items-center gap-3 py-2 border-b border-fc-line last:border-b-0">
        <div className="w-16 font-mono text-sm text-fc-ink-soft">{label}</div>
        <input
          type="number"
          min={0}
          step={1}
          inputMode="numeric"
          disabled={readOnly}
          value={count || ""}
          onChange={(e) => update(denom, Math.max(0, parseInt(e.target.value || "0", 10) || 0))}
          className="flex-1 min-w-0 min-h-touch rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-right font-mono text-base text-fc-ink focus:border-fc-primary focus:outline-none focus:ring-1 focus:ring-fc-primary disabled:bg-fc-bg-alt"
          placeholder="0"
          aria-label={`Nombre de coupures de ${label}`}
        />
        <div className="w-24 text-right font-mono text-sm tabular-nums text-fc-ink">
          {subtotal > 0 ? formatCurrency(subtotal) : "—"}
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-3">
      <div className="grid gap-4 md:grid-cols-2">
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fc-ink-mute">Billets</h3>
          <div className="rounded-fc border border-fc-line bg-fc-surface px-3 py-1">{BANKNOTES.map(renderRow)}</div>
        </section>
        {!banknotesOnly && (
          <section>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fc-ink-mute">Pièces</h3>
            <div className="rounded-fc border border-fc-line bg-fc-surface px-3 py-1">{COINS.map(renderRow)}</div>
          </section>
        )}
      </div>

      <div className="flex items-center justify-between rounded-fc-lg bg-fc-primary-soft px-4 py-3">
        <span className="text-sm font-medium text-fc-primary-deep">Total compté</span>
        <span className="font-mono text-2xl font-bold tabular-nums text-fc-primary-deep">{formatCurrency(total)}</span>
      </div>
    </div>
  );
}

export function totalFromBreakdown(rows: DenominationLine[]): number {
  return rows.reduce((sum, r) => sum + r.denom * r.count, 0);
}
