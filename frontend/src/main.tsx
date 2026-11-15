// Must stay the first import: fills in crypto.randomUUID before any module uses it.
import './lib/polyfills';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import { ErrorBoundary } from './components/ErrorBoundary';
import App from './App';
import { initApiBase, isTauri } from './lib/api';
import { initAnalytics } from './lib/analytics';
import { modeFromSearch } from './lib/presence';
import { PresenceOverlay } from './components/presence/PresenceOverlay';
import { LivingWallpaper } from './components/presence/LivingWallpaper';
import './index.css';

function applyTheme() {
  try {
    const raw = localStorage.getItem('ghost-settings');
    const settings = raw ? JSON.parse(raw) : {};
    const theme = settings.theme || 'system';
    if (theme === 'dark') {
      document.documentElement.classList.add('dark');
      document.documentElement.classList.remove('light');
    } else if (theme === 'light') {
      document.documentElement.classList.add('light');
      document.documentElement.classList.remove('dark');
    }
  } catch { /* use system default */ }
}

applyTheme();

/**
 * Desktop app: make sure no service worker or cache from an older build is
 * serving this page. If one was in control, reload once so the freshly
 * installed version is what the user sees.
 */
async function dropStaleDesktopCache(): Promise<boolean> {
  if (!isTauri() || !('serviceWorker' in navigator)) return false;
  try {
    const regs = await navigator.serviceWorker.getRegistrations();
    await Promise.all(regs.map((r) => r.unregister()));
    if ('caches' in window) {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
    }
    const servedByOldWorker = !!navigator.serviceWorker.controller;
    if (servedByOldWorker && !sessionStorage.getItem('ghost-sw-cleared')) {
      sessionStorage.setItem('ghost-sw-cleared', '1');
      window.location.reload();
      return true;
    }
  } catch { /* nothing to clean up */ }
  return false;
}

dropStaleDesktopCache().then((reloading) => {
  if (reloading) return;
  void start();
});

// Fetch the API base URL from the Tauri backend before rendering.
// This ensures GHOST_PORT is defined in one place (the Rust backend).
// In non-Tauri environments this is a no-op.
function start() {
  // Desktop presence windows (Stonic port, phase 5) render a single small
  // page each: no router, no analytics, no app shell.
  const { mode, size } = modeFromSearch(window.location.search);
  if (mode !== 'app') {
    createRoot(document.getElementById('root')!).render(
      <StrictMode>
        {mode === 'presence' ? <PresenceOverlay initialSize={size} /> : <LivingWallpaper />}
      </StrictMode>,
    );
    return;
  }
  initApiBase().finally(() => {
    // Kick off analytics init in the background — it's never awaited so
    // a slow/failed identity fetch never delays UI render.
    void initAnalytics();

    createRoot(document.getElementById('root')!).render(
      <StrictMode>
        <ErrorBoundary>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </ErrorBoundary>
      </StrictMode>,
    );
  });
}
