"use client";

/**
 * Onglet Clients (PR3, §5 ARCHITECTURE_PR3.md ; PR10, L6) — déplacé depuis
 * `app/admin/page.tsx`, pour que la page d'administration se limite à
 * l'aiguillage entre onglets.
 *
 * En tête, les fiches qui se ressemblent (« Doublons possibles ») et le
 * geste qui les réunit. En dessous, deux colonnes : la recherche à gauche,
 * la fiche sélectionnée à droite (coordonnées, consentements, envois de
 * ticket, tickets liés avec leurs articles, données personnelles).
 *
 * Vocabulaire tenu du côté du comptoir : on parle de « fiche », de
 * « fusionner » et de « conserver », jamais d'enregistrement ni de
 * déduplication. Une fusion ne touche aucune vente : elle rattache
 * simplement les tickets de la fiche vidée à la fiche conservée.
 */
import React, { useEffect, useMemo, useState } from "react";

import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import Input from "@/components/ui/Input";
import Modal from "@/components/ui/Modal";
import { api, ApiError } from "@/lib/api";
import {
  DELETION_DELAY_DEFAULT_DAYS,
  cancelClientDeletion,
  CLIENT_FILTERS,
  clientFilterParams,
  deletionEffectiveDate,
  downloadNewsletterCsv,
  duplicateReasonLabel,
  fetchClients,
  fetchDuplicateGroups,
  fetchRgpdSettings,
  mergeClients,
  requestClientDeletion,
  type ClientListFilter,
  type DuplicateGroup,
  type DuplicateGroupClient,
} from "@/lib/clients";
import { formatClientName, formatCurrency, formatDate, formatDateTime } from "@/lib/format";
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

// ---------------------------------------------------------------------------
// Doublons possibles et fusion (PR10, L6 — contrat L2/L3)
// ---------------------------------------------------------------------------

/** « J. Vasseur » — initiale du prénom et nom de famille, comme sur les
 * listes de la caisse. Une fiche sans nom se dit par sa coordonnée. */
function shortClientName(client: Pick<Client, "first_name" | "last_name">): string {
  const first = (client.first_name ?? "").trim();
  const last = (client.last_name ?? "").trim();
  if (!first && !last) return "Fiche sans nom";
  if (!last) return first;
  return first ? `${first[0].toUpperCase()}. ${last}` : last;
}

/** « 3 visites · dernière le 12/09/2026 » — les deux chiffres qui aident
 * à choisir la fiche à conserver. */
function visitsSummary(client: Pick<DuplicateGroupClient, "visits_count" | "last_visit_at">): string {
  if (!client.visits_count) return "Jamais venue";
  const visits = client.visits_count === 1 ? "1 visite" : `${client.visits_count} visites`;
  return client.last_visit_at ? `${visits} · dernière le ${formatDate(client.last_visit_at)}` : visits;
}

/** Fiche à conserver par défaut : celle qui a le plus de visites, puis la
 * plus ancienne (elle porte l'historique le plus long). */
function preferredWinner(clients: DuplicateGroupClient[]): DuplicateGroupClient | undefined {
  return [...clients].sort((a, b) => {
    if (b.visits_count !== a.visits_count) return b.visits_count - a.visits_count;
    return (a.created_at ?? "") < (b.created_at ?? "") ? -1 : 1;
  })[0];
}

function DuplicatesCard({ refreshKey, onMerged }: { refreshKey: number; onMerged: (winnerId: string) => void }) {
  const [groups, setGroups] = useState<DuplicateGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openGroup, setOpenGroup] = useState<DuplicateGroup | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchDuplicateGroups()
      .then((data) => {
        if (cancelled) return;
        setGroups(data.groups);
        setError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.detail : "Impossible de chercher les doublons.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  return (
    <Card
      title="Doublons possibles"
      subtitle="Des fiches qui désignent peut-être la même personne. Rien n'est fusionné sans votre accord."
    >
      <div className="space-y-4">
        <ErrorNotice message={error} />
        {loading && <p className="text-sm text-fc-ink-soft">Recherche…</p>}
        {!loading && !error && groups.length === 0 && (
          <p className="text-sm text-fc-ink-soft">Aucun doublon repéré pour le moment.</p>
        )}
        {groups.map((group, index) => (
          <div
            key={`${group.reason}-${group.clients.map((c) => c.id).join("-")}`}
            className="rounded-fc-lg border border-fc-line bg-fc-bg-alt p-4"
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm font-semibold text-fc-ink">
                  {group.clients.length} fiches — {duplicateReasonLabel(group.reason)}
                </p>
                <p className="text-xs text-fc-ink-mute">Groupe {index + 1}</p>
              </div>
              <Button variant="outline" size="sm" onClick={() => setOpenGroup(group)}>
                Fusionner
              </Button>
            </div>
            <ul className="mt-3 space-y-2">
              {group.clients.map((client) => (
                <li key={client.id} className="rounded-fc bg-fc-surface px-3 py-2 text-sm">
                  <div className="font-medium text-fc-ink truncate">{shortClientName(client)}</div>
                  <div className="text-xs text-fc-ink-mute truncate">
                    {client.email_masked ?? "Pas d'e-mail"}
                    {" · "}
                    {client.phone_masked ?? "Pas de téléphone"}
                  </div>
                  <div className="text-xs text-fc-ink-mute">{visitsSummary(client)}</div>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      {openGroup && (
        <MergeModal
          group={openGroup}
          onClose={() => setOpenGroup(null)}
          onMerged={(winnerId) => {
            setOpenGroup(null);
            onMerged(winnerId);
          }}
        />
      )}
    </Card>
  );
}

/**
 * Modale de fusion : on choisit la fiche à conserver, on lit ce qui va
 * bouger, puis on confirme. Les autres fiches du groupe sont rattachées
 * l'une après l'autre à la fiche conservée (le serveur ne fusionne que
 * deux fiches à la fois).
 */
function MergeModal({
  group,
  onClose,
  onMerged,
}: {
  group: DuplicateGroup;
  onClose: () => void;
  onMerged: (winnerId: string) => void;
}) {
  const [winnerId, setWinnerId] = useState<string>(() => preferredWinner(group.clients)?.id ?? group.clients[0].id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sources = useMemo(() => group.clients.filter((c) => c.id !== winnerId), [group.clients, winnerId]);
  const salesToMove = sources.reduce((sum, c) => sum + c.visits_count, 0);
  const winner = group.clients.find((c) => c.id === winnerId);

  const handleMerge = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      for (const source of sources) {
        await mergeClients(winnerId, source.id);
      }
      onMerged(winnerId);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "La fusion a échoué.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onClose={busy ? () => undefined : onClose}
      title="Fusionner des fiches"
      closeOnBackdrop={!busy}
      actions={
        <>
          <Button variant="outline" size="sm" onClick={onClose} disabled={busy}>
            Annuler
          </Button>
          <Button variant="primary" size="sm" onClick={() => void handleMerge()} disabled={busy}>
            {busy ? "Fusion…" : "Fusionner"}
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        <ErrorNotice message={error} />

        <fieldset className="space-y-2">
          <legend className="mb-2 text-sm font-medium text-fc-ink">Quelle fiche conserver ?</legend>
          {group.clients.map((client) => (
            <label
              key={client.id}
              className={`flex min-h-touch cursor-pointer items-start gap-3 rounded-fc border px-3 py-2.5 transition-colors ${
                client.id === winnerId
                  ? "border-fc-primary bg-fc-primary-soft"
                  : "border-fc-line bg-fc-surface hover:bg-fc-bg"
              }`}
            >
              <input
                type="radio"
                name="merge-winner"
                className="mt-1"
                checked={client.id === winnerId}
                onChange={() => setWinnerId(client.id)}
                disabled={busy}
              />
              <span className="min-w-0 text-sm">
                <span className="block font-medium text-fc-ink truncate">{shortClientName(client)}</span>
                <span className="block text-xs text-fc-ink-mute truncate">
                  {client.email_masked ?? "Pas d'e-mail"}
                  {" · "}
                  {client.phone_masked ?? "Pas de téléphone"}
                </span>
                <span className="block text-xs text-fc-ink-mute">
                  {visitsSummary(client)} · fiche créée le {formatDate(client.created_at)}
                </span>
              </span>
            </label>
          ))}
        </fieldset>

        <div className="rounded-fc-lg bg-fc-bg-alt p-4 text-sm text-fc-ink-soft">
          <p className="font-medium text-fc-ink">
            {salesToMove === 0
              ? "Les consentements et les messages"
              : `${salesToMove === 1 ? "1 vente" : `${salesToMove} ventes`}, les consentements et les messages`}
            {" seront rattachés à la fiche conservée"}
            {winner ? ` (${shortClientName(winner)})` : ""}.
          </p>
          <p className="mt-1">
            {sources.length > 1
              ? "Les autres fiches seront vidées de leurs coordonnées et ne s'afficheront plus dans la recherche."
              : "L'autre fiche sera vidée de ses coordonnées et ne s'affichera plus dans la recherche."}
            {" Aucun ticket n'est modifié ni supprimé."}
          </p>
        </div>
      </div>
    </Modal>
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
  // PR11 (M4) : la liste se restreint aux abonnées à la newsletter ou aux
  // fiches dont la suppression est programmée. Les deux puces s'ajoutent à
  // la recherche libre, elles ne la remplacent pas.
  const [filter, setFilter] = useState<ClientListFilter>("all");
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Recompté après chaque fusion : la carte des doublons se relit sans
  // que la page entière ne se remonte.
  const [duplicatesKey, setDuplicatesKey] = useState(0);
  // Bandeau affiché quand on arrive sur une fiche absorbée : on ouvre la
  // fiche conservée à sa place, en le disant.
  const [mergedNotice, setMergedNotice] = useState<string | null>(null);
  // Délai de suppression RGPD (réglable de 1 à 90 jours) : lu une fois à
  // l'ouverture de l'onglet, jamais écrit en dur dans les libellés.
  const [deletionDelayDays, setDeletionDelayDays] = useState(DELETION_DELAY_DEFAULT_DAYS);

  useEffect(() => {
    let cancelled = false;
    fetchRgpdSettings().then((rgpd) => {
      if (!cancelled) setDeletionDelayDays(rgpd.deletion_delay_days);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const search = (q: string, listFilter: ClientListFilter = filter) => {
    setLoadingList(true);
    setListError(null);
    fetchClients({ q, ...clientFilterParams(listFilter) })
      .then((clients) => setList(clients))
      .catch((err) => setListError(err instanceof ApiError ? err.detail : "Impossible de charger les clients."))
      .finally(() => setLoadingList(false));
  };

  useEffect(() => search(""), []); // eslint-disable-line react-hooks/exhaustive-deps

  /** Changement de puce : la liste se relit tout de suite, sans repasser
   * par le bouton « Rechercher » — le filtre est un geste, pas une saisie. */
  const changeFilter = (next: ClientListFilter): void => {
    setFilter(next);
    search(query, next);
  };

  /** Export des abonnés. Le fichier part vers l'outil d'e-mailing déclaré
   * de la boutique, et nulle part ailleurs (voir docs/MANUEL_MANAGER.md). */
  const exportSubscribers = async (): Promise<void> => {
    setExporting(true);
    setExportError(null);
    try {
      await downloadNewsletterCsv();
    } catch (err) {
      setExportError(err instanceof ApiError ? err.detail : "Impossible de préparer l'export.");
    } finally {
      setExporting(false);
    }
  };

  /** Après une fusion : la liste, la fiche ouverte et les doublons
   * repartent de la fiche conservée. */
  const handleMerged = (winnerId: string): void => {
    search(query);
    setSelectedId(winnerId);
    setDuplicatesKey((k) => k + 1);
    setMergedNotice(null);
  };

  /** Fiche absorbée ouverte par erreur : on bascule sur la conservée. */
  const handleRedirect = (winnerId: string): void => {
    setSelectedId(winnerId);
    setMergedNotice("Cette fiche a été fusionnée : voici la fiche conservée.");
  };

  return (
    <div className="space-y-6">
      <DuplicatesCard refreshKey={duplicatesKey} onMerged={handleMerged} />

      {/* `min-w-0` sur les deux colonnes : sans cela, un élément de grille
          garde sa largeur minimale automatique, les `truncate` ci-dessous
          ne s'appliquent pas et une adresse longue pousse la page en
          largeur (défilement horizontal sur téléphone). */}
      <div className="grid items-start gap-6 lg:grid-cols-[340px_1fr]">
        <Card
          className="min-w-0"
          title="Clients"
          subtitle="Recherche par e-mail, par nom ou par téléphone."
        >
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

          {/* PR11 (M4) — puces de filtre, compteur et export des abonnés. */}
          <div className="flex flex-wrap gap-2" role="group" aria-label="Filtrer les fiches">
            {CLIENT_FILTERS.map((chip) => (
              <button
                key={chip.value}
                type="button"
                onClick={() => changeFilter(chip.value)}
                aria-pressed={filter === chip.value}
                className={`min-h-touch rounded-fc border px-3 py-2 text-xs font-medium transition-colors ${
                  filter === chip.value
                    ? "border-fc-primary bg-fc-primary-soft text-fc-primary-deep"
                    : "border-fc-line bg-fc-surface text-fc-ink-soft hover:bg-fc-bg-alt"
                }`}
              >
                {chip.label}
              </button>
            ))}
          </div>

          <p className="text-xs text-fc-ink-mute" aria-live="polite">
            {loadingList
              ? "Recherche…"
              : `${list.length} ${list.length > 1 ? "fiches" : "fiche"}${
                  filter === "newsletter"
                    ? " abonnées à la newsletter"
                    : filter === "deletion"
                      ? " dont la suppression est programmée"
                      : ""
                }`}
          </p>

          <Button variant="outline" size="sm" onClick={() => void exportSubscribers()} disabled={exporting}>
            {exporting ? "Préparation…" : "Exporter les abonnés (CSV)"}
          </Button>
          <p className="text-xs text-fc-ink-mute">
            Le fichier ne contient que les fiches abonnées et actives. Il ne sert qu&apos;à alimenter
            l&apos;outil d&apos;e-mailing déclaré de la boutique.
          </p>
          <ErrorNotice message={exportError} />

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

        <div className="min-w-0 space-y-4">
        {mergedNotice && (
          <div
            role="status"
            className="flex items-start justify-between gap-3 rounded-fc-lg border border-fc-primary/30 bg-fc-primary-soft px-4 py-3 text-sm text-fc-primary-deep"
          >
            <span className="min-w-0">{mergedNotice}</span>
            <button
              type="button"
              onClick={() => setMergedNotice(null)}
              className="min-h-touch shrink-0 rounded-fc px-2 text-xs font-medium underline"
            >
              Fermer
            </button>
          </div>
        )}

        {selectedId ? (
          <ClientDetailCard
            key={selectedId}
            clientId={selectedId}
            deletionDelayDays={deletionDelayDays}
            onChanged={() => {
              search(query);
              setDuplicatesKey((k) => k + 1);
            }}
            onRedirect={handleRedirect}
          />
        ) : (
          <Card title="Fiche client" subtitle="Sélectionnez un client dans la liste pour voir le détail.">
            <p className="text-sm text-fc-ink-soft">Aucun client sélectionné.</p>
          </Card>
        )}
        </div>
      </div>
    </div>
  );
}

function ClientDetailCard({
  clientId,
  deletionDelayDays,
  onChanged,
  onRedirect,
}: {
  clientId: string;
  deletionDelayDays: number;
  onChanged: () => void;
  onRedirect: (winnerId: string) => void;
}) {
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

  // Fiche absorbée par une fusion : elle n'a plus de coordonnées et ne
  // sert plus à rien. On ouvre la fiche conservée à sa place (L6).
  const mergedInto = data?.client.merged_into_client_id ?? null;
  useEffect(() => {
    if (mergedInto && mergedInto !== clientId) onRedirect(mergedInto);
  }, [mergedInto, clientId, onRedirect]);

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
            {client.deletion_scheduled_for && (
              <p className="inline-flex items-center gap-2 rounded-fc bg-fc-warn-soft px-3 py-1.5 text-sm font-medium text-fc-warn">
                <span className="h-1.5 w-1.5 rounded-full bg-fc-warn" aria-hidden />
                Suppression programmée le {formatDate(client.deletion_scheduled_for)}
              </p>
            )}
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
              <ul className="divide-y divide-fc-line">
                {transactions.map((t) => (
                  <li key={t.id} className="py-2.5 first:pt-0 last:pb-0">
                    <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
                      <span className="font-medium text-fc-ink">
                        N° {t.transaction_number} · {formatDateTime(t.created_at)}
                      </span>
                      <span className="flex items-center gap-2">
                        {t.refunded && (
                          <span className="rounded-fc bg-fc-danger-soft px-2 py-0.5 text-xs font-medium text-fc-danger">
                            annulé
                          </span>
                        )}
                        <span className="font-mono tabular-nums text-fc-ink">{formatCurrency(t.total_ttc)}</span>
                      </span>
                    </div>
                    {/* PR10 (L4) : les articles du ticket, pour reconnaître
                        un achat sans rouvrir la vente. */}
                    {t.items && t.items.length > 0 && (
                      <p className="mt-0.5 text-xs text-fc-ink-mute">
                        {t.items
                          .map((item) => (item.label === "…" ? "…" : `${item.quantity} × ${item.label}`))
                          .join(" · ")}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <RgpdCard
            client={client}
            deletionDelayDays={deletionDelayDays}
            onChanged={() => {
              load();
              onChanged();
            }}
          />
        </>
      )}
    </div>
  );
}

/**
 * Données personnelles d'une fiche (PR3 §5, PR10 L5/L6).
 *
 * Deux chemins, dans cet ordre : la suppression **programmée** — la fiche
 * reste utilisable, ses coordonnées s'effacent toutes seules à la date
 * d'effet, et on peut revenir en arrière — puis, en action secondaire, la
 * suppression **immédiate**, qui demande un motif et la saisie du mot
 * SUPPRIMER parce qu'elle ne se rattrape pas.
 *
 * Le délai est un réglage (1 à 90 jours) : il est annoncé dans le libellé
 * du bouton et la date d'effet est calculée avant de confirmer. Une fois
 * la demande enregistrée, c'est la date renvoyée par le serveur qui
 * s'affiche, jamais celle qu'on avait calculée.
 */
function RgpdCard({
  client,
  deletionDelayDays,
  onChanged,
}: {
  client: Client;
  deletionDelayDays: number;
  onChanged: () => void;
}) {
  const [exportJson, setExportJson] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const [scheduleBusy, setScheduleBusy] = useState(false);
  const [scheduleError, setScheduleError] = useState<string | null>(null);
  const [scheduleConfirm, setScheduleConfirm] = useState(false);

  const [immediateOpen, setImmediateOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [confirmWord, setConfirmWord] = useState("");
  const [anonymizing, setAnonymizing] = useState(false);
  const [anonymizeError, setAnonymizeError] = useState<string | null>(null);

  const clientId = client.id;
  const scheduledFor = client.deletion_scheduled_for ?? null;
  const delayLabel = deletionDelayDays === 1 ? "1 jour" : `${deletionDelayDays} jours`;
  // Date annoncée AVANT la demande ; une fois enregistrée, c'est la date
  // renvoyée par le serveur (badge + rappel ci-dessous) qui s'affiche.
  const effectiveDate = useMemo(
    () => deletionEffectiveDate(deletionDelayDays),
    [deletionDelayDays],
  );

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

  const handleSchedule = async (): Promise<void> => {
    setScheduleBusy(true);
    setScheduleError(null);
    try {
      await requestClientDeletion(clientId);
      setScheduleConfirm(false);
      onChanged();
    } catch (err) {
      setScheduleError(err instanceof ApiError ? err.detail : "Impossible de programmer la suppression.");
    } finally {
      setScheduleBusy(false);
    }
  };

  const handleCancelSchedule = async (): Promise<void> => {
    setScheduleBusy(true);
    setScheduleError(null);
    try {
      await cancelClientDeletion(clientId);
      onChanged();
    } catch (err) {
      setScheduleError(err instanceof ApiError ? err.detail : "Impossible d'annuler la suppression programmée.");
    } finally {
      setScheduleBusy(false);
    }
  };

  const canAnonymize = reason.trim().length >= 3 && confirmWord.trim().toUpperCase() === "SUPPRIMER";

  const handleAnonymize = async (): Promise<void> => {
    if (!canAnonymize) return;
    setAnonymizing(true);
    setAnonymizeError(null);
    try {
      const body: AnonymizeRequest = { reason: reason.trim() };
      await api.post(`/api/admin/clients/${clientId}/anonymize`, body);
      setImmediateOpen(false);
      setReason("");
      setConfirmWord("");
      onChanged();
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
          <ErrorNotice message={scheduleError} />
          {scheduledFor ? (
            <>
              <p className="text-sm text-fc-ink">
                Suppression programmée le <strong>{formatDate(scheduledFor)}</strong>
                {client.deletion_requested_at ? ` (demandée le ${formatDate(client.deletion_requested_at)})` : ""}.
              </p>
              <p className="text-sm text-fc-ink-soft">
                La fiche reste utilisable en caisse jusqu&apos;à cette date. Les coordonnées s&apos;effaceront ensuite
                toutes seules ; les tickets, eux, sont conservés.
              </p>
              <Button variant="outline" size="sm" onClick={() => void handleCancelSchedule()} disabled={scheduleBusy}>
                {scheduleBusy ? "Annulation…" : "Annuler la suppression programmée"}
              </Button>
            </>
          ) : (
            <>
              <p className="text-sm text-fc-ink-soft">
                La personne demande la suppression de ses données ? On la programme à {delayLabel} : elle reste
                cliente d&apos;ici là, et vous pouvez revenir en arrière si elle change d&apos;avis.
              </p>
              {!scheduleConfirm ? (
                <Button variant="primary" size="sm" onClick={() => setScheduleConfirm(true)}>
                  Programmer la suppression ({delayLabel})
                </Button>
              ) : (
                <div className="space-y-3 rounded-fc-lg border border-fc-line bg-fc-bg-alt p-4">
                  <p className="text-sm text-fc-ink">
                    Les coordonnées de cette fiche seront effacées le <strong>{formatDate(effectiveDate)}</strong>.
                    Vous pourrez annuler jusque-là.
                  </p>
                  <div className="flex flex-wrap gap-3">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setScheduleConfirm(false)}
                      disabled={scheduleBusy}
                    >
                      Annuler
                    </Button>
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => void handleSchedule()}
                      disabled={scheduleBusy}
                    >
                      {scheduleBusy ? "Enregistrement…" : "Confirmer la programmation"}
                    </Button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        <div className="space-y-3 border-t border-fc-line pt-4">
          <ErrorNotice message={anonymizeError} />
          {!immediateOpen ? (
            <Button variant="ghost" size="sm" onClick={() => setImmediateOpen(true)}>
              Supprimer immédiatement
            </Button>
          ) : (
            <div className="space-y-3 rounded-fc-lg border border-fc-danger/40 bg-fc-danger-soft p-4">
              <p className="text-sm font-semibold text-fc-danger">
                Cette action est irréversible : les coordonnées seront effacées, les tickets conservés anonymisés.
              </p>
              <label className="block">
                <span className="mb-1.5 block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft">
                  Motif de la suppression
                </span>
                <textarea
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  rows={2}
                  placeholder="Ex. demande écrite du client du 12/09"
                  className="w-full rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-sm text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
                />
              </label>
              <Input
                label="Tapez SUPPRIMER pour confirmer"
                value={confirmWord}
                onChange={(e) => setConfirmWord(e.target.value)}
                placeholder="SUPPRIMER"
                autoComplete="off"
              />
              <div className="flex flex-wrap gap-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setImmediateOpen(false);
                    setConfirmWord("");
                  }}
                  disabled={anonymizing}
                >
                  Annuler
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => void handleAnonymize()}
                  disabled={anonymizing || !canAnonymize}
                >
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
