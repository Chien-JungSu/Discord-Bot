/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./static/**/*.js"
  ],
  darkMode: 'class',
  corePlugins: {
    preflight: false,
  },
  theme: {
    extend: {
      colors: {
        brand: {
          dark: '#090d1a',
          bg: '#0b1020',
          bg2: '#111827',
          panel: 'rgba(15, 23, 42, 0.65)',
          border: 'rgba(255, 255, 255, 0.14)',
          primary: '#7dd3fc',
          secondary: '#34d399',
          accent: '#a78bfa',
          text: '#f8fafc',
          muted: '#b9c6db',
          danger: '#f87171'
        },
        surface: {
          canvas: 'var(--surface-canvas)',
          sunken: 'var(--surface-sunken)',
          raised: 'var(--surface-raised)',
          overlay: 'var(--surface-overlay)',
          inverse: 'var(--surface-inverse)',
        }
      },
      borderRadius: {
        'xs': 'var(--radius-xs)',
        'sm': 'var(--radius-sm)',
        'md': 'var(--radius-md)',
        'lg': 'var(--radius-lg)',
        'xl': 'var(--radius-xl)',
      },
      zIndex: {
        'ambient': 'var(--z-ambient)',
        'dropdown': 'var(--z-dropdown)',
        'sticky': 'var(--z-sticky)',
        'fab': 'var(--z-fab)',
        'drawer': 'var(--z-drawer)',
        'modal': 'var(--z-modal)',
        'toast': 'var(--z-toast)',
        'tooltip': 'var(--z-tooltip)',
      },
      fontFamily: {
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          '"SF Pro Display"',
          '"SF Pro Text"',
          '"Segoe UI"',
          'Roboto',
          '"Microsoft JhengHei"',
          'sans-serif'
        ]
      },
      backdropBlur: {
        glass: '28px',
        ambient: '95px'
      },
      boxShadow: {
        'glass-inset': 'inset 0 1px 1.5px 0 rgba(255, 255, 255, 0.25), inset 0 -1px 2px 0 rgba(0, 0, 0, 0.35)',
        'glass-card': '0 20px 50px rgba(0, 0, 0, 0.42)',
        'glow-cyan': '0 0 20px rgba(125, 211, 252, 0.35)'
      },
      transitionTimingFunction: {
        'spring': 'cubic-bezier(0.28, 1.45, 0.58, 1)',
        'smooth': 'cubic-bezier(0.2, 0.8, 0.2, 1)'
      }
    },
  },
  plugins: [],
}

