import { useCallback, useEffect, useLayoutEffect, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Lock } from 'lucide-react';
import { FACELOCK_LOCKED_EVENT, fetchFaceLockStatus, type FaceLockStatus } from '../../lib/facelock/api';
import { getStonicFlags } from '../../lib/stonic-flags';
import { UnlockPanel } from './UnlockPanel';

/** Remembered so a locked app shows its cover before the first status reply. */
const HINT_KEY = 'ghost.facelock.enabled';

function readHint(): boolean {
  try { return localStorage.getItem(HINT_KEY) === '1'; } catch { return false; }
}
function writeHint(on: boolean): void {
  try { on ? localStorage.setItem(HINT_KEY, '1') : localStorage.removeItem(HINT_KEY); } catch { /* storage off */ }
}

/**
 * Everything else on the page goes inert while locked: no focus, no clicks,
 * no keys. The decision is the backend's — it also refuses the UI's API calls
 * while locked — so this is the face of the lock, not the lock itself.
 */
function LockLayer({ children }: { children: ReactNode }) {
  const [host] = useState(() => {
    const el = document.createElement('div');
    el.setAttribute('data-facelock-host', '');
    return el;
  });
  useLayoutEffect(() => {
    document.body.appendChild(host);
    const made = new Set<Element>();
    const seal = () => {
      for (const el of Array.from(document.body.children)) {
        if (el === host || el.tagName === 'SCRIPT' || el.tagName === 'STYLE' || el.hasAttribute('inert')) continue;
        el.setAttribute('inert', '');
        made.add(el);
      }
    };
    seal();
    const watch = new MutationObserver(seal);
    watch.observe(document.body, { childList: true });
    (document.activeElement as HTMLElement | null)?.blur?.();
    return () => {
      watch.disconnect();
      made.forEach((el) => el.removeAttribute('inert'));
      host.remove();
    };
  }, [host]);
  return createPortal(children, host);
}

/** Mounted once at the app root. Inert unless the `facelock` flag is on. */
export function FaceLockGate() {
  const [flag, setFlag] = useState<boolean | null>(null);
  const [status, setStatus] = useState<FaceLockStatus | null>(null);
  const [hint] = useState(readHint);

  const refresh = useCallback(async () => {
    const s = await fetchFaceLockStatus();
    setStatus(s);
    writeHint(s.available && s.enabled);
    if (s.locked) window.dispatchEvent(new Event(FACELOCK_LOCKED_EVENT));
  }, []);

  useEffect(() => {
    void getStonicFlags().then((f) => setFlag(f.facelock));
  }, []);

  useEffect(() => {
    if (!flag) {
      if (flag === false) writeHint(false);
      return;
    }
    void refresh();
    // Windows lock / sleep re-locks in the backend; notice it promptly.
    const t = setInterval(() => void refresh(), 2000);
    const onVis = () => { if (document.visibilityState === 'visible') void refresh(); };
    document.addEventListener('visibilitychange', onVis);
    return () => { clearInterval(t); document.removeEventListener('visibilitychange', onVis); };
  }, [flag, refresh]);

  // Until the first answer: cover the app only if it was locked last time.
  if (flag === null || (flag && !status)) {
    return hint ? <LockLayer><div className="fixed inset-0 z-[100]" style={{ background: 'var(--color-bg)' }} /></LockLayer> : null;
  }
  if (!flag || !status?.locked) return null;

  return (
    <LockLayer>
      <div
        className="fixed inset-0 z-[100] flex flex-col items-center justify-center gap-6 px-6"
        style={{ background: 'var(--color-bg)' }}
        role="dialog"
        aria-modal="true"
        aria-label="Ghost is locked"
      >
        <div className="flex items-center gap-2 text-sm font-semibold" style={{ color: 'var(--color-text-secondary)' }}>
          <Lock size={15} /> Ghost is locked
        </div>
        <UnlockPanel purpose="unlock" onSuccess={() => void refresh()} />
      </div>
    </LockLayer>
  );
}
