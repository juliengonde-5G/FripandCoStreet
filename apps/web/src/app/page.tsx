"use client";

/**
 * Accueil (PR6, docs/ARCHITECTURE_PR6.md §1, H4).
 *
 * `/` ne redirige plus vers `/caisse` : une fois connectée, la page affiche
 * le tableau de bord de performance (jour, mois, 7 derniers jours). Non
 * connectée, `RequireAuth` renvoie vers `/login` — comportement inchangé.
 */
import DashboardHome from "@/components/dashboard/DashboardHome";

export default function HomePage() {
  return <DashboardHome />;
}
