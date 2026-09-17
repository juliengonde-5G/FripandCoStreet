import type { Metadata, Viewport } from "next";
import { Poppins, IBM_Plex_Mono } from "next/font/google";

import PwaInstallBanner from "@/components/pwa/PwaInstallBanner";
import ServiceWorkerRegistration from "@/components/pwa/ServiceWorkerRegistration";
import "./globals.css";

// Charte graphique (docs/CHARTE_GRAPHIQUE.md) — Poppins pour les titres et
// le corps de texte (police déclarée dans l'affiche source, Google Fonts
// libre OFL) ; IBM Plex Mono pour les chiffres de caisse (chasse fixe +
// tabular-nums). `display: "swap"` évite tout FOIT bloquant ; en cas
// d'échec réseau au build, Next.js retombe automatiquement sur les piles
// système déclarées en repli (voir tailwind.config.ts).
const poppins = Poppins({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-poppins",
  display: "swap",
});

const ibmPlexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-ibm-plex-mono",
  display: "swap",
});

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  themeColor: "#1B1BFB",
};

export const metadata: Metadata = {
  title: "Frip & Co Street — Caisse",
  description: "Frip & Co Street — caisse boutique éphémère, Rouen.",
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "any" },
      { url: "/favicon.png", type: "image/png" },
    ],
    shortcut: "/favicon.ico",
    apple: "/apple-touch-icon.png",
  },
  // Application installable (PR9, contrat K6) — le manifeste est un fichier
  // statique (`public/manifest.webmanifest`), pas une route générée : c'est
  // Chrome sur la tablette Android qui le lit pour proposer l'installation.
  manifest: "/manifest.webmanifest",
  applicationName: "Frip & Co Street",
  appleWebApp: {
    capable: true,
    title: "Frip & Co",
    statusBarStyle: "default",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="fr" className={`${poppins.variable} ${ibmPlexMono.variable}`}>
      <body className="font-sans antialiased bg-background text-foreground">
        {children}
        {/* Application installable (PR9, K6) — rendus ici pour être présents
            sur toutes les pages, écran de connexion compris. */}
        <ServiceWorkerRegistration />
        <PwaInstallBanner />
      </body>
    </html>
  );
}
