import type { Config } from "tailwindcss";

// Frip & Co Street — charte graphique v1 (docs/CHARTE_GRAPHIQUE.md),
// mesurée sur l'affiche d'ouverture (seule source de marque disponible).
//   bg           #FFF9F9 (fond app — blanc cassé légèrement rosé de l'affiche)
//   surface      #FFFFFF
//   ink          #17181A (texte principal)
//   primary      #1B1BFB (bleu du logo — CTA, liens, focus)
//   accent       #FF66C4 (rose affiche — badges de célébration uniquement,
//                jamais pour les erreurs ; ne passe pas l'AA en texte fin)
// Contrastes AA vérifiés — voir docs/CHARTE_GRAPHIQUE.md §1.3/§6.
const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        fc: {
          bg: "#FFF9F9",
          "bg-alt": "#EBEAE5",
          surface: "#FFFFFF",
          ink: "#17181A",
          "ink-soft": "#52544F",
          "ink-mute": "#8A8C86",
          line: "#DAD9D3",
          primary: {
            DEFAULT: "#1B1BFB",
            deep: "#1212A3",
            soft: "#E8E8FF",
          },
          danger: "#C81E3A",
          "danger-soft": "#FBEDEF",
          success: "#0A6E52",
          "success-soft": "#E6F0EE",
          warn: "#8A5A1E",
          "warn-soft": "#F3E3CC",
          // Rose de l'affiche — usage décoratif/célébration uniquement
          // (badges, encarts promo). Ne jamais l'utiliser en texte fin sur
          // fond clair (2.64:1) ni sur fond primaire (3.06:1) : voir charte.
          accent: "#FF66C4",
          "accent-soft": "#FFE8F6",
        },
      },
      fontFamily: {
        // Poppins (next/font/google, layout.tsx) — police des titres et du
        // corps de texte, identifiée avec certitude dans l'affiche source.
        // Repli système si le chargement de police échoue (hors-ligne).
        sans: [
          "var(--font-poppins)",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        // IBM Plex Mono (next/font/google, layout.tsx) — chiffres de caisse
        // (montants, rendu, totaux, n° de rapport Z) : chasse fixe, combiné
        // à la classe utilitaire Tailwind `tabular-nums`.
        mono: [
          "var(--font-ibm-plex-mono)",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      borderRadius: {
        fc: "8px",
        "fc-lg": "16px",
      },
      minHeight: {
        touch: "48px",
      },
      minWidth: {
        touch: "48px",
      },
    },
  },
  plugins: [],
};
export default config;
