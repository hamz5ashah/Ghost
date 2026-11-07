import { useEffect, useState } from 'react';
import { Download, ExternalLink } from 'lucide-react';
import { isTauri } from '../lib/api';

/** Where new installers are published (GitHub Releases of this repository). */
export const RELEASES_URL = 'https://github.com/hamz5ashah/Ghost/releases';

/** Version baked in at build time (CI sets it per build), e.g. "1.1.7". */
const BUILD_VERSION = import.meta.env.VITE_GHOST_VERSION || '';
/** Build label, e.g. "build-7 · 4ed2e57". */
const BUILD_LABEL = import.meta.env.VITE_GHOST_BUILD || 'development build';

async function appVersion(): Promise<string> {
  if (isTauri()) {
    try {
      const { getVersion } = await import('@tauri-apps/api/app');
      return await getVersion();
    } catch { /* fall back to the build-time value */ }
  }
  return BUILD_VERSION || 'dev';
}

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl p-5" style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}>
      {children}
    </div>
  );
}

/** Settings → About: app identity, exact version/build, and how to update. */
export function AboutSection() {
  const [version, setVersion] = useState(BUILD_VERSION);
  useEffect(() => { void appVersion().then(setVersion); }, []);

  return (
    <>
      <Card>
        <div className="flex items-center gap-4">
          <img src="/pwa-192x192.png" alt="" className="w-14 h-14 rounded-2xl" />
          <div className="min-w-0">
            <div className="text-lg font-semibold" style={{ color: 'var(--color-text)' }}>Ghost</div>
            <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              Personal AI that runs on your own computer.
            </div>
            <div className="text-xs mt-1 font-mono" style={{ color: 'var(--color-text-tertiary)' }}>
              Version {version || '…'} · {BUILD_LABEL}
            </div>
          </div>
        </div>
      </Card>

      <Card>
        <h3 className="text-sm font-semibold mb-1" style={{ color: 'var(--color-text)' }}>Updates</h3>
        <p className="text-xs mb-4" style={{ color: 'var(--color-text-tertiary)' }}>
          New versions are published on your GitHub Releases page. Download the newest installer and
          run it; your chats, settings and API keys are kept.
        </p>
        <a
          href={RELEASES_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-2 text-sm px-4 py-2 rounded-lg"
          style={{ background: 'var(--color-accent)', color: 'white' }}
        >
          <Download size={14} /> Get the latest version <ExternalLink size={12} />
        </a>
      </Card>

      <Card>
        <h3 className="text-sm font-semibold mb-1" style={{ color: 'var(--color-text)' }}>License</h3>
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          Copyright © 2026 Syed Hamza Ali Shah. All rights reserved.
        </p>
      </Card>
    </>
  );
}
