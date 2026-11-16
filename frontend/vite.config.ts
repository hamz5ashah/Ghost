import path from 'path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { VitePWA } from 'vite-plugin-pwa';
import { readFileSync } from 'fs';

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

// Face Lock / hand control (Stonic port, Phase 4): MediaPipe loads its loader
// script with a <script> tag, and the desktop CSP only allows scripts from the
// app's own origin — so that one small file (~320 KB) ships with the app. The
// large WASM binaries and models are downloaded on demand by the backend and
// served from /facelock-assets/ instead (see ghost/security/facelock_models.py).
const MEDIAPIPE_LOADER = 'facelock/vision_wasm_internal.js';
const mediapipeLoader = () => ({
  name: 'ghost-mediapipe-loader',
  configureServer(server: { middlewares: { use: (path: string, fn: (req: unknown, res: { setHeader: (k: string, v: string) => void; end: (b: Buffer) => void }) => void) => void } }) {
    server.middlewares.use(`/${MEDIAPIPE_LOADER}`, (_req, res) => {
      res.setHeader('Content-Type', 'text/javascript');
      res.end(readFileSync(path.resolve(import.meta.dirname, 'node_modules/@mediapipe/tasks-vision/wasm/vision_wasm_internal.js')));
    });
  },
  generateBundle(
    this: { emitFile: (f: { type: 'asset'; fileName: string; source: Buffer }) => void },
    _options: unknown,
    bundle: Record<string, unknown>,
  ) {
    // ONNX Runtime's bundle points at its default 14 MB .wasm via
    // new URL(…, import.meta.url), so Vite copies it in. Ghost always sets
    // ort.env.wasm.wasmPaths to the downloaded, hash-checked copy
    // (src/lib/facelock/engine.ts), so that default is never fetched — drop it.
    for (const name of Object.keys(bundle)) {
      if (/(^|\/)ort-wasm[^/]*\.wasm$/.test(name)) delete bundle[name];
    }
    this.emitFile({
      type: 'asset',
      fileName: MEDIAPIPE_LOADER,
      source: readFileSync(path.resolve(import.meta.dirname, 'node_modules/@mediapipe/tasks-vision/wasm/vision_wasm_internal.js')),
    });
  },
});

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
    mode !== 'mobile' && mediapipeLoader(),
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
        // Only needed once Face Lock or hand control is turned on.
        globIgnores: ['facelock/**'],
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
