import type { Config } from "tailwindcss";

// Frip & Co Street — palette neutre et sobre.
//   bg      #F5F5F3 (fond clair)
//   surface #FFFFFF
//   ink     #17181A (texte principal)
//   primary #16433B (vert forêt profond — CTA, liens)
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
          bg: "#F5F5F3",
          "bg-alt": "#EBEAE5",
          surface: "#FFFFFF",
          ink: "#17181A",
          "ink-soft": "#52544F",
          "ink-mute": "#8A8C86",
          line: "#DAD9D3",
          primary: {
            DEFAULT: "#16433B",
            deep: "#0C2D27",
            soft: "#D9E6E1",
          },
          danger: "#B3261E",
          success: "#1E7B4D",
          warn: "#8A5A1E",
          "warn-soft": "#F3E3CC",
        },
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
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
