"use client";

/**
 * Cahier du jour (PR11, docs/ARCHITECTURE_PR11.md §1, M5).
 *
 * Même ossature que l'accueil : la navigation vit dans `<Sidebar />`, la
 * page décale son contenu de la largeur de la barre (`md:ml-64`, ramené à
 * 4 rem par `globals.css` quand la barre est réduite) et réserve en haut
 * la place du bouton hamburger sur mobile (`pt-16`). Tout le contenu est
 * dans `CahierDuJour`.
 */
import CahierDuJour from "@/components/cahier/CahierDuJour";
import RequireAuth from "@/components/layout/RequireAuth";
import Sidebar from "@/components/layout/Sidebar";

export default function CahierPage() {
  return (
    <RequireAuth>
      <Sidebar />
      <main className="md:ml-64 px-4 pt-16 pb-6 md:p-8">
        <CahierDuJour />
      </main>
    </RequireAuth>
  );
}
