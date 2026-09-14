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
          surface: "#FFFFFF",
          ink: "#17181A",
          "ink-soft": "#52544F",
          line: "#DAD9D3",
          primary: {
            DEFAULT: "#16433B",
            deep: "#0C2D27",
          },
          danger: "#B3261E",
          success: "#1E7B4D",
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
