"use client";

/**
 * Réglages → carte « Application » (PR9, contrat K6).
 *
 * Donne au manager la contrepartie durable de la bannière du bas d'écran :
 * l'état d'installation de la tablette et un bouton pour rejouer l'invite
 * native quand elle a été écartée. L'invite différée est celle capturée par
 * `lib/pwaInstall` — c'est le même et unique événement `beforeinstallprompt`,
 * partagé avec la bannière.
 *
 * Trois états possibles, tous explicités à l'écran :
 *   - **installée** : l'onglet tourne déjà en fenêtre autonome ;
 *   - **installable** : Chrome a donné une invite, le bouton la rejoue ;
 *   - **non proposée par ce navigateur** : soit l'application est déjà
 *     installée sur cette tablette, soit le navigateur ne propose pas
 *     l'installation — on indique alors le chemin manuel (menu ⋮).
 */
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import {
  getPwaInstallServerSnapshot,
  getPwaInstallSnapshot,
  promptInstall,
  setInstallDismissedForever,
  setInstallSnoozedForSession,
  subscribeToPwaInstall,
} from "@/lib/pwaInstall";

export default function AppInstallCard() {
  // Même magasin que la bannière : un « Ne plus proposer » touché en bas
  // d'écran se voit ici tout de suite, sans rechargement.
  const { standalone, canInstall, justInstalled, dismissedForever } = useSyncExternalStore(
    subscribeToPwaInstall,
    getPwaInstallSnapshot,
    getPwaInstallServerSnapshot,
  );

  // Le rendu serveur ignore l'état du navigateur : on n'affiche un
  // diagnostic qu'une fois monté, pour ne pas annoncer « non proposée »
  // le temps d'une image.
  const [mounted, setMounted] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => setMounted(true), []);

  const handleInstall = useCallback(async () => {
    setMessage(null);
    const outcome = await promptInstall();
    if (outcome === "accepted") setMessage("Installation lancée. L'icône apparaît sur l'écran d'accueil.");
    else if (outcome === "dismissed") setMessage("Installation refusée dans la fenêtre du navigateur.");
    else setMessage("Ce navigateur ne propose pas l'installation pour le moment.");
  }, []);

  const handleRestoreBanner = useCallback(() => {
    setInstallDismissedForever(false);
    setInstallSnoozedForSession(false);
    setMessage("La proposition d'installation est réactivée.");
  }, []);

  const installed = standalone || justInstalled;
  const status = !mounted ? "…" : installed ? "Installée" : canInstall ? "Installable" : "Non proposée par ce navigateur";
  const statusClass = installed
    ? "bg-fc-success-soft text-fc-success"
    : canInstall
      ? "bg-fc-primary-soft text-fc-primary-deep"
      : "bg-fc-bg-alt text-fc-ink-soft";

  return (
    <Card
      title="Application"
      subtitle="Installer la caisse sur la tablette pour un lancement direct, en plein écran."
    >
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm font-medium text-fc-ink-soft">État sur cet appareil</span>
          <span className={`rounded-fc px-3 py-1 text-sm font-semibold ${statusClass}`}>{status}</span>
        </div>

        {installed && (
          <p className="text-sm text-fc-ink-soft">
            L&apos;application est déjà installée : elle se lance depuis l&apos;icône de l&apos;écran
            d&apos;accueil, sans barre d&apos;adresse.
          </p>
        )}

        {!installed && canInstall && (
          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" onClick={() => void handleInstall()}>
              Installer l&apos;application
            </Button>
          </div>
        )}

        {mounted && !installed && !canInstall && (
          <p className="text-sm text-fc-ink-soft">
            Ce navigateur ne propose rien pour l&apos;instant : soit l&apos;application est déjà
            installée sur cette tablette, soit l&apos;installation doit se faire à la main —
            ouvre le menu <span className="font-semibold">⋮</span> de Chrome, puis
            <span className="font-semibold"> « Installer l&apos;application »</span> (ou
            « Ajouter à l&apos;écran d&apos;accueil »).
          </p>
        )}

        {mounted && dismissedForever && (
          <div className="flex flex-wrap items-center gap-3 rounded-fc bg-fc-bg-alt px-3 py-2">
            <span className="text-sm text-fc-ink-soft">
              La proposition d&apos;installation a été désactivée sur cet appareil.
            </span>
            <Button type="button" variant="outline" size="sm" onClick={handleRestoreBanner}>
              La réactiver
            </Button>
          </div>
        )}

        {message && <p className="text-sm text-fc-ink">{message}</p>}
      </div>
    </Card>
  );
}
