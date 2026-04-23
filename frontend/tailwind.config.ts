import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        // Two faces only. Mono for everything data-shaped, serif only for the
        // FinAlly wordmark and major section headers — that one editorial touch.
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
        serif: ["var(--font-serif)", "ui-serif", "Georgia", "serif"],
      },
      colors: {
        // deep cool-neutral surfaces
        bg: "#0a0e14",
        surface: "#10151c",
        elevated: "#161c24",
        line: "#1f2630",
        "line-strong": "#2a3340",
        // text
        ink: "#e6edf3",
        muted: "#8b949e",
        dim: "#6e7681",
        // brand
        accent: "#ecad0a",
        info: "#209dd7",
        primary: "#753991",
        // signal
        up: "#3fb950",
        down: "#f85149",
        warn: "#d29922",
      },
      keyframes: {
        flashUp: {
          "0%": { backgroundColor: "rgba(63,185,80,0.35)" },
          "100%": { backgroundColor: "transparent" },
        },
        flashDown: {
          "0%": { backgroundColor: "rgba(248,81,73,0.35)" },
          "100%": { backgroundColor: "transparent" },
        },
        marquee: {
          "0%": { transform: "translateX(0)" },
          "100%": { transform: "translateX(-50%)" },
        },
        pulseSoft: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.4" },
        },
      },
      animation: {
        flashUp: "flashUp 600ms ease-out",
        flashDown: "flashDown 600ms ease-out",
        marquee: "marquee 60s linear infinite",
        pulseSoft: "pulseSoft 1.6s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;
