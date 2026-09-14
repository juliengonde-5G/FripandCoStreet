import type { Metadata, Viewport } from "next";
import { Poppins, IBM_Plex_Mono } from "next/font/google";
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
    icon: "/favicon.png",
    shortcut: "/favicon.png",
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
      </body>
    </html>
  );
}
