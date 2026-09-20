import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./hooks/**/*.{ts,tsx}",
    "./utils/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Backgrounds: dark, never pure black (PLAN.md section 2).
        canvas: "#0d1117",
        panel: "#161b22",
        raised: "#1c2230",
        line: "#30363d",
        muted: "#8b949e",
        // Brand palette.
        accent: "#ecad0a", // yellow accent
        primary: "#209dd7", // blue primary
        secondary: "#753991", // purple: submit buttons
        // Market direction.
        up: "#3fb950",
        down: "#f85149",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
