/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Geist",
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: [
          "Geist Mono",
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
        display: ["Cabinet Grotesk", "Geist", "Inter", "sans-serif"],
      },
      colors: {
        ink: {
          950: "#0a0a0c",
          900: "#101013",
          850: "#14141a",
          800: "#1a1a22",
          700: "#23232c",
          600: "#2e2e38",
          500: "#3a3a47",
          400: "#5b5b6a",
          300: "#8a8a99",
          200: "#b8b8c4",
          100: "#e6e6ec",
          50: "#f4f4f7",
        },
        amber: {
          glow: "#f5a524",
          pulse: "#fbbf24",
          deep: "#b45309",
        },
        ember: {
          500: "#f25c54",
          400: "#f7837c",
        },
        moss: {
          500: "#22c55e",
          400: "#4ade80",
        },
        sea: {
          500: "#0ea5b7",
          400: "#22d3ee",
        },
        plum: {
          500: "#a855f7",
          400: "#c084fc",
        },
      },
      boxShadow: {
        "soft-lift": "0 1px 0 rgba(255,255,255,0.04) inset, 0 8px 24px -10px rgba(0,0,0,0.5)",
        "diffuse": "0 20px 40px -20px rgba(0,0,0,0.55)",
        "ring-amber": "0 0 0 1px rgba(245,165,36,0.18), 0 8px 24px -10px rgba(245,165,36,0.25)",
      },
      keyframes: {
        pulseDot: {
          "0%, 100%": { opacity: "0.6", transform: "scale(0.95)" },
          "50%": { opacity: "1", transform: "scale(1.05)" },
        },
        breathe: {
          "0%, 100%": { opacity: "0.5" },
          "50%": { opacity: "1" },
        },
        scan: {
          "0%": { transform: "translateY(-100%)" },
          "100%": { transform: "translateY(100%)" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
      },
      animation: {
        "pulse-dot": "pulseDot 1.6s cubic-bezier(0.4, 0, 0.2, 1) infinite",
        breathe: "breathe 2.4s cubic-bezier(0.4, 0, 0.2, 1) infinite",
        scan: "scan 3.2s linear infinite",
        shimmer: "shimmer 2.4s linear infinite",
      },
    },
  },
  plugins: [],
};
