/**
 * Déconnexion — action unique partagée par la barre latérale (I1) et la
 * barre haute de la caisse (I2), docs/ARCHITECTURE_PR7.md §1.
 *
 * On invalide le jeton côté serveur, puis on efface la session locale même
 * si l'appel a échoué (réseau coupé, jeton déjà expiré) : rester connectée
 * localement alors que la vendeuse a demandé à sortir serait pire.
 * La redirection vers `/login` reste à la charge de l'appelant (routeur).
 */
import { fetchAPI } from "./api";
import { clearSession } from "./auth";

export async function logout(): Promise<void> {
  try {
    await fetchAPI("/api/auth/logout", { method: "POST" });
  } catch {
    // Déconnexion locale malgré tout — voir en-tête.
  } finally {
    clearSession();
  }
}
