"use client";

/**
 * Administration (§6 PR2) — paramètres boutique/fiscal/ticket, état du
 * terminal de paiement, rapports de clôture, contrôle d'intégrité et
 * journal des événements (jamais nommé « JET » dans l'UI — CDC §3.2).
 */
import React, { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import AccountingTab from "@/components/admin/AccountingTab";
import BackupsTab from "@/components/admin/BackupsTab";
import FiscalArchivesTab from "@/components/admin/FiscalArchivesTab";
import RequireAuth from "@/components/layout/RequireAuth";
import Sidebar from "@/components/layout/Sidebar";
import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import Input from "@/components/ui/Input";
import { api, ApiError } from "@/lib/api";
import { downloadFile } from "@/lib/download";
import { formatCurrency, formatDateTime } from "@/lib/format";
import { kickDrawer } from "@/lib/printing";
import {
  TVA_RATES,
  type AnonymizeRequest,
  type CbStatusConfig,
  type Client,
  type ClientFull,
  type ConsentUpdateRequest,
  type FiscalIntegrityResponse,
  type FiscalSettings,
  type HardwareSettings,
  type JetEvent,
  type MessagingStatus,
  type PrinterMode,
  type PrinterStatus,
  type ReceiptSettings,
  type ReceiptTestResponse,
  type ShopSettings,
  type TargetsSettings,
  type ZReport,
} from "@/lib/types";
import { findPairedUsbDevice, getStoredPrinter, isWebUsbSupported, pairUsbPrinter, sendBytes } from "@/lib/webusb-printer";

/** Onglets de la page — l'ordre suit celui de la barre latérale (PR7, I1). */
type Tab = "settings" | "hardware" | "clients" | "accounting" | "fiscal" | "backups";

const TABS: { id: Tab; label: string }[] = [
  { id: "settings", label: "Réglages" },
  { id: "hardware", label: "Matériel" },
  { id: "clients", label: "Clients" },
  { id: "accounting", label: "Comptabilité" },
  { id: "fiscal", label: "Archives fiscales" },
  { id: "backups", label: "Sauvegardes" },
];

/** Onglet ouvert par défaut quand l'URL ne porte pas de `?tab=`. */
const DEFAULT_TAB: Tab = "settings";

function isTab(value: string | null): value is Tab {
  return !!value && TABS.some((t) => t.id === value);
}

export default function AdminPage() {
  return (
    <RequireAuth>
      <Sidebar />
      <main className="md:ml-64 px-4 pt-16 pb-6 md:p-8">
        {/* `useSearchParams` impose une frontière de rendu différé (Next 15). */}
        <Suspense fallback={<p className="text-sm text-fc-ink-soft">Chargement…</p>}>
          <AdminTabs />
        </Suspense>
      </main>
    </RequireAuth>
  );
}

/**
 * PR7 (I1) : l'onglet actif vit dans l'URL (`/admin?tab=clients`), pour que
 * les entrées de la barre latérale ouvrent directement le bon onglet et que
 * la page reste partageable/rechargeable. Un clic sur un onglet met l'URL à
 * jour sans empiler d'entrée d'historique (`replace`).
 */
function AdminTabs() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const param = searchParams.get("tab");
  const tab: Tab = isTab(param) ? param : DEFAULT_TAB;

  const selectTab = (next: Tab): void => {
    router.replace(next === DEFAULT_TAB ? "/admin" : `/admin?tab=${next}`, { scroll: false });
  };

  return (
    <>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-fc-ink">Administration</h1>
        <div className="flex flex-wrap items-center gap-1 rounded-fc-lg bg-fc-bg-alt p-1">
          {TABS.map((t) => (
            <TabButton key={t.id} active={tab === t.id} onClick={() => selectTab(t.id)}>
              {t.label}
            </TabButton>
          ))}
        </div>
      </div>

      {tab === "settings" && (
        <div className="space-y-6">
          <ShopSettingsCard />
          <FiscalSettingsCard />
          <TargetsCard />
          <ReceiptSettingsCard />
          <MessagingStatusCard />
          <TerminalStatusCard />
          <ZReportsCard />
          <IntegrityCard />
          <EventLogCard />
        </div>
      )}

      {tab === "hardware" && (
        <div className="space-y-6">
          <HardwareSettingsCard />
        </div>
      )}

      {tab === "clients" && <ClientsSection />}

      {tab === "accounting" && <AccountingTab />}

      {tab === "fiscal" && <FiscalArchivesTab />}

      {tab === "backups" && <BackupsTab />}
    </>
  );
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`min-h-touch rounded-fc px-4 py-2 text-sm font-medium transition-colors ${
        active ? "bg-fc-surface text-fc-primary-deep shadow-sm" : "text-fc-ink-soft hover:text-fc-ink"
      }`}
    >
      {children}
    </button>
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
    <div role="alert" className="rounded-fc bg-fc-danger-soft border border-fc-danger/30 px-3 py-2 text-sm text-fc-danger">
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
  dpo_email: "",
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
            <Input
              label="E-mail RGPD (droits d'accès, suppression)"
              type="email"
              value={form.dpo_email ?? ""}
              onChange={set("dpo_email")}
            />
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
// Objectifs de chiffre d'affaires (PR6, H5)
// ---------------------------------------------------------------------------

/** `Date` → clé de mois du contrat H1 (`"YYYY-MM"`), calculée côté front
 * (la boutique et la tablette sont sur le même fuseau). */
function monthKeyOf(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function monthLabelOf(key: string): string {
  const m = /^(\d{4})-(\d{2})$/.exec(key);
  if (!m) return key;
  return new Date(Number(m[1]), Number(m[2]) - 1, 1).toLocaleDateString("fr-FR", {
    month: "long",
    year: "numeric",
  });
}

/** Saisie en euros (virgule ou point acceptée, champ vide = 0) → chaîne à
 * 2 décimales du contrat H1. `null` si la saisie n'est pas un montant
 * positif : la carte refuse alors d'appeler l'API. */
function toAmountString(raw: string): string | null {
  const trimmed = raw.trim().replace(",", ".").replace(/\s/g, "");
  if (trimmed === "") return "0.00";
  if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) return null;
  const n = Number.parseFloat(trimmed);
  if (!Number.isFinite(n) || n < 0) return null;
  return n.toFixed(2);
}

/** Affichage d'une valeur du contrat ("1500.00") dans un champ de saisie,
 * en convention française (virgule décimale) — `toAmountString` sait relire
 * les deux écritures. */
function toAmountInput(value: string | undefined): string {
  const n = Number.parseFloat(value ?? "");
  return Number.isFinite(n) && n > 0 ? n.toFixed(2).replace(".", ",") : "";
}

function TargetsCard() {
  // Clés de mois figées au premier rendu : la carte doit rester stable même
  // si l'onglet reste ouvert au passage de minuit.
  const [monthKeys] = useState(() => {
    const now = new Date();
    const next = new Date(now.getFullYear(), now.getMonth() + 1, 1);
    return { current: monthKeyOf(now), next: monthKeyOf(next) };
  });

  const [daily, setDaily] = useState("");
  const [currentMonth, setCurrentMonth] = useState("");
  const [nextMonth, setNextMonth] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<TargetsSettings>("/api/admin/settings/targets")
      .then((data) => {
        const monthly = data?.monthly ?? {};
        setDaily(toAmountInput(data?.daily));
        // Un mois non saisi hérite du repli `default` (H1) : on affiche la
        // valeur qui s'applique réellement, pas un champ vide trompeur.
        setCurrentMonth(toAmountInput(monthly[monthKeys.current] ?? monthly.default));
        setNextMonth(toAmountInput(monthly[monthKeys.next] ?? monthly.default));
      })
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger les objectifs."))
      .finally(() => setLoading(false));
  }, [monthKeys]);

  const edit = (setter: (v: string) => void) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setter(e.target.value);
    setSaved(false);
  };

  const handleSave = async (): Promise<void> => {
    const dailyValue = toAmountString(daily);
    const currentValue = toAmountString(currentMonth);
    const nextValue = toAmountString(nextMonth);
    if (dailyValue === null || currentValue === null || nextValue === null) {
      setError("Montant invalide : saisissez un nombre positif (ex. 1500 ou 1500,50).");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      // Relecture de la carte complète avant réécriture (H5) : le `PUT`
      // remplace `monthly` en entier, les mois déjà saisis (et un éventuel
      // repli `default`) doivent donc être reportés tels quels.
      let existing: Record<string, string> = {};
      try {
        const current = await api.get<TargetsSettings>("/api/admin/settings/targets");
        existing = { ...(current?.monthly ?? {}) };
      } catch {
        // Réglage encore absent côté serveur : on repart d'une carte vide
        // plutôt que d'abandonner l'enregistrement.
      }
      const payload: TargetsSettings = {
        daily: dailyValue,
        monthly: { ...existing, [monthKeys.current]: currentValue, [monthKeys.next]: nextValue },
      };
      const data = await api.put<TargetsSettings>("/api/admin/settings/targets", payload);
      const monthly = data?.monthly ?? payload.monthly;
      setDaily(toAmountInput(data?.daily ?? payload.daily));
      setCurrentMonth(toAmountInput(monthly[monthKeys.current]));
      setNextMonth(toAmountInput(monthly[monthKeys.next]));
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Échec de l'enregistrement.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card
      title="Objectifs"
      subtitle="Chiffre d'affaires visé, affiché sur le tableau de bord d'accueil. Laisser à zéro pour ne pas fixer d'objectif."
    >
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-4">
          <ErrorNotice message={error} />
          <div className="grid gap-4 sm:grid-cols-3">
            <Input
              label="Objectif journalier (€)"
              inputMode="decimal"
              placeholder="0,00"
              value={daily}
              onChange={edit(setDaily)}
            />
            <Input
              label={`Objectif ${monthLabelOf(monthKeys.current)} (€)`}
              inputMode="decimal"
              placeholder="0,00"
              value={currentMonth}
              onChange={edit(setCurrentMonth)}
            />
            <Input
              label={`Objectif ${monthLabelOf(monthKeys.next)} (€)`}
              inputMode="decimal"
              placeholder="0,00"
              value={nextMonth}
              onChange={edit(setNextMonth)}
            />
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
// E-mail & newsletter (PR3, §5.4 : `GET /admin/messaging/status`)
// ---------------------------------------------------------------------------

const EMAIL_PROVIDER_LABELS: Record<string, string> = {
  brevo: "Brevo",
  smtp: "SMTP",
  simulated: "Simulation",
};

function MessagingStatusCard() {
  const [status, setStatus] = useState<MessagingStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    api
      .get<MessagingStatus>("/api/admin/messaging/status")
      .then(setStatus)
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger l'état de la messagerie."))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  return (
    <Card
      title="E-mail & newsletter"
      subtitle="Envoi des tickets par e-mail et inscription à la newsletter — aucune information sensible n'est affichée ici."
      action={
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          Actualiser
        </Button>
      }
    >
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-3">
          <ErrorNotice message={error} />
          {status && (
            <>
              {status.email.provider === "simulated" && (
                <div role="alert" className="rounded-fc-lg bg-fc-warn-soft border border-fc-warn/40 px-3 py-2.5 text-sm font-medium text-fc-warn">
                  Les tickets ne sont PAS réellement envoyés (mode simulation).
                </div>
              )}
              <div className="flex flex-wrap items-center gap-4 text-sm text-fc-ink-soft">
                <span>
                  Envoi des tickets :{" "}
                  <span className="font-semibold text-fc-ink">
                    {EMAIL_PROVIDER_LABELS[status.email.provider] ?? status.email.provider}
                  </span>
                </span>
                <StatusPill ok={status.email.anonymous_tracking} okLabel="Suivi anonyme" koLabel="Suivi non anonyme" />
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <StatusPill ok={status.brevo_contacts.configured} okLabel="Newsletter active" koLabel="Newsletter non active" />
                <StatusPill ok={status.brevo_contacts.list_id_set} okLabel="Liste renseignée" koLabel="Liste non renseignée" />
                <StatusPill
                  ok={status.brevo_contacts.webhook_token_set}
                  okLabel="Désinscriptions sécurisées"
                  koLabel="Désinscriptions non sécurisées"
                />
              </div>
            </>
          )}
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
// Matériel — imprimante ticket MUNBYN + tiroir-caisse Safescan (PR3b).
// Aucun jargon technique : « Imprimante ticket », « Tiroir-caisse »,
// « USB (tablette) », « Réseau (Wi-Fi) » — jamais ESC/POS, WebUSB, MUNBYN
// ou Safescan dans les libellés visibles.
// ---------------------------------------------------------------------------

const EMPTY_HARDWARE: HardwareSettings = {
  printer_mode: "none",
  printer_host: "",
  printer_port: 9100,
  drawer_enabled: false,
  drawer_pin: 0,
  auto_print_on_sale: false,
  auto_kick_on_cash: false,
};

function HardwareSettingsCard() {
  const [form, setForm] = useState<HardwareSettings>(EMPTY_HARDWARE);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [status, setStatus] = useState<PrinterStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);

  const [testMessage, setTestMessage] = useState<string | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);

  const [kickMessage, setKickMessage] = useState<string | null>(null);
  const [kickError, setKickError] = useState<string | null>(null);
  const [kicking, setKicking] = useState(false);

  const [pairedLabel, setPairedLabel] = useState<string | null>(null);
  const [pairing, setPairing] = useState(false);
  const [pairError, setPairError] = useState<string | null>(null);

  const loadStatus = (): void => {
    setStatusLoading(true);
    api
      .get<PrinterStatus>("/api/hardware/printer/status")
      .then(setStatus)
      .catch(() => setStatus(null))
      .finally(() => setStatusLoading(false));
  };

  useEffect(() => {
    api
      .get<HardwareSettings>("/api/admin/settings/hardware")
      .then((data) => setForm({ ...EMPTY_HARDWARE, ...data }))
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger les réglages matériel."))
      .finally(() => setLoading(false));
    loadStatus();
    setPairedLabel(getStoredPrinter()?.label ?? null);
  }, []);

  const set = <K extends keyof HardwareSettings>(field: K, value: HardwareSettings[K]): void => {
    setForm((f) => ({ ...f, [field]: value }));
    setSaved(false);
  };

  const handleSave = async (): Promise<void> => {
    setSaving(true);
    setError(null);
    try {
      const data = await api.put<HardwareSettings>("/api/admin/settings/hardware", form);
      setForm({ ...EMPTY_HARDWARE, ...data });
      setSaved(true);
      loadStatus();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Échec de l'enregistrement.");
    } finally {
      setSaving(false);
    }
  };

  const handleTestPrint = async (): Promise<void> => {
    setTesting(true);
    setTestError(null);
    setTestMessage(null);
    try {
      if (form.printer_mode === "network") {
        const res = await api.post<ReceiptTestResponse>("/api/hardware/receipt/test", {});
        setTestMessage(`Ticket de test envoyé à l'imprimante (${res.host}:${res.port}).`);
      } else if (form.printer_mode === "webusb") {
        if (!isWebUsbSupported()) throw new Error("Impression USB non disponible sur cet appareil/navigateur.");
        const device = await findPairedUsbDevice();
        if (!device) throw new Error("Aucune imprimante USB couplée — associez-la d'abord.");
        const bytes = await api.getBytes("/api/hardware/receipt/test-escpos");
        await sendBytes(device, bytes);
        setTestMessage("Ticket de test envoyé à l'imprimante (USB).");
      }
    } catch (err) {
      setTestError(err instanceof ApiError ? err.detail : err instanceof Error ? err.message : "Échec du test.");
    } finally {
      setTesting(false);
    }
  };

  const handleTestKick = async (): Promise<void> => {
    setKicking(true);
    setKickError(null);
    setKickMessage(null);
    const result = await kickDrawer(form, "manual");
    setKicking(false);
    if (result.ok) setKickMessage(result.message);
    else setKickError(result.message);
  };

  const handlePair = async (): Promise<void> => {
    setPairing(true);
    setPairError(null);
    try {
      const info = await pairUsbPrinter();
      setPairedLabel(info.label);
    } catch (err) {
      setPairError(err instanceof Error ? err.message : "Échec du couplage.");
    } finally {
      setPairing(false);
    }
  };

  return (
    <Card title="Imprimante ticket" subtitle="Connexion, tiroir-caisse et comportement à la vente.">
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-5">
          <ErrorNotice message={error} />

          <label className="block max-w-xs">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Connexion</span>
            <select
              value={form.printer_mode}
              onChange={(e) => set("printer_mode", e.target.value as PrinterMode)}
              className="w-full min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
            >
              <option value="network">Réseau (Wi-Fi)</option>
              <option value="webusb">USB (tablette)</option>
              <option value="none">Désactivée</option>
            </select>
          </label>

          {form.printer_mode === "network" && (
            <div className="grid gap-4 sm:grid-cols-2">
              <Input
                label="Adresse IP de l'imprimante"
                value={form.printer_host}
                onChange={(e) => set("printer_host", e.target.value)}
                placeholder="192.168.1.50"
              />
              <Input
                label="Port"
                type="number"
                value={String(form.printer_port)}
                onChange={(e) => set("printer_port", Number(e.target.value) || 9100)}
              />
            </div>
          )}

          {form.printer_mode === "network" && (
            <div className="flex flex-wrap items-center gap-3">
              {statusLoading ? (
                <span className="text-sm text-fc-ink-soft">Vérification…</span>
              ) : (
                status && (
                  <StatusPill
                    ok={!!status.online}
                    okLabel={status.latency_ms !== null ? `En ligne (${status.latency_ms} ms)` : "En ligne"}
                    koLabel="Hors ligne"
                  />
                )
              )}
              <Button variant="outline" size="sm" onClick={loadStatus} disabled={statusLoading}>
                Actualiser l&apos;état
              </Button>
            </div>
          )}

          {form.printer_mode === "webusb" && (
            <div className="rounded-fc-lg bg-fc-bg-alt p-3 text-sm text-fc-ink-soft">
              {pairedLabel ? (
                <>
                  Imprimante couplée : <strong className="font-mono text-fc-ink">{pairedLabel}</strong>
                </>
              ) : (
                "Aucune imprimante USB couplée sur cette tablette."
              )}
            </div>
          )}

          <div className="space-y-3 border-t border-fc-line pt-4">
            <label className="flex items-center gap-2 text-sm font-medium text-fc-ink">
              <input
                type="checkbox"
                checked={form.drawer_enabled}
                onChange={(e) => set("drawer_enabled", e.target.checked)}
                className="h-5 w-5 rounded border-fc-line text-fc-primary focus:ring-fc-primary"
              />
              Activer le tiroir-caisse
            </label>
            {form.drawer_enabled && (
              <label className="block max-w-[200px]">
                <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">
                  Broche d&apos;impulsion
                </span>
                <select
                  value={form.drawer_pin}
                  onChange={(e) => set("drawer_pin", (Number(e.target.value) === 1 ? 1 : 0) as 0 | 1)}
                  className="w-full min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
                >
                  <option value={0}>0</option>
                  <option value={1}>1</option>
                </select>
                <span className="mt-1 block text-xs text-fc-ink-mute">Laissez 0 si vous ne savez pas.</span>
              </label>
            )}
          </div>

          <div className="space-y-3 border-t border-fc-line pt-4">
            <label className="flex items-center gap-2 text-sm text-fc-ink">
              <input
                type="checkbox"
                checked={form.auto_print_on_sale}
                onChange={(e) => set("auto_print_on_sale", e.target.checked)}
                className="h-5 w-5 rounded border-fc-line text-fc-primary focus:ring-fc-primary"
              />
              Imprimer automatiquement le ticket à chaque vente
            </label>
            <label className="flex items-center gap-2 text-sm text-fc-ink">
              <input
                type="checkbox"
                checked={form.auto_kick_on_cash}
                onChange={(e) => set("auto_kick_on_cash", e.target.checked)}
                className="h-5 w-5 rounded border-fc-line text-fc-primary focus:ring-fc-primary"
              />
              Ouvrir automatiquement le tiroir pour un paiement en espèces
            </label>
          </div>

          <div className="flex items-center gap-3">
            <Button onClick={() => void handleSave()} disabled={saving}>
              {saving ? "Enregistrement…" : "Enregistrer"}
            </Button>
            <SavedNotice show={saved} />
          </div>

          {form.printer_mode !== "none" && (
            <div className="space-y-3 border-t border-fc-line pt-4">
              <p className="text-sm font-medium text-fc-ink">Tester le matériel</p>
              <ErrorNotice message={testError} />
              {testMessage && !testError && <p className="text-sm text-fc-primary-deep">{testMessage}</p>}
              <ErrorNotice message={pairError} />
              <div className="flex flex-wrap gap-3">
                {form.printer_mode === "webusb" && (
                  <Button variant="outline" size="sm" onClick={() => void handlePair()} disabled={pairing}>
                    {pairing ? "Association…" : "Associer l'imprimante USB"}
                  </Button>
                )}
                <Button variant="outline" size="sm" onClick={() => void handleTestPrint()} disabled={testing}>
                  {testing ? "Envoi…" : form.printer_mode === "webusb" ? "Test USB" : "Imprimer un ticket de test"}
                </Button>
                {form.drawer_enabled && (
                  <Button variant="outline" size="sm" onClick={() => void handleTestKick()} disabled={kicking}>
                    {kicking ? "Ouverture…" : "Ouvrir le tiroir"}
                  </Button>
                )}
              </div>
              <ErrorNotice message={kickError} />
              {kickMessage && !kickError && <p className="text-sm text-fc-primary-deep">{kickMessage}</p>}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Rapports de clôture (Z)
// ---------------------------------------------------------------------------

function ZReportsCard() {
  const [list, setList] = useState<ZReport[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pdfError, setPdfError] = useState<string | null>(null);
  const [pdfBusyId, setPdfBusyId] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<{ z_reports: ZReport[] }>("/api/pos/z-reports")
      .then((data) => setList(data.z_reports))
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger les clôtures."))
      .finally(() => setLoading(false));
  }, []);

  const handlePdf = async (z: ZReport): Promise<void> => {
    setPdfBusyId(z.id);
    setPdfError(null);
    try {
      await downloadFile(`/api/pos/z-reports/${z.id}/pdf`, `Z${String(z.report_number).padStart(4, "0")}.pdf`);
    } catch (err) {
      setPdfError(err instanceof ApiError ? err.detail : "Échec du téléchargement du PDF.");
    } finally {
      setPdfBusyId(null);
    }
  };

  return (
    <Card title="Clôtures de caisse" subtitle="Historique des rapports Z, du plus récent au plus ancien.">
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-2">
          <ErrorNotice message={error} />
          <ErrorNotice message={pdfError} />
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
                    <th className="py-2 pr-4" />
                  </tr>
                </thead>
                <tbody>
                  {list.map((z) => (
                    <tr key={z.id} className="border-t border-fc-line">
                      <td className="py-2 pr-4 font-mono tabular-nums">{z.report_number}</td>
                      <td className="py-2 pr-4">{formatDateTime(z.closed_at)}</td>
                      <td className="py-2 pr-4 font-mono tabular-nums">{formatCurrency(z.total_net)}</td>
                      <td className="py-2 pr-4 font-mono tabular-nums">{formatCurrency(z.expected_amount)}</td>
                      <td className="py-2 pr-4 font-mono tabular-nums">{formatCurrency(z.closing_amount)}</td>
                      <td className={`py-2 pr-4 font-mono tabular-nums ${Math.abs(z.discrepancy) > 2 ? "text-fc-warn" : ""}`}>
                        {z.discrepancy > 0 ? "+" : ""}
                        {formatCurrency(z.discrepancy)}
                      </td>
                      <td className="py-2 pr-4">
                        <Button variant="outline" onClick={() => void handlePdf(z)} disabled={pdfBusyId === z.id}>
                          {pdfBusyId === z.id ? "Préparation…" : "PDF"}
                        </Button>
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
  "backup.deleted": "Sauvegarde de la base supprimée",
};

function describeEvent(type: string): string {
  return EVENT_LABELS[type] ?? type;
}

// ---------------------------------------------------------------------------
// Onglet Clients (PR3, §5 ARCHITECTURE_PR3.md)
// ---------------------------------------------------------------------------

const CONSENT_SOURCE_LABELS: Record<string, string> = {
  pos: "Caisse",
  webhook: "Désinscription en ligne",
  admin: "Administration",
  rgpd: "Suppression RGPD",
};

const COMMUNICATION_STATUS_LABELS: Record<string, string> = {
  sent: "Envoyé",
  failed: "Échec",
  simulated: "Simulé",
};

function ClientsSection() {
  const [query, setQuery] = useState("");
  const [list, setList] = useState<Client[]>([]);
  const [loadingList, setLoadingList] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const search = (q: string) => {
    setLoadingList(true);
    setListError(null);
    const qs = new URLSearchParams({ limit: "50" });
    if (q.trim()) qs.set("q", q.trim());
    api
      .get<{ clients: Client[] }>(`/api/admin/clients?${qs.toString()}`)
      .then((data) => setList(data.clients))
      .catch((err) => setListError(err instanceof ApiError ? err.detail : "Impossible de charger les clients."))
      .finally(() => setLoadingList(false));
  };

  useEffect(() => search(""), []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="grid items-start gap-6 lg:grid-cols-[340px_1fr]">
      <Card title="Clients" subtitle="Recherche par e-mail ou par nom.">
        <div className="space-y-3">
          <Input
            label="Rechercher"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") search(query);
            }}
            placeholder="julie@exemple.fr ou Dupont"
          />
          <Button variant="outline" size="sm" onClick={() => search(query)} disabled={loadingList}>
            {loadingList ? "Recherche…" : "Rechercher"}
          </Button>
          <ErrorNotice message={listError} />
          {!loadingList && list.length === 0 && <p className="text-sm text-fc-ink-soft">Aucun client.</p>}
          <ul className="divide-y divide-fc-line max-h-[520px] overflow-y-auto -mx-1">
            {list.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => setSelectedId(c.id)}
                  aria-pressed={selectedId === c.id}
                  className={`w-full min-h-touch rounded-fc px-3 py-2.5 text-left transition-colors ${
                    selectedId === c.id ? "bg-fc-primary-soft text-fc-primary-deep" : "hover:bg-fc-bg-alt text-fc-ink"
                  }`}
                >
                  <div className="text-sm font-medium truncate">{c.anonymized_at ? "Client anonymisé" : c.email}</div>
                  <div className="text-xs text-fc-ink-mute truncate">
                    {[c.first_name, c.last_name].filter(Boolean).join(" ") || "—"} ·{" "}
                    {c.newsletter_optin ? "Newsletter : oui" : "Newsletter : non"}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </Card>

      {selectedId ? (
        <ClientDetailCard key={selectedId} clientId={selectedId} onChanged={() => search(query)} />
      ) : (
        <Card title="Fiche client" subtitle="Sélectionnez un client dans la liste pour voir le détail.">
          <p className="text-sm text-fc-ink-soft">Aucun client sélectionné.</p>
        </Card>
      )}
    </div>
  );
}

function ClientDetailCard({ clientId, onChanged }: { clientId: string; onChanged: () => void }) {
  const [data, setData] = useState<ClientFull | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [consentBusy, setConsentBusy] = useState(false);
  const [consentError, setConsentError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    api
      .get<ClientFull>(`/api/admin/clients/${clientId}`)
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger la fiche client."))
      .finally(() => setLoading(false));
  };

  useEffect(load, [clientId]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleToggleNewsletter = async (granted: boolean): Promise<void> => {
    setConsentBusy(true);
    setConsentError(null);
    try {
      const body: ConsentUpdateRequest = {
        purpose: "newsletter",
        granted,
        note: granted ? "Demande orale du client" : undefined,
      };
      await api.post(`/api/admin/clients/${clientId}/consents`, body);
      load();
      onChanged();
    } catch (err) {
      setConsentError(err instanceof ApiError ? err.detail : "Échec de la mise à jour.");
    } finally {
      setConsentBusy(false);
    }
  };

  if (loading) {
    return (
      <Card title="Fiche client">
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      </Card>
    );
  }

  if (error || !data) {
    return (
      <Card title="Fiche client">
        <ErrorNotice message={error ?? "Client introuvable."} />
      </Card>
    );
  }

  const { client, consents, communications, transactions } = data;
  const fullName = [client.first_name, client.last_name].filter(Boolean).join(" ");

  return (
    <div className="space-y-6">
      <Card title={client.anonymized_at ? "Client anonymisé" : client.email} subtitle={client.anonymized_at ? undefined : fullName || undefined}>
        {client.anonymized_at ? (
          <div className="rounded-fc-lg bg-fc-bg-alt p-4 text-sm text-fc-ink-soft">
            Données supprimées le {formatDateTime(client.anonymized_at)}.
          </div>
        ) : (
          <div className="space-y-4">
            <ErrorNotice message={consentError} />
            <div className="flex flex-wrap items-center gap-4">
              <StatusPill ok={client.newsletter_optin} okLabel="Inscrit à la newsletter" koLabel="Non inscrit à la newsletter" />
              <span className="text-xs text-fc-ink-mute">Client depuis le {formatDateTime(client.created_at)}</span>
            </div>
            <div className="flex flex-wrap gap-3">
              {client.newsletter_optin ? (
                <Button variant="outline" size="sm" onClick={() => void handleToggleNewsletter(false)} disabled={consentBusy}>
                  Retirer de la newsletter
                </Button>
              ) : (
                <Button variant="outline" size="sm" onClick={() => void handleToggleNewsletter(true)} disabled={consentBusy}>
                  Inscrire (demande orale du client)
                </Button>
              )}
            </div>
          </div>
        )}
      </Card>

      {!client.anonymized_at && (
        <>
          <Card title="Historique des consentements">
            {consents.length === 0 ? (
              <p className="text-sm text-fc-ink-soft">Aucun consentement enregistré.</p>
            ) : (
              <SimpleTable
                columns={["Date", "Newsletter", "Origine"]}
                rows={consents.map((c) => [formatDateTime(c.created_at), c.granted ? "Oui" : "Non", CONSENT_SOURCE_LABELS[c.source] ?? c.source])}
              />
            )}
          </Card>

          <Card title="Envois de ticket">
            {communications.length === 0 ? (
              <p className="text-sm text-fc-ink-soft">Aucun envoi.</p>
            ) : (
              <SimpleTable
                columns={["Date", "Destinataire", "Statut"]}
                rows={communications.map((c) => [
                  formatDateTime(c.created_at),
                  c.recipient,
                  COMMUNICATION_STATUS_LABELS[c.status] ?? c.status,
                ])}
              />
            )}
          </Card>

          <Card title="Tickets liés">
            {transactions.length === 0 ? (
              <p className="text-sm text-fc-ink-soft">Aucun ticket lié.</p>
            ) : (
              <SimpleTable
                columns={["N°", "Date", "Montant"]}
                rows={transactions.map((t) => [String(t.transaction_number), formatDateTime(t.created_at), formatCurrency(t.total_ttc)])}
              />
            )}
          </Card>

          <RgpdCard clientId={clientId} onAnonymized={() => { load(); onChanged(); }} />
        </>
      )}
    </div>
  );
}

function RgpdCard({ clientId, onAnonymized }: { clientId: string; onAnonymized: () => void }) {
  const [exportJson, setExportJson] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const [reason, setReason] = useState("");
  const [confirmStep, setConfirmStep] = useState(false);
  const [anonymizing, setAnonymizing] = useState(false);
  const [anonymizeError, setAnonymizeError] = useState<string | null>(null);

  const handleExport = async (): Promise<void> => {
    setExporting(true);
    setExportError(null);
    try {
      const data = await api.get<Record<string, unknown>>(`/api/admin/clients/${clientId}/export`);
      setExportJson(JSON.stringify(data, null, 2));
    } catch (err) {
      setExportError(err instanceof ApiError ? err.detail : "Échec de l'export.");
    } finally {
      setExporting(false);
    }
  };

  const handleAnonymize = async (): Promise<void> => {
    if (reason.trim().length < 3) return;
    setAnonymizing(true);
    setAnonymizeError(null);
    try {
      const body: AnonymizeRequest = { reason: reason.trim() };
      await api.post(`/api/admin/clients/${clientId}/anonymize`, body);
      setConfirmStep(false);
      setReason("");
      onAnonymized();
    } catch (err) {
      setAnonymizeError(err instanceof ApiError ? err.detail : "Échec de la suppression.");
    } finally {
      setAnonymizing(false);
    }
  };

  return (
    <Card title="Données personnelles (RGPD)" subtitle="Export portable et suppression des coordonnées.">
      <div className="space-y-5">
        <div className="space-y-2">
          <ErrorNotice message={exportError} />
          <Button variant="outline" size="sm" onClick={() => void handleExport()} disabled={exporting}>
            {exporting ? "Export…" : "Exporter les données (RGPD)"}
          </Button>
          {exportJson && (
            <textarea
              readOnly
              value={exportJson}
              rows={10}
              onFocus={(e) => e.currentTarget.select()}
              aria-label="Export RGPD (JSON)"
              className="w-full rounded-fc border border-fc-line bg-fc-bg-alt px-3 py-2 font-mono text-xs text-fc-ink"
            />
          )}
        </div>

        <div className="space-y-3 border-t border-fc-line pt-4">
          <ErrorNotice message={anonymizeError} />
          <label className="block">
            <span className="mb-1.5 block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft">Motif de la suppression</span>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={2}
              placeholder="Ex. demande écrite du client du 12/09"
              className="w-full rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-sm text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
            />
          </label>
          {!confirmStep ? (
            <Button variant="danger" size="sm" disabled={reason.trim().length < 3} onClick={() => setConfirmStep(true)}>
              Supprimer les données (RGPD)
            </Button>
          ) : (
            <div className="space-y-3 rounded-fc-lg border border-fc-danger/40 bg-fc-danger-soft p-4">
              <p className="text-sm font-semibold text-fc-danger">
                Cette action est irréversible : les coordonnées seront effacées, les tickets conservés anonymisés.
              </p>
              <p className="text-sm text-fc-danger">Confirmez-vous la suppression définitive des coordonnées de ce client ?</p>
              <div className="flex gap-3">
                <Button variant="outline" size="sm" onClick={() => setConfirmStep(false)} disabled={anonymizing}>
                  Annuler
                </Button>
                <Button variant="danger" size="sm" onClick={() => void handleAnonymize()} disabled={anonymizing}>
                  {anonymizing ? "Suppression…" : "Confirmer la suppression"}
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}

function SimpleTable({ columns, rows }: { columns: string[]; rows: string[][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-fc-ink-mute uppercase text-xs tracking-wide">
            {columns.map((c) => (
              <th key={c} className="py-2 pr-4">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-t border-fc-line">
              {row.map((cell, j) => (
                <td key={j} className="py-2 pr-4">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
