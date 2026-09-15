"use client";

/**
 * Téléchargement binaire — helper commun (PR4) pour tous les fichiers
 * générés par l'administration : écritures comptables (CSV Pennylane),
 * fichier FEC, exports de table, archive fiscale (`.json.gz`), export
 * fiscal à la demande (JSON/XML), PDF d'un rapport Z.
 *
 * Basé sur `api.getBytesWithHeaders` (jamais un lien direct vers l'API :
 * le jeton d'authentification doit être posé, cf. lib/api.ts) +
 * `URL.createObjectURL`. Renvoie les en-têtes de la réponse pour que
 * l'appelant affiche l'empreinte (`X-Archive-SHA256`, `X-Export-SHA256`)
 * sans refaire de requête.
 */
import { api } from "./api";

export interface DownloadResult {
  /** En-têtes de la réponse, clés en minuscules. */
  headers: Record<string, string>;
}

export async function downloadFile(path: string, filename: string): Promise<DownloadResult> {
  const { bytes, headers } = await api.getBytesWithHeaders(path);
  const contentType = headers["content-type"] || "application/octet-stream";
  // `Uint8Array` copié dans un `ArrayBuffer` neuf : évite tout souci de
  // typage strict de `BlobPart` selon la cible TypeScript/lib DOM.
  const blob = new Blob([new Uint8Array(bytes)], { type: contentType });
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
  return { headers };
}

/** Empreinte courte (8 premiers caractères) pour un affichage compact en
 * liste — l'empreinte complète reste copiable via un bouton dédié. */
export function shortHash(hash: string | null | undefined): string {
  if (!hash) return "—";
  return hash.slice(0, 8);
}

/** Copie une chaîne dans le presse-papiers ; renvoie `false` sans lever en
 * cas d'échec (permission refusée, contexte non sécurisé…) — l'appelant
 * affiche alors un message de repli plutôt que de casser l'action. */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // repli silencieux ci-dessous
  }
  return false;
}
