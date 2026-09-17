"use client";

/**
 * Référence d'erreur (PR12 N5) — la ligne discrète « Référence : … » sous
 * un message d'erreur, avec un bouton pour la copier.
 *
 * Ne s'affiche que quand elle sert : panne du serveur (5xx) ou du réseau,
 * les seuls cas où quelqu'un ira lire les logs. Jamais sur une erreur
 * métier (4xx) — « caisse fermée » ou « code faux » se comprennent sans
 * code technique, et en ajouter un ferait croire à une panne.
 *
 * S'emploie de deux façons : `error` (l'erreur attrapée, le composant
 * décide lui-même) ou `reference` (l'identifiant déjà extrait par
 * `describeError`). Sans rien à montrer, le composant ne rend rien.
 */
import React, { useState } from "react";

import { copyToClipboard } from "@/lib/download";
import { errorReference } from "@/lib/apiError";

interface Props {
  /** Erreur attrapée — `ApiError`, `NetworkError` ou n'importe quoi. */
  error?: unknown;
  /** Référence déjà extraite (`describeError(...).reference`). */
  reference?: string | null;
  className?: string;
}

export default function ErrorReference({ error, reference, className = "" }: Props) {
  const [copied, setCopied] = useState(false);
  const value = reference ?? errorReference(error);
  if (!value) return null;

  const handleCopy = async (): Promise<void> => {
    const ok = await copyToClipboard(value);
    setCopied(ok);
    if (ok) setTimeout(() => setCopied(false), 2000);
  };

  return (
    <p className={`mt-1 flex flex-wrap items-center gap-2 text-xs opacity-80 ${className}`}>
      <span>
        Référence : <span className="font-mono">{value}</span>
      </span>
      <button
        type="button"
        onClick={() => void handleCopy()}
        className="min-h-touch inline-flex items-center rounded-fc border border-current px-2 py-1 font-medium hover:bg-black/5"
      >
        {copied ? "Copiée" : "Copier"}
      </button>
    </p>
  );
}
