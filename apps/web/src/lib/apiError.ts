/**
 * Erreur API — extrait de lib/api.ts pour être partagée sans import
 * circulaire par lib/mockApi.ts (§6 PR2 : mode démo sans backend).
 *
 * `code` porte le code métier snake_case du contrat (§5 ARCHITECTURE_PR2.md,
 * ex. "drawer_closed", "card_not_confirmed") quand l'API le fournit — les
 * pages s'en servent pour des redirections/branches précises sans parser
 * le texte français de `detail`.
 */
export class ApiError extends Error {
  status: number;
  detail: string;
  code?: string;
  retryAfter?: string;
  /**
   * Corps JSON brut de la réponse en erreur, quand il y en avait un.
   * `detail` et `code` suffisent à l'immense majorité des écrans ; certaines
   * erreurs portent en plus des champs de reprise que l'appelant doit lire
   * (PR9 K4 : `recoverable` et `failed_payment_id` sur une 409
   * `payment_failed`, pour proposer « Réessayer » en caisse). Toujours
   * traité comme une donnée inconnue : on ne suppose jamais sa forme.
   */
  body?: unknown;

  constructor(status: number, detail: string, code?: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.code = code;
  }
}

/**
 * Extrait un message d'erreur lisible d'un corps de réponse JSON — jamais
 * `String(objet)` (qui produit « [object Object] », le bug remonté par le
 * testeur sur une double annulation). Gère le `detail` texte simple du
 * contrat §5, mais aussi les formes objet/tableau qu'un backend réel peut
 * renvoyer (ex. erreurs de validation Pydantic `detail: [{msg, ...}]`, ou
 * `detail: {message, code}`) — dans ces cas, on descend d'un niveau plutôt
 * que d'accepter la structure telle quelle, avec un JSON.stringify en tout
 * dernier recours pour ne jamais rien perdre.
 */
export function extractErrorDetail(data: unknown): string {
  if (data === null || data === undefined) return "Erreur inconnue";
  if (typeof data === "string") return data.trim() || "Erreur inconnue";
  if (typeof data !== "object") return "Erreur inconnue";

  const obj = data as Record<string, unknown>;
  const detail = obj.detail;

  if (typeof detail === "string") return detail.trim() || "Erreur inconnue";

  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0];
    if (first && typeof first === "object" && typeof (first as Record<string, unknown>).msg === "string") {
      return String((first as Record<string, unknown>).msg);
    }
    try {
      return JSON.stringify(detail);
    } catch {
      return "Erreur inconnue";
    }
  }

  if (detail && typeof detail === "object") {
    const inner = detail as Record<string, unknown>;
    if (typeof inner.detail === "string") return inner.detail;
    if (typeof inner.message === "string") return inner.message;
    try {
      return JSON.stringify(detail);
    } catch {
      return "Erreur inconnue";
    }
  }

  if (typeof obj.message === "string") return obj.message;

  try {
    return JSON.stringify(obj);
  } catch {
    return "Erreur inconnue";
  }
}

/** Code métier snake_case (§5) — toujours une chaîne simple ou `undefined`. */
export function extractErrorCode(data: unknown): string | undefined {
  if (!data || typeof data !== "object") return undefined;
  const code = (data as Record<string, unknown>).code;
  return typeof code === "string" && code.length > 0 ? code : undefined;
}
