/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        ink: { 950: '#0b0f17', 900: '#111827', 800: '#1a2233', 700: '#243044', 600: '#33415a', 400: '#7d8ba3', 300: '#a8b3c7', 200: '#d3dae6' },
        brand: { 500: '#2aabee', 400: '#4fbcf3', 600: '#1f8fcc' },
      },
    },
  },
  plugins: [],
}
