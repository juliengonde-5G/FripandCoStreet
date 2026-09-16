"use client";

/**
 * Onglet Clients (PR3, §5 ARCHITECTURE_PR3.md) — déplacé tel quel depuis
 * `app/admin/page.tsx` (PR10, L6), pour que la page d'administration se
 * limite à l'aiguillage entre onglets.
 *
 * Deux colonnes : la recherche à gauche, la fiche sélectionnée à droite
 * (coordonnées, consentements, envois de ticket, tickets liés, données
 * personnelles).
 */
import React, { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import Input from "@/components/ui/Input";
import { api, ApiError } from "@/lib/api";
import { formatClientName, formatCurrency, formatDateTime } from "@/lib/format";
import type { AnonymizeRequest, Client, ClientFull, ConsentUpdateRequest } from "@/lib/types";

// Mêmes deux aides d'affichage que les autres onglets extraits
// (CashiersTab, PaymentsTab) : chacun porte sa copie plutôt qu'une
// dépendance croisée vers la page.
function ErrorNotice({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div role="alert" className="rounded-fc bg-fc-danger-soft border border-fc-danger/30 px-3 py-2 text-sm text-fc-danger">
      {message}
    </div>
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

export default function ClientsTab() {
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
      <Card title="Clients" subtitle="Recherche par e-mail, par nom ou par téléphone.">
        <div className="space-y-3">
          <Input
            label="Rechercher"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") search(query);
            }}
            placeholder="julie@exemple.fr, Dupont ou 06 12 34 56 78"
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
                  {/* PR7 (I3) : une fiche peut n'avoir qu'un téléphone —
                      l'en-tête prend alors le nom, et la ligne de détail
                      dit explicitement « Pas d'e-mail » plutôt que de
                      laisser un vide. */}
                  <div className="text-sm font-medium truncate">
                    {c.anonymized_at
                      ? "Client anonymisé"
                      : c.email || formatClientName(c) || c.phone || "Fiche sans coordonnée"}
                  </div>
                  <div className="text-xs text-fc-ink-mute truncate">
                    {c.anonymized_at ? "—" : c.email ? formatClientName(c) || "—" : "Pas d'e-mail"}
                    {" · "}
                    {c.phone || "Pas de téléphone"}
                  </div>
                  <div className="text-xs text-fc-ink-mute truncate">
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
      <Card
        title={client.anonymized_at ? "Client anonymisé" : client.email || fullName || "Pas d'e-mail"}
        subtitle={client.anonymized_at || !client.email ? undefined : fullName || undefined}
      >
        {client.anonymized_at ? (
          <div className="rounded-fc-lg bg-fc-bg-alt p-4 text-sm text-fc-ink-soft">
            Données supprimées le {formatDateTime(client.anonymized_at)}.
          </div>
        ) : (
          <div className="space-y-4">
            <ErrorNotice message={consentError} />
            <dl className="grid gap-4 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs uppercase tracking-wide text-fc-ink-mute">E-mail</dt>
                <dd className="break-all text-fc-ink">{client.email || "Pas d'e-mail"}</dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-fc-ink-mute">Téléphone</dt>
                <dd className="break-all text-fc-ink">{client.phone || "Pas de téléphone"}</dd>
              </div>
            </dl>
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
