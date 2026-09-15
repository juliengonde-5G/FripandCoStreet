"use client";

/**
 * Onglet Vendeuses (PR8, docs/ARCHITECTURE_PR8.md §1, J3).
 *
 * Une vendeuse n'est pas un compte : le manager reste le seul utilisateur
 * de l'application. C'est un prénom et un code à quatre chiffres, qu'on
 * pose sur la caisse le temps d'un service. D'où un écran volontairement
 * court : ajouter, changer le code, désactiver — et la case qui rend
 * l'identification obligatoire en caisse.
 *
 * Une vendeuse ne se supprime jamais (les ventes passées la référencent) :
 * on la désactive, elle disparaît alors de l'écran d'identification.
 *
 * Le code n'est ni affiché, ni relu, ni renvoyé par l'API : la liste dit
 * seulement « code défini » ou « code à définir ».
 */
import React, { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import Input from "@/components/ui/Input";
import { api, ApiError } from "@/lib/api";
import { PIN_LENGTH, digitsOnly, isPinComplete } from "@/lib/cashier";
import type {
  AdminCashier,
  AdminCashierListResponse,
  AdminCashierResponse,
  PosSettings,
} from "@/lib/types";

function ErrorNotice({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div role="alert" className="rounded-fc bg-fc-danger-soft border border-fc-danger/30 px-3 py-2 text-sm text-fc-danger">
      {message}
    </div>
  );
}

/** Message d'aide commun aux deux saisies de code — même règle que le
 * serveur (J3), rappelée avant l'envoi plutôt qu'après un 422. */
const PIN_HINT = `Exactement ${PIN_LENGTH} chiffres. Évite les suites évidentes (0000, 1234, 1111…).`;

function pinLocalError(pin: string, confirmation: string): string | null {
  if (!isPinComplete(pin)) return `Le code doit contenir ${PIN_LENGTH} chiffres.`;
  if (pin !== confirmation) return "Les deux codes saisis ne sont pas identiques.";
  return null;
}

export default function CashiersTab() {
  const [cashiers, setCashiers] = useState<AdminCashier[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Pas de `setLoading(true)` sur un rafraîchissement : la liste reste à
  // l'écran pendant la relecture. Sans cela, chaque enregistrement
  // démonterait les lignes — et avec elles le « Code enregistré. » qu'on
  // vient d'afficher.
  const load = (): void => {
    api
      .get<AdminCashierListResponse>("/api/admin/cashiers")
      .then((data) => {
        setCashiers(data.cashiers ?? []);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Impossible de charger les vendeuses."))
      .finally(() => setLoading(false));
  };

  useEffect(load, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-6">
      <PosSettingsCard />

      <Card
        title="Vendeuses"
        subtitle="Les vendeuses actives apparaissent sur l'écran d'identification de la caisse."
      >
        <div className="space-y-4">
          <ErrorNotice message={error} />
          {loading ? (
            <p className="text-sm text-fc-ink-soft">Chargement…</p>
          ) : cashiers.length === 0 ? (
            <p className="text-sm text-fc-ink-soft">
              Aucune vendeuse pour l&apos;instant. Ajoutez-en une ci-dessous.
            </p>
          ) : (
            <ul className="divide-y divide-fc-line">
              {cashiers.map((cashier) => (
                <CashierRow key={cashier.id} cashier={cashier} onChanged={load} />
              ))}
            </ul>
          )}
        </div>
      </Card>

      <NewCashierCard onCreated={load} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Réglage « identification obligatoire »
// ---------------------------------------------------------------------------

function PosSettingsCard() {
  const [required, setRequired] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<PosSettings>("/api/admin/settings/pos")
      .then((data) => setRequired(!!data.cashier_required))
      .catch((err) => setError(err instanceof ApiError ? err.detail : "Réglage indisponible."))
      .finally(() => setLoading(false));
  }, []);

  const toggle = async (next: boolean): Promise<void> => {
    setSaving(true);
    setError(null);
    // Affichage optimiste : la case suit le doigt, et revient en arrière
    // si le serveur refuse.
    setRequired(next);
    try {
      const data = await api.put<PosSettings>("/api/admin/settings/pos", { cashier_required: next });
      setRequired(!!data.cashier_required);
    } catch (err) {
      setRequired(!next);
      setError(err instanceof ApiError ? err.detail : "Échec de l'enregistrement.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card title="Identification en caisse" subtitle="Qui encaisse est noté sur chaque vente et sur le rapport Z.">
      <div className="space-y-3">
        <ErrorNotice message={error} />
        <label className="flex items-start gap-3 text-sm text-fc-ink">
          <input
            type="checkbox"
            checked={required}
            disabled={loading || saving}
            onChange={(e) => void toggle(e.target.checked)}
            className="mt-0.5 h-5 w-5 flex-shrink-0 rounded border-fc-line text-fc-primary focus:ring-fc-primary"
          />
          <span>
            <span className="font-medium">Identification obligatoire en caisse</span>
            <span className="mt-0.5 block text-fc-ink-soft">
              Sans vendeuse identifiée, la caisse refuse les ventes et les mouvements. Laissée décochée, la caisse
              fonctionne comme avant et l&apos;identification reste facultative.
            </span>
          </span>
        </label>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Une ligne de la liste
// ---------------------------------------------------------------------------

function CashierRow({ cashier, onChanged }: { cashier: AdminCashier; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pinOpen, setPinOpen] = useState(false);
  const [pin, setPin] = useState("");
  const [pinConfirm, setPinConfirm] = useState("");
  const [pinSaved, setPinSaved] = useState(false);

  const setActive = async (active: boolean): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await api.put<AdminCashierResponse>(`/api/admin/cashiers/${cashier.id}`, { active });
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Échec de l'enregistrement.");
    } finally {
      setBusy(false);
    }
  };

  const savePin = async (): Promise<void> => {
    const local = pinLocalError(pin, pinConfirm);
    if (local) {
      setError(local);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.put<AdminCashierResponse>(`/api/admin/cashiers/${cashier.id}/pin`, { pin });
      setPin("");
      setPinConfirm("");
      setPinOpen(false);
      setPinSaved(true);
      setTimeout(() => setPinSaved(false), 2500);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Échec de l'enregistrement du code.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="py-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-fc-ink">{cashier.display_name}</span>
            {!cashier.active && (
              <span className="rounded-fc bg-fc-bg-alt px-2 py-0.5 text-xs font-medium text-fc-ink-soft">Désactivée</span>
            )}
          </div>
          <div className="text-xs text-fc-ink-mute">
            {cashier.has_pin ? "Code défini" : "Code à définir"}
            {pinSaved && <span className="ml-2 font-medium text-fc-success">Code enregistré.</span>}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => {
              setError(null);
              setPin("");
              setPinConfirm("");
              setPinOpen((v) => !v);
            }}
          >
            {cashier.has_pin ? "Changer le code" : "Définir le code"}
          </Button>
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void setActive(!cashier.active)}>
            {cashier.active ? "Désactiver" : "Réactiver"}
          </Button>
        </div>
      </div>

      {pinOpen && (
        <div className="mt-3 space-y-3 rounded-fc-lg bg-fc-bg-alt p-3">
          <div className="grid gap-3 sm:grid-cols-2">
            {/* Identifiants explicites : `Input` dérive sinon l'`id` du
                libellé, et deux lignes ouvertes en même temps (ou la carte
                d'ajout plus bas) partageraient le même `id` — le clic sur
                un libellé viserait alors le champ d'une autre vendeuse. */}
            <Input
              id={`pin-${cashier.id}`}
              label="Nouveau code"
              type="password"
              inputMode="numeric"
              autoComplete="off"
              maxLength={PIN_LENGTH}
              value={pin}
              onChange={(e) => setPin(digitsOnly(e.target.value).slice(0, PIN_LENGTH))}
            />
            <Input
              id={`pin-confirm-${cashier.id}`}
              label="Confirme le code"
              type="password"
              inputMode="numeric"
              autoComplete="off"
              maxLength={PIN_LENGTH}
              value={pinConfirm}
              onChange={(e) => setPinConfirm(digitsOnly(e.target.value).slice(0, PIN_LENGTH))}
            />
          </div>
          <p className="text-xs text-fc-ink-mute">{PIN_HINT}</p>
          <div className="flex items-center gap-2">
            <Button size="sm" disabled={busy} onClick={() => void savePin()}>
              {busy ? "Enregistrement…" : "Enregistrer le code"}
            </Button>
            <Button variant="ghost" size="sm" disabled={busy} onClick={() => setPinOpen(false)}>
              Annuler
            </Button>
          </div>
        </div>
      )}

      {error && (
        <div className="mt-2">
          <ErrorNotice message={error} />
        </div>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Ajout
// ---------------------------------------------------------------------------

function NewCashierCard({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [pin, setPin] = useState("");
  const [pinConfirm, setPinConfirm] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);

  const submit = async (): Promise<void> => {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Le prénom de la vendeuse est obligatoire.");
      return;
    }
    const local = pinLocalError(pin, pinConfirm);
    if (local) {
      setError(local);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const data = await api.post<AdminCashierResponse>("/api/admin/cashiers", {
        display_name: trimmed,
        pin,
      });
      setName("");
      setPin("");
      setPinConfirm("");
      setCreated(data.cashier?.display_name ?? trimmed);
      setTimeout(() => setCreated(null), 2500);
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Échec de l'enregistrement.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card title="Ajouter une vendeuse" subtitle="Le prénom s'affiche sur la caisse, le ticket et le rapport Z.">
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <ErrorNotice message={error} />
        {created && (
          <div role="status" className="rounded-fc bg-fc-primary-soft px-3 py-2 text-sm font-medium text-fc-primary-deep">
            {created} peut maintenant s&apos;identifier en caisse.
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-3">
          <Input
            id="new-cashier-name"
            label="Prénom"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={60}
            placeholder="Léa"
            autoComplete="off"
          />
          <Input
            id="new-cashier-pin"
            label="Code"
            type="password"
            inputMode="numeric"
            autoComplete="off"
            maxLength={PIN_LENGTH}
            value={pin}
            onChange={(e) => setPin(digitsOnly(e.target.value).slice(0, PIN_LENGTH))}
          />
          <Input
            id="new-cashier-pin-confirm"
            label="Confirme le code"
            type="password"
            inputMode="numeric"
            autoComplete="off"
            maxLength={PIN_LENGTH}
            value={pinConfirm}
            onChange={(e) => setPinConfirm(digitsOnly(e.target.value).slice(0, PIN_LENGTH))}
          />
        </div>
        <p className="text-xs text-fc-ink-mute">{PIN_HINT}</p>
        <Button type="submit" disabled={saving}>
          {saving ? "Enregistrement…" : "Ajouter la vendeuse"}
        </Button>
      </form>
    </Card>
  );
}
