"use client";

/**
 * Administration (§6 PR2) — paramètres boutique/fiscal/ticket, état du
 * terminal de paiement, rapports de clôture, contrôle d'intégrité et
 * journal des événements (jamais nommé « JET » dans l'UI — CDC §3.2).
 */
import React, { useEffect, useState } from "react";

import AppShell from "@/components/layout/AppShell";
import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import Input from "@/components/ui/Input";
import { api, ApiError } from "@/lib/api";
import { formatCurrency, formatDateTime } from "@/lib/format";
import {
  TVA_RATES,
  type CbStatusConfig,
  type FiscalIntegrityResponse,
  type FiscalSettings,
  type JetEvent,
  type ReceiptSettings,
  type ShopSettings,
  type ZReport,
} from "@/lib/types";

export default function AdminPage() {
  return (
    <AppShell>
      <h1 className="text-2xl font-bold text-fc-ink mb-6">Administration</h1>
      <div className="space-y-6">
        <ShopSettingsCard />
        <FiscalSettingsCard />
        <ReceiptSettingsCard />
        <TerminalStatusCard />
        <ZReportsCard />
        <IntegrityCard />
        <EventLogCard />
      </div>
    </AppShell>
  );
}

// ---------------------------------------------------------------------------
// Aides communes
// ---------------------------------------------------------------------------

function SavedNotice({ show }: { show: boolean }) {
  if (!show) return null;
  return <span className="text-sm text-fc-success font-medium">Enregistré.</span>;
}

function ErrorNotice({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div role="alert" className="rounded-fc bg-red-50 border border-red-200 px-3 py-2 text-sm text-red-700">
      {message}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Boutique
// ---------------------------------------------------------------------------

const EMPTY_SHOP: ShopSettings = {
  name: "",
  address_line1: "",
  address_line2: "",
  postal_code: "",
  city: "",
  siret: "",
  vat_number: "",
  phone: "",
  email: "",
};

function ShopSettingsCard() {
  const [form, setForm] = useState<ShopSettings>(EMPTY_SHOP);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<ShopSettings>("/api/admin/settings/shop")
      .then((data) => setForm({ ...EMPTY_SHOP, ...data }))
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger les informations boutique."))
      .finally(() => setLoading(false));
  }, []);

  const set = (field: keyof ShopSettings) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setForm((f) => ({ ...f, [field]: e.target.value }));
    setSaved(false);
  };

  const handleSave = async (): Promise<void> => {
    setSaving(true);
    setError(null);
    try {
      const data = await api.put<ShopSettings>("/api/admin/settings/shop", form);
      setForm({ ...EMPTY_SHOP, ...data });
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Échec de l'enregistrement.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card title="Boutique" subtitle="Coordonnées imprimées sur les tickets et l'en-tête d'administration.">
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-4">
          <ErrorNotice message={error} />
          <div className="grid gap-4 sm:grid-cols-2">
            <Input label="Nom de la boutique" value={form.name} onChange={set("name")} />
            <Input label="Téléphone" value={form.phone} onChange={set("phone")} />
            <Input label="Adresse" value={form.address_line1} onChange={set("address_line1")} />
            <Input label="Complément d'adresse" value={form.address_line2 ?? ""} onChange={set("address_line2")} />
            <Input label="Code postal" value={form.postal_code} onChange={set("postal_code")} />
            <Input label="Ville" value={form.city} onChange={set("city")} />
            <Input label="SIRET" value={form.siret} onChange={set("siret")} maxLength={14} />
            <Input label="N° de TVA intracommunautaire" value={form.vat_number} onChange={set("vat_number")} />
            <Input label="E-mail" type="email" value={form.email} onChange={set("email")} />
          </div>
          <div className="flex items-center gap-3">
            <Button onClick={() => void handleSave()} disabled={saving}>
              {saving ? "Enregistrement…" : "Enregistrer"}
            </Button>
            <SavedNotice show={saved} />
          </div>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Fiscal (TVA)
// ---------------------------------------------------------------------------

function FiscalSettingsCard() {
  const [rate, setRate] = useState<string>("20.00");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<FiscalSettings>("/api/admin/settings/fiscal")
      .then((data) => setRate(data.tva_rate))
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger le taux de TVA."))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async (): Promise<void> => {
    setSaving(true);
    setError(null);
    try {
      const data = await api.put<FiscalSettings>("/api/admin/settings/fiscal", { tva_rate: rate });
      setRate(data.tva_rate);
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Échec de l'enregistrement.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card title="TVA" subtitle="Taux appliqué à toutes les ventes — régime normal (§4.1, D8).">
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-4">
          <ErrorNotice message={error} />
          <label className="block max-w-xs">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Taux de TVA</span>
            <select
              value={rate}
              onChange={(e) => {
                setRate(e.target.value);
                setSaved(false);
              }}
              className="w-full min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
            >
              {TVA_RATES.map((r) => (
                <option key={r} value={r}>
                  {r.replace(".", ",")} %
                </option>
              ))}
            </select>
          </label>
          <div className="flex items-center gap-3">
            <Button onClick={() => void handleSave()} disabled={saving}>
              {saving ? "Enregistrement…" : "Enregistrer"}
            </Button>
            <SavedNotice show={saved} />
          </div>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Pied de ticket
// ---------------------------------------------------------------------------

const EMPTY_RECEIPT: ReceiptSettings = { header_note: "", footer_note: "", return_policy: "" };

function ReceiptSettingsCard() {
  const [form, setForm] = useState<ReceiptSettings>(EMPTY_RECEIPT);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<ReceiptSettings>("/api/admin/settings/receipt")
      .then((data) => setForm({ ...EMPTY_RECEIPT, ...data }))
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger le pied de ticket."))
      .finally(() => setLoading(false));
  }, []);

  const set = (field: keyof ReceiptSettings) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setForm((f) => ({ ...f, [field]: e.target.value }));
    setSaved(false);
  };

  const handleSave = async (): Promise<void> => {
    setSaving(true);
    setError(null);
    try {
      const data = await api.put<ReceiptSettings>("/api/admin/settings/receipt", form);
      setForm({ ...EMPTY_RECEIPT, ...data });
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Échec de l'enregistrement.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card title="Pied de ticket" subtitle="Mentions ajoutées en bas de chaque ticket imprimé.">
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-4">
          <ErrorNotice message={error} />
          <Input label="Message d'en-tête" value={form.header_note} onChange={set("header_note")} />
          <Input label="Message de fin (ex. remerciement)" value={form.footer_note} onChange={set("footer_note")} />
          <Input label="Politique de retour" value={form.return_policy} onChange={set("return_policy")} />
          <div className="flex items-center gap-3">
            <Button onClick={() => void handleSave()} disabled={saving}>
              {saving ? "Enregistrement…" : "Enregistrer"}
            </Button>
            <SavedNotice show={saved} />
          </div>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Terminal de paiement
// ---------------------------------------------------------------------------

function TerminalStatusCard() {
  const [status, setStatus] = useState<CbStatusConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api
      .get<CbStatusConfig>("/api/pos/payments/cb/status")
      .then(setStatus)
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de contacter le terminal."))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  return (
    <Card
      title="Terminal de paiement (SumUp)"
      subtitle="Aucune information sensible n'est affichée ici."
      action={
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          Actualiser
        </Button>
      }
    >
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Vérification…</p>
      ) : (
        <div className="space-y-2">
          <ErrorNotice message={error} />
          {status && (
            <div className="flex flex-wrap items-center gap-4">
              <StatusPill ok={status.configured} okLabel="Configuré" koLabel="Non configuré" />
              {status.configured && <StatusPill ok={!!status.reader_online} okLabel="En ligne" koLabel="Hors ligne" />}
              {status.battery !== undefined && status.battery !== null && (
                <span className="text-sm text-fc-ink-soft">Batterie : {status.battery}%</span>
              )}
              {status.message && <span className="text-sm text-fc-ink-mute">{status.message}</span>}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function StatusPill({ ok, okLabel, koLabel }: { ok: boolean; okLabel: string; koLabel: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-fc px-2.5 py-1 text-xs font-medium ${
        ok ? "bg-fc-primary-soft text-fc-primary-deep" : "bg-fc-warn-soft text-fc-warn"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-fc-primary" : "bg-fc-warn"}`} aria-hidden />
      {ok ? okLabel : koLabel}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Rapports de clôture (Z)
// ---------------------------------------------------------------------------

function ZReportsCard() {
  const [list, setList] = useState<ZReport[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<{ z_reports: ZReport[] }>("/api/pos/z-reports")
      .then((data) => setList(data.z_reports))
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger les clôtures."))
      .finally(() => setLoading(false));
  }, []);

  return (
    <Card title="Clôtures de caisse" subtitle="Historique des rapports Z, du plus récent au plus ancien.">
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-2">
          <ErrorNotice message={error} />
          {list.length === 0 && <p className="text-sm text-fc-ink-soft">Aucune clôture enregistrée.</p>}
          {list.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-fc-ink-mute uppercase text-xs tracking-wide">
                    <th className="py-2 pr-4">N°</th>
                    <th className="py-2 pr-4">Clôturé le</th>
                    <th className="py-2 pr-4">Ventes</th>
                    <th className="py-2 pr-4">Attendu</th>
                    <th className="py-2 pr-4">Compté</th>
                    <th className="py-2 pr-4">Écart</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((z) => (
                    <tr key={z.id} className="border-t border-fc-line">
                      <td className="py-2 pr-4 font-mono">{z.report_number}</td>
                      <td className="py-2 pr-4">{formatDateTime(z.closed_at)}</td>
                      <td className="py-2 pr-4 font-mono">{formatCurrency(z.total_net)}</td>
                      <td className="py-2 pr-4 font-mono">{formatCurrency(z.expected_amount)}</td>
                      <td className="py-2 pr-4 font-mono">{formatCurrency(z.closing_amount)}</td>
                      <td className={`py-2 pr-4 font-mono ${Math.abs(z.discrepancy) > 2 ? "text-fc-warn" : ""}`}>
                        {z.discrepancy > 0 ? "+" : ""}
                        {formatCurrency(z.discrepancy)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Contrôle d'intégrité
// ---------------------------------------------------------------------------

function IntegrityCard() {
  const [result, setResult] = useState<FiscalIntegrityResponse | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleCheck = async (): Promise<void> => {
    setChecking(true);
    setError(null);
    try {
      const data = await api.get<FiscalIntegrityResponse>("/api/admin/fiscal/integrity");
      setResult(data);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Échec du contrôle d'intégrité.");
    } finally {
      setChecking(false);
    }
  };

  return (
    <Card title="Contrôle d'intégrité" subtitle="Vérifie la continuité des ventes, des clôtures et du journal des événements.">
      <div className="space-y-3">
        <ErrorNotice message={error} />
        <Button onClick={() => void handleCheck()} disabled={checking}>
          {checking ? "Vérification…" : "Vérifier l'intégrité"}
        </Button>
        {result && (
          <div className="grid gap-3 sm:grid-cols-3">
            <IntegrityTile label="Ventes" check={result.transactions} />
            <IntegrityTile label="Clôtures" check={result.z_reports} />
            <IntegrityTile label="Journal des événements" check={result.jet} />
          </div>
        )}
      </div>
    </Card>
  );
}

function IntegrityTile({ label, check }: { label: string; check: { ok?: boolean; valid?: boolean; message?: string; count?: number } }) {
  const ok = check.ok ?? check.valid ?? true;
  return (
    <div className={`rounded-fc-lg border p-3 ${ok ? "border-fc-primary/30 bg-fc-primary-soft" : "border-fc-warn/40 bg-fc-warn-soft"}`}>
      <div className={`text-sm font-semibold ${ok ? "text-fc-primary-deep" : "text-fc-warn"}`}>{label}</div>
      {typeof check.count === "number" && <div className="text-xs text-fc-ink-soft">{check.count} élément(s)</div>}
      {check.message && <div className="text-xs text-fc-ink-soft mt-1">{check.message}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Journal des événements (JET côté contrat — jamais affiché sous ce nom)
// ---------------------------------------------------------------------------

function EventLogCard() {
  const [events, setEvents] = useState<JetEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nextBeforeSeq, setNextBeforeSeq] = useState<number | null | undefined>(undefined);
  const [history, setHistory] = useState<(number | undefined)[]>([undefined]);

  const load = (beforeSeq?: number) => {
    setLoading(true);
    setError(null);
    const qs = new URLSearchParams({ limit: "50" });
    if (beforeSeq !== undefined) qs.set("before_seq", String(beforeSeq));
    api
      .get<{ events: JetEvent[]; next_before_seq?: number | null }>(`/api/admin/jet?${qs.toString()}`)
      .then((data) => {
        setEvents(data.events);
        setNextBeforeSeq(data.next_before_seq ?? null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger le journal des événements."))
      .finally(() => setLoading(false));
  };

  useEffect(() => load(undefined), []); // eslint-disable-line react-hooks/exhaustive-deps

  const goNext = () => {
    if (!nextBeforeSeq) return;
    setHistory((h) => [...h, nextBeforeSeq]);
    load(nextBeforeSeq);
  };

  const goPrev = () => {
    if (history.length <= 1) return;
    const h = history.slice(0, -1);
    setHistory(h);
    load(h[h.length - 1]);
  };

  return (
    <Card title="Journal des événements" subtitle="Trace de chaque action : ventes, clôtures, paramètres modifiés.">
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-3">
          <ErrorNotice message={error} />
          {events.length === 0 && <p className="text-sm text-fc-ink-soft">Aucun événement.</p>}
          <ul className="divide-y divide-fc-line">
            {events.map((e) => (
              <li key={e.seq} className="py-2 flex items-center justify-between gap-3 text-sm">
                <span className="font-mono text-xs text-fc-ink-mute w-10 flex-shrink-0">#{e.seq}</span>
                <span className="flex-1 text-fc-ink">{describeEvent(e.event_type)}</span>
                <span className="text-xs text-fc-ink-mute flex-shrink-0">{formatDateTime(e.created_at)}</span>
              </li>
            ))}
          </ul>
          <div className="flex items-center gap-3">
            <Button variant="outline" size="sm" onClick={goPrev} disabled={history.length <= 1}>
              Précédent
            </Button>
            <Button variant="outline" size="sm" onClick={goNext} disabled={!nextBeforeSeq}>
              Suivant
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}

const EVENT_LABELS: Record<string, string> = {
  "sale.created": "Vente enregistrée",
  "sale.cancelled": "Ticket annulé",
  "drawer.opened": "Caisse ouverte",
  "drawer.closed": "Caisse clôturée",
  "drawer.auto_closed": "Caisse clôturée automatiquement",
  "cash_movement.created": "Mouvement de caisse",
  "z.regularization": "Régularisation de clôture",
  "payment.cb_initiated": "Paiement carte envoyé au terminal",
  "payment.cb_paid": "Paiement carte confirmé",
  "payment.cb_failed": "Paiement carte refusé",
  "payment.cb_cancelled": "Paiement carte annulé",
  "receipt.duplicate": "Ticket réimprimé",
  "config.changed": "Paramètres modifiés",
  "system.job_failed": "Tâche automatique en échec",
  "fiscal.integrity_checked": "Contrôle d'intégrité exécuté",
};

function describeEvent(type: string): string {
  return EVENT_LABELS[type] ?? type;
}
