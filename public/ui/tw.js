/* Tailwind (CDN) theme for pages that still use utility classes in their scripts.
   Load right after the Tailwind CDN script. The blue/gray scales are remapped to the
   Indico Crosstab palette so utility-built markup matches the design system. */
tailwind.config = {
  corePlugins: { preflight: false },
  theme: {
    extend: {
      fontFamily: { sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'] },
      colors: {
        blue: {
          50: '#f2f5fd', 100: '#e7ecfa', 200: '#c8d3f3', 300: '#9fb2e9', 400: '#6f88dc',
          500: '#4a6cd4', 600: '#2f4fb3', 700: '#1f3266', 800: '#16244f', 900: '#0f1b3d',
        },
        indigo: {
          50: '#f2f5fd', 100: '#e7ecfa', 200: '#c8d3f3', 300: '#9fb2e9', 400: '#6f88dc',
          500: '#4a6cd4', 600: '#2f4fb3', 700: '#1f3266', 800: '#16244f', 900: '#0f1b3d',
        },
        gray: {
          50: '#f9fafc', 100: '#f3f4f8', 200: '#e5e8f0', 300: '#d3d8e4', 400: '#a3abbd',
          500: '#8a92a6', 600: '#5b6478', 700: '#3d4558', 800: '#262d40', 900: '#141a2e',
        },
      },
    },
  },
};
