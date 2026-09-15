"use client";

/**
 * Accueil (PR6, docs/ARCHITECTURE_PR6.md §1, H4 — barre latérale PR7, I1).
 *
 * `/` ne redirige plus vers `/caisse` : une fois connectée, la page affiche
 * le tableau de bord de performance (jour, mois, 7 derniers jours). Non
 * connectée, `RequireAuth` renvoie vers `/login` — comportement inchangé.
 *
 * PR7 : la navigation vit dans `<Sidebar />` ; la page se contente de
 * décaler son contenu de la largeur de la barre (`md:ml-64`, ramené à
 * 4 rem par `globals.css` quand la barre est réduite) et de réserver en
 * haut la place du bouton hamburger sur mobile (`pt-16`).
 */
import DashboardHome from "@/components/dashboard/DashboardHome";
import RequireAuth from "@/components/layout/RequireAuth";
import Sidebar from "@/components/layout/Sidebar";

export default function HomePage() {
  return (
    <RequireAuth>
      <Sidebar />
      <main className="md:ml-64 px-4 pt-16 pb-6 md:p-8">
        <DashboardHome />
      </main>
    </RequireAuth>
  );
}
