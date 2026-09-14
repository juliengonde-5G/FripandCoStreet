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

  constructor(status: number, detail: string, code?: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.code = code;
  }
}
