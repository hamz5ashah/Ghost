import path from 'path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { VitePWA } from 'vite-plugin-pwa';

// VITE_SUPABASE_ANON_KEY is intentionally NOT required here: a missing key
// disables the savings leaderboard at runtime (see src/lib/supabase.ts) rather
// than failing the build, so the package/app stays publishable without it.
// ghost gui sets the proxy target without exposing an absolute API URL to the
// browser, so requests stay on Vite's origin even on a custom frontend port.
const apiTarget = process.env.GHOST_VITE_PROXY_TARGET
  || process.env.VITE_API_URL
  || 'http://localhost:8000';

// Service worker for desktop builds: earlier builds registered a caching
// worker at /sw.js. The browser re-fetches that file on launch; this version
// clears every cache, unregisters itself and reloads open windows.
const DESKTOP_SW_CLEANUP = `self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
    await self.registration.unregister();
    const windows = await self.clients.matchAll({ type: 'window' });
    windows.forEach((w) => w.navigate(w.url));
  })());
});
`;

export default defineConfig(({ mode }) => ({
  // `vite build --mode mobile` builds the Android app: a remote client for
  // Ghost running on the user's PC (see src/components/MobileConnect.tsx).
  define: mode === 'mobile' ? { 'import.meta.env.VITE_GHOST_MOBILE': JSON.stringify('1') } : {},
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  plugins: [
    // Desktop builds replace the old service worker with one that deletes its
    // caches and unregisters itself, so an installed update shows at once.
    mode === 'desktop' && {
      name: 'ghost-desktop-sw-cleanup',
      generateBundle() {
        this.emitFile({ type: 'asset', fileName: 'sw.js', source: DESKTOP_SW_CLEANUP });
      },
    },
    react(),
    tailwindcss(),
    VitePWA({
      // The desktop and Android apps are native shells: a service worker
      // there only keeps serving the previous version after an update.
      disable: mode === 'mobile' || mode === 'desktop',
      registerType: 'autoUpdate',
      manifest: {
        name: 'Ghost',
        short_name: 'Ghost',
        description: 'On-device AI assistant',
        theme_color: '#161618',
        background_color: '#161618',
        display: 'standalone',
        icons: [
          { src: 'pwa-192x192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512x512.png', sizes: '512x512', type: 'image/png' },
        ],
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg}'],
        navigateFallbackDenylist: [/^\/v1\//, /^\/health/, /^\/dashboard/, /^\/api\//],
      },
    }),
  ],
  build: {
    outDir: '../src/ghost/server/static',
    emptyOutDir: true,
    // Preserve the Vite 6 browser baseline for existing desktop webviews.
    target: ['es2020', 'edge88', 'firefox78', 'chrome87', 'safari14'],
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: 'react', test: /node_modules[\\/](react|react-dom)[\\/]/ },
            {
              name: 'markdown',
              test: /node_modules[\\/](react-markdown|rehype-highlight|remark-gfm)[\\/]/,
            },
            { name: 'charts', test: /node_modules[\\/]recharts[\\/]/ },
            { name: 'router', test: /node_modules[\\/]react-router[\\/]/ },
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      // ws: true is required for the /v1/agents/events WebSocket. Without it
      // Vite proxies the HTTP request but not the upgrade, so the socket never
      // opens — no error, no close event, just silence — and every live agent
      // view sits empty in dev while working in a production build.
      '/v1': {
        target: apiTarget,
        changeOrigin: true,
        ws: true,
      },
      '/health': apiTarget,
      '/api': apiTarget,
    },
  },
}));
