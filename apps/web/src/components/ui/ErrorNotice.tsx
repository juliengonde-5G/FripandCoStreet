"use client";

/**
 * Message d'erreur de l'administration — un seul composant pour tous les
 * onglets (PR12 N5).
 *
 * Il affiche le message et, quand la panne vient du serveur (5xx) ou du
 * réseau, la ligne « Référence : … » à noter (voir `ErrorReference`).
 * Jamais sur une erreur métier (4xx) : son message se suffit, et un code
 * technique laisserait croire à une panne.
 *
 * `message` accepte les deux formes que manipulent les écrans : une
 * chaîne simple (message maison, sans erreur derrière) ou le résultat de
 * `describeError(err, "…")`, qui porte la référence.
 */
import React from "react";

import ErrorReference from "@/components/ui/ErrorReference";
import { errorRef, errorText, type DisplayableError } from "@/lib/apiError";

export default function ErrorNotice({ message }: { message: DisplayableError }) {
  const text = errorText(message);
  if (!text) return null;
  return (
    <div role="alert" className="rounded-fc bg-fc-danger-soft border border-fc-danger/30 px-3 py-2 text-sm text-fc-danger">
      {text}
      <ErrorReference reference={errorRef(message)} />
    </div>
  );
}
