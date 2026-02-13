import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        terminal: {
          bg: 'rgb(var(--t-bg) / <alpha-value>)',
          panel: 'rgb(var(--t-panel) / <alpha-value>)',
          panelAlt: 'rgb(var(--t-panelAlt) / <alpha-value>)',
          border: 'rgb(var(--t-border) / <alpha-value>)',
          text: 'rgb(var(--t-text) / <alpha-value>)',
          dim: 'rgb(var(--t-dim) / <alpha-value>)',
          positive: 'rgb(var(--t-positive) / <alpha-value>)',
          negative: 'rgb(var(--t-negative) / <alpha-value>)',
          accent: 'rgb(var(--t-accent) / <alpha-value>)',
          blue: 'rgb(var(--t-blue) / <alpha-value>)',
          violet: 'rgb(var(--t-violet) / <alpha-value>)',
        }
      },
      boxShadow: {
        glow: 'var(--shadow-glow)',
      },
      keyframes: {
        pulseUp: {
          '0%': { backgroundColor: 'rgba(46, 214, 143, 0.35)' },
          '100%': { backgroundColor: 'transparent' }
        },
        pulseDown: {
          '0%': { backgroundColor: 'rgba(242, 100, 120, 0.35)' },
          '100%': { backgroundColor: 'transparent' }
        },
      },
      animation: {
        pulseUp: 'pulseUp 0.5s ease-out',
        pulseDown: 'pulseDown 0.5s ease-out',
      },
    },
  },
  plugins: [],
};

export default config;
