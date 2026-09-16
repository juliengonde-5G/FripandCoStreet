"use client";

/**
 * Onglet Comptabilité (PR4, docs/ARCHITECTURE_PR4.md §5) : réglages des
 * comptes comptables, écritures du mois (une par clôture Z) et exports
 * bruts par table. Vocabulaire sans jargon : « écritures comptables »,
 * « fichier FEC », jamais « journal des événements » exposé comme tel côté
 * table brute (« Journal des événements » reste le libellé déjà utilisé
 * ailleurs dans l'admin, cf. CDC §3.2 — c'est un nom d'écran, pas du jargon
 * technique).
 *
 * PR8 (J6) : carte « Factures » — factures pro et avoirs d'une année, avec
 * le PDF de chaque document (`GET /api/admin/invoices?year=`).
 *
 * PR12 (N1) : carte « Journal » en tête — toutes les écritures ligne à
 * ligne sur une période, avec le cumul débit/crédit et son équilibre.
 */
import React, { useEffect, useMemo, useState } from "react";

import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import Modal from "@/components/ui/Modal";
import ErrorNotice from "@/components/ui/ErrorNotice";
import { api } from "@/lib/api";
import { describeError, type DisplayableError } from "@/lib/apiError";
import { downloadFile } from "@/lib/download";
import { formatCurrency, formatDate } from "@/lib/format";
import { downloadInvoicePdf, invoiceAmount, invoiceKindLabel, listInvoices } from "@/lib/invoices";
import {
  fetchAccountingJournal,
  isZeroAmount,
  journalAmount,
  monthlyCsvFilename,
  monthlyCsvUrl,
  monthRange,
  shiftMonth,
  type AccountingJournal,
} from "@/lib/accounting";
import { formatSiret } from "@/lib/siret";
import type { Invoice } from "@/lib/types";
import {
  EXPORTABLE_TABLES,
  type AccountingExportDetail,
  type AccountingExportSummary,
  type AccountingExportsMonthResponse,
  type AccountingSettings,
  type ExportableTable,
} from "@/lib/types";

function SavedNotice({ show }: { show: boolean }) {
  if (!show) return null;
  return <span className="text-sm text-fc-success font-medium">Enregistré.</span>;
}

const selectClass =
  "w-full min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary";

const MONTH_LABELS = [
  "Janvier",
  "Février",
  "Mars",
  "Avril",
  "Mai",
  "Juin",
  "Juillet",
  "Août",
  "Septembre",
  "Octobre",
  "Novembre",
  "Décembre",
];

function yearOptions(): number[] {
  const current = new Date().getFullYear();
  return [current, current - 1, current - 2];
}

export default function AccountingTab() {
  return (
    <div className="space-y-6">
      <JournalCard />
      <AccountingSettingsCard />
      {/* Cible du renvoi depuis l'avertissement « Z sans export » de la
          carte « Journal » — `scroll-mt` laisse le titre sous l'en-tête. */}
      <div id={ENTRIES_CARD_ID} className="scroll-mt-24">
        <AccountingEntriesCard />
      </div>
      <InvoicesCard />
      <RawTableExportsCard />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Journal comptable (PR12, N1)
// ---------------------------------------------------------------------------

/** Identifiant d'ancre de la carte des écritures : l'avertissement « Z sans
 * export » y renvoie, pour que la personne aille vérifier la clôture
 * concernée sans chercher dans la page. */
const ENTRIES_CARD_ID = "ecritures-comptables";

/** Hauteur du cadre défilant du journal : le tableau défile dans son cadre,
 * jamais la page — les filtres et le pied Totaux restent à l'écran. */
const JOURNAL_SCROLL_CLASS = "max-h-[400px] overflow-y-auto overflow-x-auto";

function JournalCard() {
  const now = useMemo(() => new Date(), []);
  const [year, setYear] = useState(now.getFullYear());
  const [month, setMonth] = useState(now.getMonth() + 1);
  // Saisie brute du filtre de compte + valeur réellement envoyée : on
  // attend une courte pause de frappe pour ne pas interroger l'API à
  // chaque caractère.
  const [accountInput, setAccountInput] = useState("");
  const [account, setAccount] = useState("");

  const [journal, setJournal] = useState<AccountingJournal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<DisplayableError>(null);

  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<DisplayableError>(null);
  const [downloadOk, setDownloadOk] = useState<string | null>(null);

  const period = useMemo(() => monthRange(year, month), [year, month]);
  const monthLabel = `${MONTH_LABELS[month - 1]} ${year}`;

  useEffect(() => {
    const timer = setTimeout(() => setAccount(accountInput.trim()), 300);
    return () => clearTimeout(timer);
  }, [accountInput]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchAccountingJournal({ from: period.from, to: period.to, account })
      .then((data) => {
        if (!cancelled) setJournal(data);
      })
      .catch((err) => {
        if (cancelled) return;
        setJournal(null);
        setError(describeError(err, "Impossible de charger le journal comptable."));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [period.from, period.to, account]);

  const goToMonth = (delta: number): void => {
    const next = shiftMonth(year, month, delta);
    setYear(next.year);
    setMonth(next.month);
  };

  const handleCsv = async (): Promise<void> => {
    setDownloading(true);
    setDownloadError(null);
    setDownloadOk(null);
    const filename = monthlyCsvFilename(year, month);
    try {
      await downloadFile(monthlyCsvUrl(year, month), filename);
      setDownloadOk(`${filename} téléchargé.`);
    } catch (err) {
      setDownloadError(describeError(err, "Échec du téléchargement."));
    } finally {
      setDownloading(false);
    }
  };

  const lines = journal?.lines ?? [];
  const totals = journal?.totals ?? null;
  const missing = journal?.z_without_export ?? [];

  return (
    <Card
      title="Journal"
      subtitle="Toutes les écritures de la période, ligne à ligne, telles qu'elles seront remises au comptable."
    >
      <div className="space-y-4">
        <ErrorNotice message={error} />

        <div className="flex flex-wrap items-end gap-3">
          <div>
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Période</span>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => goToMonth(-1)} aria-label="Mois précédent">
                ‹
              </Button>
              <span className="min-w-[140px] text-center text-sm font-semibold text-fc-ink">{monthLabel}</span>
              <Button variant="outline" size="sm" onClick={() => goToMonth(1)} aria-label="Mois suivant">
                ›
              </Button>
            </div>
          </div>
          <label className="block">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Compte</span>
            <input
              value={accountInput}
              onChange={(e) => setAccountInput(e.target.value)}
              inputMode="numeric"
              maxLength={8}
              placeholder="Ex. 7"
              aria-label="Filtrer par numéro de compte (début du numéro)"
              className="w-full max-w-[140px] min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary font-mono"
            />
          </label>
          <Button variant="outline" onClick={() => void handleCsv()} disabled={downloading}>
            {downloading ? "Préparation…" : "CSV du mois"}
          </Button>
        </div>

        <ErrorNotice message={downloadError} />
        {downloadOk && !downloadError && <p className="text-sm text-fc-primary-deep">{downloadOk}</p>}

        {missing.length > 0 && (
          <div className="rounded-fc bg-fc-warn-soft border border-fc-warn/30 px-3 py-2 text-sm text-fc-warn">
            {missing.length === 1 ? "Clôture sans écriture enregistrée : " : "Clôtures sans écriture enregistrée : "}
            <span className="font-mono tabular-nums">{missing.map((n) => `Z${String(n).padStart(4, "0")}`).join(", ")}</span>
            {missing.length === 1 ? " — ses lignes sont recalculées ici. " : " — leurs lignes sont recalculées ici. "}
            <a href={`#${ENTRIES_CARD_ID}`} className="underline font-medium">
              Voir les écritures comptables
            </a>
          </div>
        )}

        {loading ? (
          <p className="text-sm text-fc-ink-soft">Chargement…</p>
        ) : error ? null : lines.length === 0 ? (
          <p className="text-sm text-fc-ink-soft">
            Aucune écriture sur cette période{account ? ` pour les comptes commençant par ${account}` : ""}.
          </p>
        ) : (
          <div className={`rounded-fc-lg border border-fc-line ${JOURNAL_SCROLL_CLASS}`}>
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-fc-surface border-b border-fc-line">
                <tr className="text-left text-fc-ink-mute uppercase text-xs tracking-wide">
                  <th className="py-2 px-3 whitespace-nowrap">Date</th>
                  <th className="py-2 px-3 whitespace-nowrap">Z</th>
                  <th className="py-2 px-3 whitespace-nowrap">Compte</th>
                  <th className="py-2 px-3">Libellé</th>
                  <th className="py-2 px-3 text-right whitespace-nowrap">Débit</th>
                  <th className="py-2 px-3 text-right whitespace-nowrap">Crédit</th>
                </tr>
              </thead>
              <tbody>
                {lines.map((line, index) => (
                  <tr key={`${line.z_report_id}-${line.account_number}-${index}`} className="border-t border-fc-line align-top">
                    <td className="py-2 px-3 whitespace-nowrap">{formatDate(line.date)}</td>
                    <td className="py-2 px-3 font-mono tabular-nums whitespace-nowrap">
                      {`Z${String(line.z_report_number).padStart(4, "0")}`}
                      {line.source === "computed" && (
                        <span className="block text-[11px] font-sans text-fc-warn">recalculé</span>
                      )}
                    </td>
                    <td className="py-2 px-3 font-mono tabular-nums whitespace-nowrap">
                      {line.account_number}
                      <span className="block text-xs text-fc-ink-mute font-sans">{line.account_label}</span>
                    </td>
                    <td className="py-2 px-3 text-fc-ink-soft">{line.label}</td>
                    <td className="py-2 px-3 font-mono tabular-nums text-right whitespace-nowrap">
                      {isZeroAmount(line.debit) ? "" : formatCurrency(journalAmount(line.debit))}
                    </td>
                    <td className="py-2 px-3 font-mono tabular-nums text-right whitespace-nowrap">
                      {isZeroAmount(line.credit) ? "" : formatCurrency(journalAmount(line.credit))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {totals && !loading && !error && (
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-fc-line pt-3">
            <span className="text-sm font-semibold text-fc-ink">Totaux</span>
            <div className="flex flex-wrap items-center gap-4">
              <span className="text-sm text-fc-ink-soft">
                Débit <span className="font-mono tabular-nums font-semibold text-fc-ink">{formatCurrency(journalAmount(totals.debit))}</span>
              </span>
              <span className="text-sm text-fc-ink-soft">
                Crédit <span className="font-mono tabular-nums font-semibold text-fc-ink">{formatCurrency(journalAmount(totals.credit))}</span>
              </span>
              <span
                className={`inline-flex items-center gap-1.5 rounded-fc px-2.5 py-1 text-xs font-medium ${
                  totals.balanced ? "bg-fc-success-soft text-fc-success" : "bg-fc-danger-soft text-fc-danger"
                }`}
              >
                {totals.balanced ? "✔ Équilibré" : "⚠ Déséquilibré"}
              </span>
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Comptes comptables (F1)
// ---------------------------------------------------------------------------

/** Comptes avec un libellé éditable (F1, `AccountingSettingsIn` côté
 * backend) — numéro + libellé forment une paire. */
const LABELED_ACCOUNT_FIELDS: { field: keyof AccountingSettings; labelField: keyof AccountingSettings; title: string }[] = [
  { field: "account_sales", labelField: "label_sales", title: "Ventes (707)" },
  { field: "account_tva", labelField: "label_tva", title: "TVA collectée (44571)" },
  { field: "account_cash", labelField: "label_cash", title: "Caisse (531)" },
  { field: "account_card", labelField: "label_card", title: "Carte bancaire (512)" },
];

/** Comptes d'ajustement d'arrondi — pas de libellé éditable côté backend. */
const ROUNDING_ACCOUNT_FIELDS: { field: keyof AccountingSettings; label: string; help: string }[] = [
  { field: "account_rounding_expense", label: "Compte d'arrondi en charge (658)", help: "Ajustement d'arrondi défavorable" },
  { field: "account_rounding_income", label: "Compte d'arrondi en produit (758)", help: "Ajustement d'arrondi favorable" },
];

const EMPTY_ACCOUNTING: AccountingSettings = {
  journal_code: "",
  account_sales: "",
  label_sales: "",
  account_tva: "",
  label_tva: "",
  account_cash: "",
  label_cash: "",
  account_card: "",
  label_card: "",
  account_rounding_expense: "",
  account_rounding_income: "",
};

function AccountingSettingsCard() {
  const [form, setForm] = useState<AccountingSettings>(EMPTY_ACCOUNTING);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<DisplayableError>(null);

  useEffect(() => {
    api
      .get<AccountingSettings>("/api/admin/settings/accounting")
      .then((data) => setForm({ ...EMPTY_ACCOUNTING, ...data }))
      .catch((err) => setError(describeError(err, "Impossible de charger les comptes comptables.")))
      .finally(() => setLoading(false));
  }, []);

  const set = (field: keyof AccountingSettings) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setForm((f) => ({ ...f, [field]: e.target.value }));
    setSaved(false);
  };

  const handleSave = async (): Promise<void> => {
    setSaving(true);
    setError(null);
    try {
      const data = await api.put<AccountingSettings>("/api/admin/settings/accounting", form);
      setForm({ ...EMPTY_ACCOUNTING, ...data });
      setSaved(true);
    } catch (err) {
      setError(describeError(err, "Échec de l'enregistrement."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card
      title="Comptes comptables"
      subtitle="Code journal et comptes utilisés pour générer une écriture à chaque clôture de caisse."
    >
      {loading ? (
        <p className="text-sm text-fc-ink-soft">Chargement…</p>
      ) : (
        <div className="space-y-4">
          <ErrorNotice message={error} />
          <label className="block max-w-[160px]">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Code journal</span>
            <input
              value={form.journal_code}
              onChange={set("journal_code")}
              maxLength={5}
              className="w-full min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary font-mono"
            />
          </label>
          <div className="grid gap-4 sm:grid-cols-2">
            {LABELED_ACCOUNT_FIELDS.map(({ field, labelField, title }) => (
              <div key={field} className="rounded-fc-lg border border-fc-line p-3 space-y-3">
                <span className="block text-sm font-semibold text-fc-ink">{title}</span>
                <label className="block">
                  <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Numéro de compte</span>
                  <input
                    value={form[field]}
                    onChange={set(field)}
                    maxLength={8}
                    inputMode="numeric"
                    className="w-full min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary font-mono"
                  />
                </label>
                <label className="block">
                  <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Libellé</span>
                  <input
                    value={form[labelField]}
                    onChange={set(labelField)}
                    maxLength={100}
                    className="w-full min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
                  />
                </label>
              </div>
            ))}
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            {ROUNDING_ACCOUNT_FIELDS.map(({ field, label, help }) => (
              <label key={field} className="block">
                <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">{label}</span>
                <input
                  value={form[field]}
                  onChange={set(field)}
                  maxLength={8}
                  inputMode="numeric"
                  className="w-full min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary font-mono"
                />
                <span className="mt-1 block text-xs text-fc-ink-mute">{help}</span>
              </label>
            ))}
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
// Écritures du mois + téléchargements (F2, F3, F7)
// ---------------------------------------------------------------------------

function AccountingEntriesCard() {
  const now = useMemo(() => new Date(), []);
  const [year, setYear] = useState(now.getFullYear());
  const [month, setMonth] = useState(now.getMonth() + 1);
  const [day, setDay] = useState(() => now.toISOString().slice(0, 10));

  const [list, setList] = useState<AccountingExportSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<DisplayableError>(null);

  const [downloading, setDownloading] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<DisplayableError>(null);
  const [downloadOk, setDownloadOk] = useState<string | null>(null);

  const [detail, setDetail] = useState<AccountingExportDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<DisplayableError>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    api
      .get<AccountingExportsMonthResponse>(`/api/admin/accounting/exports?year=${year}&month=${month}`)
      .then((data) => setList(data.exports))
      .catch((err) => setError(describeError(err, "Impossible de charger les écritures du mois.")))
      .finally(() => setLoading(false));
  };

  useEffect(load, [year, month]); // eslint-disable-line react-hooks/exhaustive-deps

  const monthLabel = `${MONTH_LABELS[month - 1]} ${year}`;

  const runDownload = async (kind: string, path: string, filename: string): Promise<void> => {
    setDownloading(kind);
    setDownloadError(null);
    setDownloadOk(null);
    try {
      await downloadFile(path, filename);
      setDownloadOk(`${filename} téléchargé.`);
    } catch (err) {
      setDownloadError(describeError(err, "Échec du téléchargement."));
    } finally {
      setDownloading(null);
    }
  };

  const monthKey = `${year}-${String(month).padStart(2, "0")}`;

  const openDetail = (zReportId: string): void => {
    setDetailLoading(true);
    setDetailError(null);
    setDetail(null);
    api
      .get<AccountingExportDetail>(`/api/admin/accounting/exports/${zReportId}`)
      .then(setDetail)
      .catch((err) => setDetailError(describeError(err, "Impossible de charger le détail de l'écriture.")))
      .finally(() => setDetailLoading(false));
  };

  return (
    <Card title="Écritures comptables" subtitle="Une écriture est générée automatiquement à chaque clôture de caisse.">
      <div className="space-y-5">
        <ErrorNotice message={error} />
        <div className="flex flex-wrap items-end gap-3">
          <label className="block">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Mois</span>
            <select value={month} onChange={(e) => setMonth(Number(e.target.value))} className={`${selectClass} min-w-[160px]`}>
              {MONTH_LABELS.map((label, i) => (
                <option key={label} value={i + 1}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Année</span>
            <select value={year} onChange={(e) => setYear(Number(e.target.value))} className={`${selectClass} min-w-[110px]`}>
              {yearOptions().map((y) => (
                <option key={y} value={y}>
                  {y}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="flex flex-wrap gap-3">
          <Button
            variant="outline"
            onClick={() =>
              void runDownload("csv", `/api/admin/accounting/monthly-csv/${year}/${month}`, `ecritures_${monthKey}.csv`)
            }
            disabled={downloading !== null}
          >
            {downloading === "csv" ? "Préparation…" : "Télécharger le CSV Pennylane"}
          </Button>
          <Button
            variant="outline"
            onClick={() =>
              void runDownload("fec-month", `/api/admin/accounting/fec/month/${year}/${month}`, `FEC_${monthKey}.txt`)
            }
            disabled={downloading !== null}
          >
            {downloading === "fec-month" ? "Préparation…" : "Télécharger le fichier FEC du mois"}
          </Button>
        </div>

        <div className="flex flex-wrap items-end gap-3 border-t border-fc-line pt-4">
          <label className="block">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Jour</span>
            <input
              type="date"
              value={day}
              onChange={(e) => setDay(e.target.value)}
              className="min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
            />
          </label>
          <Button
            variant="outline"
            onClick={() => void runDownload("fec-day", `/api/admin/accounting/fec/day/${day}`, `FEC_${day.replaceAll("-", "")}.txt`)}
            disabled={downloading !== null || !day}
          >
            {downloading === "fec-day" ? "Préparation…" : "Fichier FEC du jour"}
          </Button>
        </div>

        <ErrorNotice message={downloadError} />
        {downloadOk && !downloadError && <p className="text-sm text-fc-primary-deep">{downloadOk}</p>}

        <div className="border-t border-fc-line pt-4">
          <h4 className="text-sm font-semibold text-fc-ink mb-3">Écritures de {monthLabel}</h4>
          {loading ? (
            <p className="text-sm text-fc-ink-soft">Chargement…</p>
          ) : list.length === 0 ? (
            <p className="text-sm text-fc-ink-soft">Aucune écriture pour cette période.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-fc-ink-mute uppercase text-xs tracking-wide">
                    <th className="py-2 pr-4">Z</th>
                    <th className="py-2 pr-4">Date</th>
                    <th className="py-2 pr-4">Débit</th>
                    <th className="py-2 pr-4">Crédit</th>
                    <th className="py-2 pr-4">Équilibre</th>
                    <th className="py-2 pr-4" />
                  </tr>
                </thead>
                <tbody>
                  {list.map((exp) => (
                    <tr key={exp.id} className="border-t border-fc-line">
                      <td className="py-2 pr-4 font-mono tabular-nums">{exp.z_number}</td>
                      <td className="py-2 pr-4">{formatDate(exp.export_date)}</td>
                      <td className="py-2 pr-4 font-mono tabular-nums">{formatCurrency(exp.total_debit)}</td>
                      <td className="py-2 pr-4 font-mono tabular-nums">{formatCurrency(exp.total_credit)}</td>
                      <td className="py-2 pr-4">
                        <span
                          className={`inline-flex items-center gap-1.5 rounded-fc px-2.5 py-1 text-xs font-medium ${
                            exp.balanced ? "bg-fc-primary-soft text-fc-primary-deep" : "bg-fc-warn-soft text-fc-warn"
                          }`}
                        >
                          {exp.balanced ? "✔ Équilibrée" : "⚠ Écart"}
                        </span>
                      </td>
                      <td className="py-2 pr-4">
                        <Button variant="outline" onClick={() => openDetail(exp.z_report_id)}>
                          Détail
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <Modal
        open={detailLoading || detail !== null || detailError !== null}
        onClose={() => {
          setDetail(null);
          setDetailError(null);
        }}
        title={detail ? `Écriture Z${String(detail.z_number).padStart(4, "0")}` : "Écriture comptable"}
      >
        {detailLoading && <p className="text-sm text-fc-ink-soft">Chargement…</p>}
        <ErrorNotice message={detailError} />
        {detail && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-fc-ink-mute uppercase text-xs tracking-wide">
                  <th className="py-2 pr-4">Compte</th>
                  <th className="py-2 pr-4">Libellé</th>
                  <th className="py-2 pr-4">Débit</th>
                  <th className="py-2 pr-4">Crédit</th>
                </tr>
              </thead>
              <tbody>
                {detail.lines.map((line) => (
                  <tr key={line.line_number} className="border-t border-fc-line">
                    <td className="py-2 pr-4 font-mono tabular-nums whitespace-nowrap">
                      {line.account_number}
                      <span className="block text-xs text-fc-ink-mute font-sans">{line.account_label}</span>
                    </td>
                    <td className="py-2 pr-4">{line.label}</td>
                    <td className="py-2 pr-4 font-mono tabular-nums">{line.debit > 0 ? formatCurrency(line.debit) : ""}</td>
                    <td className="py-2 pr-4 font-mono tabular-nums">{line.credit > 0 ? formatCurrency(line.credit) : ""}</td>
                  </tr>
                ))}
                <tr className="border-t border-fc-line font-semibold">
                  <td className="py-2 pr-4" colSpan={2}>
                    Totaux
                  </td>
                  <td className="py-2 pr-4 font-mono tabular-nums">{formatCurrency(detail.total_debit)}</td>
                  <td className="py-2 pr-4 font-mono tabular-nums">{formatCurrency(detail.total_credit)}</td>
                </tr>
              </tbody>
            </table>
          </div>
        )}
      </Modal>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Factures pro et avoirs (PR8, J6)
// ---------------------------------------------------------------------------

function InvoicesCard() {
  const [year, setYear] = useState(() => new Date().getFullYear());
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<DisplayableError>(null);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<DisplayableError>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    listInvoices(year)
      .then(setInvoices)
      .catch((err) => setError(describeError(err, "Impossible de charger les factures.")))
      .finally(() => setLoading(false));
  }, [year]);

  const handleDownload = async (invoice: Invoice): Promise<void> => {
    setDownloadingId(invoice.id);
    setDownloadError(null);
    try {
      await downloadInvoicePdf(invoice);
    } catch (err) {
      setDownloadError(describeError(err, "Échec du téléchargement du PDF."));
    } finally {
      setDownloadingId(null);
    }
  };

  return (
    <Card
      title="Factures"
      subtitle="Factures émises pour des professionnels et avoirs d'annulation, numérotés par année."
    >
      <div className="space-y-4">
        <ErrorNotice message={error} />
        <ErrorNotice message={downloadError} />
        <label className="block">
          <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Année</span>
          <select
            value={year}
            onChange={(e) => setYear(Number(e.target.value))}
            aria-label="Année des factures"
            className={`${selectClass} min-w-[110px] max-w-[160px]`}
          >
            {yearOptions().map((y) => (
              <option key={y} value={y}>
                {y}
              </option>
            ))}
          </select>
        </label>

        {loading ? (
          <p className="text-sm text-fc-ink-soft">Chargement…</p>
        ) : invoices.length === 0 ? (
          <p className="text-sm text-fc-ink-soft">Aucune facture en {year}.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-fc-ink-mute uppercase text-xs tracking-wide">
                  <th className="py-2 pr-4">Numéro</th>
                  <th className="py-2 pr-4">Date</th>
                  <th className="py-2 pr-4">Société</th>
                  <th className="py-2 pr-4">Total TTC</th>
                  <th className="py-2 pr-4" />
                </tr>
              </thead>
              <tbody>
                {invoices.map((inv) => (
                  <tr key={inv.id} className="border-t border-fc-line">
                    <td className="py-2 pr-4 whitespace-nowrap">
                      <span className="font-mono tabular-nums font-semibold">{inv.invoice_number}</span>
                      {inv.kind === "credit_note" && (
                        <span className="ml-2 inline-flex items-center rounded-fc bg-fc-warn-soft px-2 py-0.5 text-xs font-medium text-fc-warn">
                          {invoiceKindLabel(inv.kind)}
                        </span>
                      )}
                    </td>
                    <td className="py-2 pr-4 whitespace-nowrap">{formatDate(inv.issued_at)}</td>
                    <td className="py-2 pr-4">
                      {inv.company_name}
                      <span className="block font-mono text-xs text-fc-ink-mute tabular-nums">{formatSiret(inv.siret)}</span>
                    </td>
                    <td className="py-2 pr-4 font-mono tabular-nums whitespace-nowrap">
                      {formatCurrency(invoiceAmount(inv.total_ttc))}
                    </td>
                    <td className="py-2 pr-4">
                      <Button variant="outline" onClick={() => void handleDownload(inv)} disabled={downloadingId !== null}>
                        {downloadingId === inv.id ? "Préparation…" : "PDF"}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Exports détaillés (F4)
// ---------------------------------------------------------------------------

function RawTableExportsCard() {
  const [table, setTable] = useState<ExportableTable>("transactions");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<DisplayableError>(null);
  const [ok, setOk] = useState<string | null>(null);

  const handleDownload = async (): Promise<void> => {
    setDownloading(true);
    setError(null);
    setOk(null);
    try {
      const qs = new URLSearchParams();
      if (from) qs.set("from", from);
      if (to) qs.set("to", to);
      const query = qs.toString();
      const filename = `${table}${from || to ? `_${from || "debut"}_${to || "fin"}` : ""}.csv`;
      await downloadFile(`/api/admin/exports/table/${table}${query ? `?${query}` : ""}`, filename);
      setOk(`${filename} téléchargé.`);
    } catch (err) {
      setError(describeError(err, "Échec du téléchargement."));
    } finally {
      setDownloading(false);
    }
  };

  return (
    <Card title="Exports détaillés" subtitle="Exporter une table en CSV, sur une période choisie — aucune donnée client.">
      <div className="space-y-4">
        <ErrorNotice message={error} />
        {ok && !error && <p className="text-sm text-fc-primary-deep">{ok}</p>}
        <div className="flex flex-wrap items-end gap-3">
          <label className="block">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Table</span>
            <select value={table} onChange={(e) => setTable(e.target.value as ExportableTable)} className={`${selectClass} min-w-[220px]`}>
              {EXPORTABLE_TABLES.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Du</span>
            <input
              type="date"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
              className="min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
            />
          </label>
          <label className="block">
            <span className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5">Au</span>
            <input
              type="date"
              value={to}
              onChange={(e) => setTo(e.target.value)}
              className="min-h-touch px-4 py-2.5 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
            />
          </label>
          <Button variant="outline" onClick={() => void handleDownload()} disabled={downloading}>
            {downloading ? "Préparation…" : "Télécharger le CSV"}
          </Button>
        </div>
      </div>
    </Card>
  );
}
