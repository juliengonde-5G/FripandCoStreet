"use client";

/**
 * Écran « Choisir la cliente » (PR7, docs/ARCHITECTURE_PR7.md §1, I3).
 *
 * Plein écran, jamais une petite modale : au comptoir, la vendeuse tape un
 * nom ou les derniers chiffres d'un numéro sur une tablette, avec la
 * cliente en face. Même barre haute sombre que la caisse
 * (`components/pos/PosTopBar.tsx`) pour qu'on sache toujours où l'on est,
 * et un bouton « Retour » qui ramène au ticket sans rien changer.
 *
 * Deux gestes seulement :
 *   1. chercher (nom, e-mail ou téléphone, à partir de 2 caractères) puis
 *      toucher la bonne carte ;
 *   2. ou toucher « Nouveau client » — visible en permanence, y compris
 *      quand la recherche ne donne rien — et remplir la fiche.
 *
 * Aucune fidélité, aucun point, aucun historique d'achat détaillé : une
 * carte porte le nom, les coordonnées MASQUÉES (`email_masked`,
 * `phone_masked` — la vendeuse identifie sans lire l'adresse complète
 * devant la file d'attente), le nombre de visites et la date de la
 * dernière.
 *
 * PR10 (L7) : deux ajouts, tous deux au service du même réflexe — ne pas
 * créer une deuxième fiche à quelqu'un qui en a déjà une, et savoir si la
 * personne en face est une habituée. (a) pendant la saisie d'un nouveau
 * client, un encart « Une fiche existe peut-être déjà » propose jusqu'à
 * trois fiches proches ; (b) chaque carte de résultat porte un bouton
 * « Historique » qui ouvre `ClientHistoryPanel`.
 *
 * Accessibilité : `role="dialog"`, focus piégé et restauré
 * (`useDialogA11y`), Échap = Retour — sauf quand le panneau Historique est
 * ouvert par-dessus : c'est alors lui qui prend Échap, et l'écran de
 * sélection reste en place. L'arrière-plan est rendu inert par la page de
 * caisse, comme pour les autres écrans plein écran.
 */
import React, { useCallback, useEffect, useId, useRef, useState } from "react";

import ClientHistoryPanel from "@/components/pos/ClientHistoryPanel";
import { api, ApiError } from "@/lib/api";
import { duplicateReasonLabel, fetchPosDuplicates, type DuplicateCandidate } from "@/lib/clients";
import { formatClientName, formatRelativeTime, isValidEmail } from "@/lib/format";
import { useDialogA11y } from "@/lib/useDialogA11y";
import type { CreatePosClientResponse, PosClient, PosClientSearchResponse } from "@/lib/types";

/** Fiche dont on veut voir l'historique : l'identifiant, et le nom déjà
 * calculé par la carte qui a ouvert le panneau (PR10, L7). Type strictement
 * caisse, il ne sort pas de cet écran ni de la page de caisse. */
export interface ClientHistoryTarget {
  id: string;
  name: string;
}

interface Props {
  open: boolean;
  /** Retour au ticket, sans rien sélectionner. */
  onClose: () => void;
  /** Fiche choisie (sélection d'une carte ou création). */
  onSelect: (client: PosClient) => void;
  /** `shop.dpo_email` — mention RGPD du formulaire de création, même
   * source que l'écran de fin de vente (E8 de PR3). */
  dpoEmail?: string;
}

/** Délai avant de lancer la recherche : assez court pour suivre la frappe,
 * assez long pour ne pas envoyer une requête par lettre. */
const SEARCH_DEBOUNCE_MS = 250;
const MIN_QUERY_LENGTH = 2;

export default function ClientSelectionScreen({ open, onClose, onSelect, dpoEmail }: Props) {
  const titleId = useId();
  /** Fiche dont l'historique est ouvert par-dessus cet écran (PR10, L7). */
  const [historyTarget, setHistoryTarget] = useState<ClientHistoryTarget | null>(null);
  // Échap ferme le panneau du dessus, pas les deux d'un coup : les deux
  // écouteurs vivent sur `document`, celui du parent se déclenche donc
  // même quand l'enfant a « arrêté » l'événement. On le neutralise ici
  // tant que le panneau Historique est ouvert.
  const handleEscape = useCallback(() => {
    if (historyTarget) return;
    onClose();
  }, [historyTarget, onClose]);
  const containerRef = useDialogA11y<HTMLDivElement>(open, handleEscape);
  const searchRef = useRef<HTMLInputElement | null>(null);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<PosClient[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  /** Vrai dès qu'une recherche a abouti : évite d'annoncer « aucune fiche »
   * avant même d'avoir interrogé le serveur. */
  const [searched, setSearched] = useState(false);
  const [creating, setCreating] = useState(false);

  // Remise à zéro à chaque ouverture : on ne rouvre jamais l'écran sur la
  // recherche de la cliente précédente.
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setResults([]);
    setSearching(false);
    setSearchError(null);
    setSearched(false);
    setCreating(false);
    setHistoryTarget(null);
  }, [open]);

  // Le champ de recherche prend le focus à l'ouverture — `useDialogA11y`
  // pose d'abord le focus sur le premier élément focusable (le bouton
  // « Retour ») dans une micro-tâche ; ce `setTimeout` s'exécute après,
  // donc c'est bien la recherche qui gagne, sans course.
  useEffect(() => {
    if (!open || creating) return;
    const timer = setTimeout(() => searchRef.current?.focus(), 0);
    return () => clearTimeout(timer);
  }, [open, creating]);

  // Recherche débattue. Chaque frappe annule la requête précédente
  // (`AbortController`) : une réponse lente ne peut pas écraser le
  // résultat d'une frappe plus récente.
  useEffect(() => {
    if (!open) return;
    const trimmed = query.trim();
    if (trimmed.length < MIN_QUERY_LENGTH) {
      setResults([]);
      setSearching(false);
      setSearchError(null);
      setSearched(false);
      return;
    }
    const controller = new AbortController();
    setSearching(true);
    const timer = setTimeout(() => {
      api
        .get<PosClientSearchResponse>(`/api/pos/clients/search?q=${encodeURIComponent(trimmed)}`)
        .then((data) => {
          if (controller.signal.aborted) return;
          setResults(data.clients ?? []);
          setSearchError(null);
          setSearched(true);
        })
        .catch((err) => {
          if (controller.signal.aborted) return;
          setResults([]);
          setSearched(true);
          setSearchError(err instanceof ApiError ? err.detail : "Recherche impossible.");
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false);
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [open, query]);

  if (!open) return null;

  return (
    <div
      ref={containerRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-[60] flex flex-col bg-fc-bg"
    >
      <header className="flex h-14 flex-shrink-0 items-center gap-3 bg-fc-primary px-2 text-white sm:px-3">
        <button
          type="button"
          onClick={onClose}
          className="inline-flex min-h-touch flex-shrink-0 items-center rounded-fc px-2 text-sm font-semibold text-white transition-colors hover:bg-white/15"
        >
          <span aria-hidden className="mr-1.5 text-base leading-none">
            ←
          </span>
          Retour
        </button>
        <h2 id={titleId} className="min-w-0 flex-1 truncate text-center text-sm font-semibold">
          {creating ? "Nouveau client" : "Choisir la cliente"}
        </h2>
        {/* Contrepoids du bouton « Retour » : garde le titre centré. */}
        <span aria-hidden className="w-20 flex-shrink-0" />
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mx-auto w-full max-w-3xl space-y-4">
          {creating ? (
            <NewClientForm
              dpoEmail={dpoEmail}
              initialQuery={query}
              onCancel={() => setCreating(false)}
              onCreated={onSelect}
              onUseExisting={onSelect}
            />
          ) : (
            <>
              <div className="flex flex-col gap-3 sm:flex-row">
                <input
                  ref={searchRef}
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Nom, e-mail, téléphone"
                  aria-label="Rechercher une cliente"
                  className="min-h-touch w-full flex-1 rounded-fc border border-fc-line bg-fc-surface px-4 py-3 text-base text-fc-ink placeholder-fc-ink-mute focus:border-fc-primary focus:outline-none focus:ring-2 focus:ring-fc-primary"
                />
                <button
                  type="button"
                  onClick={() => setCreating(true)}
                  className="min-h-touch flex-shrink-0 rounded-fc-lg bg-fc-primary px-4 py-3 text-base font-semibold text-white transition-colors hover:bg-fc-primary-deep"
                >
                  Nouveau client
                </button>
              </div>

              {searchError && (
                <div role="alert" className="rounded-fc-lg border border-fc-danger/30 bg-fc-danger-soft p-3 text-sm text-fc-danger">
                  {searchError}
                </div>
              )}

              {query.trim().length < MIN_QUERY_LENGTH ? (
                <p className="py-6 text-center text-sm text-fc-ink-soft">
                  Tapez au moins {MIN_QUERY_LENGTH} caractères — un nom, une adresse e-mail ou les chiffres d&apos;un
                  téléphone.
                </p>
              ) : searching ? (
                <p className="py-6 text-center text-sm text-fc-ink-soft">Recherche…</p>
              ) : results.length === 0 && searched && !searchError ? (
                <div className="rounded-fc-lg border border-fc-line bg-fc-surface p-6 text-center">
                  <p className="text-sm font-medium text-fc-ink">Aucune fiche pour « {query.trim()} ».</p>
                  <p className="mt-1 text-sm text-fc-ink-soft">
                    Créez-en une avec <span className="font-medium">Nouveau client</span>, ou continuez la vente sans
                    cliente.
                  </p>
                </div>
              ) : (
                <ul className="grid gap-2 sm:grid-cols-2">
                  {results.map((client) => (
                    <li key={client.id}>
                      <ClientCard
                        client={client}
                        onSelect={() => onSelect(client)}
                        onHistory={(target) => setHistoryTarget(target)}
                      />
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      </div>

      <ClientHistoryPanel
        open={historyTarget !== null}
        clientId={historyTarget?.id ?? null}
        clientName={historyTarget?.name}
        onClose={() => setHistoryTarget(null)}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Carte d'un résultat de recherche
// ---------------------------------------------------------------------------

function ClientCard({
  client,
  onSelect,
  onHistory,
}: {
  client: PosClient;
  onSelect: () => void;
  onHistory: (target: ClientHistoryTarget) => void;
}) {
  const name = formatClientName(client);
  // Sans nom, la fiche s'annonce par sa coordonnée masquée plutôt que par
  // un vide : elle reste identifiable et sélectionnable.
  const heading = name || client.email_masked || client.phone_masked || "Fiche sans nom";
  const initial = (name || client.email_masked || "?").trim().charAt(0).toUpperCase();

  return (
    // Deux gestes distincts sur une même carte : choisir la fiche (la
    // quasi-totalité de la surface) et regarder son historique (PR10, L7).
    // D'où un conteneur plutôt qu'un seul grand bouton — on n'imbrique pas
    // un bouton dans un bouton.
    <div className="flex items-stretch gap-1 rounded-fc-lg border border-fc-line bg-fc-surface p-1 transition-colors focus-within:border-fc-primary hover:border-fc-primary">
      <button
        type="button"
        onClick={onSelect}
        className="flex min-w-0 flex-1 items-center gap-3 rounded-fc p-2 text-left transition-colors hover:bg-fc-primary-soft"
      >
        <span
          aria-hidden
          className="flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-full bg-fc-primary text-lg font-semibold text-white"
        >
          {initial}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-semibold text-fc-ink">{heading}</span>
          <span className="block truncate text-xs text-fc-ink-soft">
            {[client.email_masked, client.phone_masked].filter(Boolean).join(" · ") || "Aucune coordonnée"}
          </span>
          <span className="mt-0.5 block truncate text-xs text-fc-ink-mute">
            {client.visits_count > 0
              ? `${client.visits_count} visite${client.visits_count > 1 ? "s" : ""}`
              : "Aucune visite"}
            {client.last_visit_at ? ` · Dernière visite ${formatRelativeTime(client.last_visit_at)}` : ""}
          </span>
        </span>
      </button>
      <button
        type="button"
        onClick={() => onHistory({ id: client.id, name: heading })}
        className="min-h-touch min-w-touch flex-shrink-0 rounded-fc border border-fc-line px-2 text-xs font-semibold text-fc-primary-deep transition-colors hover:bg-fc-primary-soft"
      >
        Historique
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Formulaire « Nouveau client »
// ---------------------------------------------------------------------------

/** La recherche en cours sert d'amorce au formulaire : ce qui a été tapé
 * ressemble à une adresse, à un numéro ou à un nom — autant le reprendre
 * plutôt que de faire retaper. */
function seedFromQuery(raw: string): { email: string; phone: string; firstName: string } {
  const value = raw.trim();
  if (!value) return { email: "", phone: "", firstName: "" };
  if (value.includes("@")) return { email: value, phone: "", firstName: "" };
  if (/^[\d+\s.\-/()]+$/.test(value)) return { email: "", phone: value, firstName: "" };
  return { email: "", phone: "", firstName: value };
}

/** Délai avant d'interroger le serveur sur les doublons (L7) : le temps
 * de finir de taper un nom, sans attendre pour autant la fin de la
 * saisie complète. */
const DUPLICATE_DEBOUNCE_MS = 400;
/** Nombre de fiches proposées dans l'encart. Le serveur en renvoie
 * jusqu'à cinq ; trois suffisent au comptoir, au-delà on ne lit plus. */
const MAX_DUPLICATES_SHOWN = 3;

/** Une fiche candidate reprise telle quelle comme cliente du ticket. Le
 * consentement newsletter n'est pas transmis par la route des doublons et
 * n'est lu nulle part en caisse (il se pose à l'envoi du ticket, écran de
 * fin de vente) : on ne l'invente pas, on le laisse à `false`. */
function candidateAsPosClient(candidate: DuplicateCandidate): PosClient {
  return {
    id: candidate.id,
    first_name: candidate.first_name,
    last_name: candidate.last_name,
    email_masked: candidate.email_masked,
    phone_masked: candidate.phone_masked,
    newsletter_optin: false,
    last_visit_at: candidate.last_visit_at,
    visits_count: candidate.visits_count,
  };
}

function NewClientForm({
  dpoEmail,
  initialQuery,
  onCancel,
  onCreated,
  onUseExisting,
}: {
  dpoEmail?: string;
  initialQuery: string;
  onCancel: () => void;
  onCreated: (client: PosClient) => void;
  /** Fiche existante retenue depuis l'encart « Une fiche existe
   * peut-être déjà » : on la sélectionne, on ne crée rien. */
  onUseExisting: (client: PosClient) => void;
}) {
  const seed = seedFromQuery(initialQuery);
  const [firstName, setFirstName] = useState(seed.firstName);
  const [lastName, setLastName] = useState("");
  const [email, setEmail] = useState(seed.email);
  const [phone, setPhone] = useState(seed.phone);
  const [newsletter, setNewsletter] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** Fiche retrouvée au lieu d'être créée (`created: false`) : on le dit
   * avant de repartir, sinon la vendeuse croit avoir créé un doublon. */
  const [reusedName, setReusedName] = useState<string | null>(null);

  // PR10 (L7) — fiches proches de ce qui est en train d'être saisi.
  const [duplicates, setDuplicates] = useState<DuplicateCandidate[]>([]);
  /** « Créer quand même » : l'encart se tait jusqu'à la prochaine
   * modification de la saisie. */
  const [duplicatesDismissed, setDuplicatesDismissed] = useState(false);

  const emailFilled = email.trim().length > 0;
  const phoneFilled = phone.trim().length > 0;
  const emailValid = !emailFilled || isValidEmail(email);
  const canSubmit = (emailFilled || phoneFilled) && emailValid && !submitting;

  const firstFieldRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    const timer = setTimeout(() => firstFieldRef.current?.focus(), 0);
    return () => clearTimeout(timer);
  }, []);

  // Recherche de doublons (L7). On n'interroge le serveur que sur des
  // critères qui veulent dire quelque chose : un prénom ET un nom d'au
  // moins deux lettres, une adresse e-mail valide, ou un numéro complet.
  // Un critère à moitié tapé ne prouve rien et ferait clignoter l'encart.
  // Chaque frappe annule la requête précédente (`AbortController`).
  useEffect(() => {
    const trimmedFirst = firstName.trim();
    const trimmedLast = lastName.trim();
    const trimmedEmail = email.trim();
    const trimmedPhone = phone.trim();
    const criteria: { first_name?: string; last_name?: string; email?: string; phone?: string } = {};
    if (trimmedFirst.length >= 2 && trimmedLast.length >= 2) {
      criteria.first_name = trimmedFirst;
      criteria.last_name = trimmedLast;
    }
    if (isValidEmail(trimmedEmail)) criteria.email = trimmedEmail;
    if (trimmedPhone.replace(/\D/g, "").length >= 10) criteria.phone = trimmedPhone;

    setDuplicatesDismissed(false);
    if (Object.keys(criteria).length === 0) {
      setDuplicates([]);
      return;
    }

    const controller = new AbortController();
    const timer = setTimeout(() => {
      fetchPosDuplicates(criteria, { signal: controller.signal })
        .then((found) => {
          if (controller.signal.aborted) return;
          setDuplicates(found);
        })
        .catch(() => {
          // Un doublon non détecté n'empêche pas d'enregistrer la fiche :
          // l'encart est une aide, jamais un obstacle. On se tait.
          if (!controller.signal.aborted) setDuplicates([]);
        });
    }, DUPLICATE_DEBOUNCE_MS);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [firstName, lastName, email, phone]);

  const shownDuplicates = duplicates.slice(0, MAX_DUPLICATES_SHOWN);
  const duplicatesVisible = !duplicatesDismissed && !reusedName && shownDuplicates.length > 0;

  const handleSubmit = async (): Promise<void> => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const data = await api.post<CreatePosClientResponse>("/api/pos/clients", {
        first_name: firstName.trim() || undefined,
        last_name: lastName.trim() || undefined,
        email: email.trim() || undefined,
        phone: phone.trim() || undefined,
        newsletter_optin: newsletter,
      });
      if (data.created) {
        onCreated(data.client);
        return;
      }
      // Fiche existante : on l'annonce, puis la vendeuse confirme — elle
      // voit ainsi qu'il s'agit bien de la même personne.
      setReusedName(formatClientName(data.client) || data.client.email_masked || data.client.phone_masked || "");
      setSubmitting(false);
      setTimeout(() => onCreated(data.client), 900);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Impossible d'enregistrer cette fiche.");
      setSubmitting(false);
    }
  };

  return (
    <form
      className="space-y-4 rounded-fc-lg border border-fc-line bg-fc-surface p-4"
      onSubmit={(e) => {
        e.preventDefault();
        void handleSubmit();
      }}
    >
      {error && (
        <div role="alert" className="rounded-fc-lg border border-fc-danger/30 bg-fc-danger-soft p-3 text-sm text-fc-danger">
          {error}
        </div>
      )}
      {reusedName && (
        <div role="status" className="rounded-fc-lg bg-fc-primary-soft p-3 text-sm font-medium text-fc-primary-deep">
          Fiche existante reprise{reusedName ? ` — ${reusedName}` : ""}.
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-fc-ink-soft">Prénom</span>
          <input
            ref={firstFieldRef}
            type="text"
            value={firstName}
            onChange={(e) => setFirstName(e.target.value)}
            autoComplete="given-name"
            className="min-h-touch w-full rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-fc-ink placeholder-fc-ink-mute focus:border-fc-primary focus:outline-none focus:ring-2 focus:ring-fc-primary"
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-fc-ink-soft">Nom</span>
          <input
            type="text"
            value={lastName}
            onChange={(e) => setLastName(e.target.value)}
            autoComplete="family-name"
            className="min-h-touch w-full rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-fc-ink placeholder-fc-ink-mute focus:border-fc-primary focus:outline-none focus:ring-2 focus:ring-fc-primary"
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-fc-ink-soft">E-mail</span>
          <input
            type="email"
            inputMode="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="adresse@exemple.fr"
            aria-invalid={emailFilled && !emailValid}
            className="min-h-touch w-full rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-fc-ink placeholder-fc-ink-mute focus:border-fc-primary focus:outline-none focus:ring-2 focus:ring-fc-primary"
          />
          {emailFilled && !emailValid && <span className="mt-1 block text-xs text-fc-danger">Adresse e-mail incomplète</span>}
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-fc-ink-soft">Téléphone</span>
          <input
            type="tel"
            inputMode="tel"
            autoComplete="tel"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="06 12 34 56 78"
            className="min-h-touch w-full rounded-fc border border-fc-line bg-fc-surface px-3 py-2 text-fc-ink placeholder-fc-ink-mute focus:border-fc-primary focus:outline-none focus:ring-2 focus:ring-fc-primary"
          />
        </label>
      </div>

      <p className={`text-xs ${emailFilled || phoneFilled ? "text-fc-ink-mute" : "text-fc-ink-soft"}`}>
        Un e-mail <span className="font-medium">ou</span> un téléphone suffit — il en faut au moins un pour enregistrer la
        fiche.
      </p>

      <label className="flex items-start gap-2 text-sm text-fc-ink-soft">
        <input
          type="checkbox"
          checked={newsletter}
          onChange={(e) => setNewsletter(e.target.checked)}
          className="mt-0.5 h-5 w-5 flex-shrink-0 rounded border-fc-line text-fc-primary focus:ring-fc-primary"
        />
        <span>Je souhaite recevoir les actualités et événements Frip &amp; Co Street</span>
      </label>

      <p className="text-xs leading-snug text-fc-ink-mute">
        Ces coordonnées servent uniquement à retrouver la fiche en caisse et à envoyer le ticket. La newsletter est
        facultative et se désinscrit en un clic. Responsable : Frip &amp; Co.{" "}
        {dpoEmail ? `Vos droits (accès, suppression) : ${dpoEmail}.` : "Vos droits (accès, suppression) : demandez en boutique."}
      </p>

      {duplicatesVisible && (
        <section
          aria-live="polite"
          className="rounded-fc-lg border border-fc-warn/40 bg-fc-warn-soft p-3"
        >
          <h3 className="text-sm font-semibold text-fc-ink">Une fiche existe peut-être déjà</h3>
          <p className="mt-0.5 text-xs text-fc-ink-soft">
            Vérifiez avant de créer une deuxième fiche à la même personne.
          </p>
          <ul className="mt-2 space-y-2">
            {shownDuplicates.map((candidate) => {
              const name = formatClientName(candidate);
              const heading = name || candidate.email_masked || candidate.phone_masked || "Fiche sans nom";
              return (
                <li
                  key={candidate.id}
                  // Sur une petite tablette, le bouton passe sous la fiche
                  // plutôt que de lui manger la moitié de la largeur.
                  className="flex flex-col gap-2 rounded-fc border border-fc-line bg-fc-surface p-2 sm:flex-row sm:items-center"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-semibold text-fc-ink">
                      {heading}{" "}
                      <span className="text-xs font-normal text-fc-ink-mute">
                        ({duplicateReasonLabel(candidate.reason)})
                      </span>
                    </span>
                    <span className="block truncate text-xs text-fc-ink-soft">
                      {[candidate.email_masked, candidate.phone_masked].filter(Boolean).join(" · ") ||
                        "Aucune coordonnée"}
                    </span>
                    <span className="block truncate text-xs text-fc-ink-mute">
                      {candidate.visits_count > 0
                        ? `${candidate.visits_count} visite${candidate.visits_count > 1 ? "s" : ""}`
                        : "Aucune visite"}
                      {candidate.last_visit_at
                        ? ` · Dernière visite ${formatRelativeTime(candidate.last_visit_at)}`
                        : ""}
                    </span>
                  </span>
                  <button
                    type="button"
                    onClick={() => onUseExisting(candidateAsPosClient(candidate))}
                    className="min-h-touch w-full flex-shrink-0 rounded-fc bg-fc-primary px-3 text-sm font-semibold text-white transition-colors hover:bg-fc-primary-deep sm:w-auto"
                  >
                    Utiliser cette fiche
                  </button>
                </li>
              );
            })}
          </ul>
          <button
            type="button"
            onClick={() => setDuplicatesDismissed(true)}
            className="mt-2 min-h-touch w-full rounded-fc border border-fc-line bg-fc-surface px-3 text-sm font-medium text-fc-ink transition-colors hover:bg-fc-bg-alt"
          >
            Créer quand même
          </button>
        </section>
      )}

      <div className="flex gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="min-h-touch flex-1 rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-sm font-medium text-fc-ink hover:bg-fc-bg-alt"
        >
          Retour à la recherche
        </button>
        <button
          type="submit"
          disabled={!canSubmit}
          className="min-h-touch flex-1 rounded-fc-lg bg-fc-primary px-4 py-3 text-sm font-semibold text-white hover:bg-fc-primary-deep disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? "Enregistrement…" : "Créer et sélectionner"}
        </button>
      </div>
    </form>
  );
}
