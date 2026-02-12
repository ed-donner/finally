import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        terminal: {
          bg: '#0d1117',
          panel: '#131d2b',
          panelAlt: '#192538',
          border: '#2a3447',
          text: '#d7dfef',
          dim: '#87a0c0',
          positive: '#2ed68f',
          negative: '#f26478',
          accent: '#ecad0a',
          blue: '#209dd7',
          violet: '#753991'
        }
      },
      boxShadow: {
        glow: '0 0 0 1px rgba(32,157,215,0.35), 0 8px 24px rgba(0,0,0,0.35)',
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
