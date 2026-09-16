"use client";

/**
 * Rapports par période (PR11, docs/ARCHITECTURE_PR11.md §1, M5).
 *
 * Même ossature que l'accueil : la navigation vit dans `<Sidebar />`, la
 * page décale son contenu de la largeur de la barre (`md:ml-64`, ramené à
 * 4 rem par `globals.css` quand la barre est réduite) et réserve en haut
 * la place du bouton hamburger sur mobile (`pt-16`).
 */
import RequireAuth from "@/components/layout/RequireAuth";
import Sidebar from "@/components/layout/Sidebar";
import ReportsView from "@/components/reports/ReportsView";

export default function ReportsPage() {
  return (
    <RequireAuth>
      <Sidebar />
      <main className="md:ml-64 px-4 pt-16 pb-6 md:p-8">
        <ReportsView />
      </main>
    </RequireAuth>
  );
}
