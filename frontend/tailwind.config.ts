import type { Config } from 'tailwindcss';

const config: Config = {
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
    './lib/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        // Brand palette from PLAN §2
        accent: '#ecad0a', // yellow
        brand: '#209dd7', // blue primary
        submit: '#753991', // purple (submit buttons)
        // Dark terminal surfaces (no pure black)
        terminal: {
          bg: '#0d1117',
          panel: '#131a24',
          raised: '#1a2230',
          border: '#232d3b',
          muted: '#8b96a5',
          text: '#e6edf3',
        },
        // P&L semantics
        up: '#26a269',
        down: '#e5484d',
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      keyframes: {
        flashUp: {
          '0%': { backgroundColor: 'rgba(38,162,105,0.55)' },
          '100%': { backgroundColor: 'transparent' },
        },
        flashDown: {
          '0%': { backgroundColor: 'rgba(229,72,77,0.55)' },
          '100%': { backgroundColor: 'transparent' },
        },
      },
      animation: {
        flashUp: 'flashUp 500ms ease-out',
        flashDown: 'flashDown 500ms ease-out',
      },
    },
  },
  plugins: [],
};

export default config;
