import { useCallback, useEffect, useState } from 'react';
import { Eye, Monitor, Loader2 } from 'lucide-react';
import { getStonicFlags } from '../../lib/stonic-flags';
import { awarenessApi, type AwarenessStatus } from '../../lib/awareness';
import { presenceApi, type PresenceInfo } from '../../lib/presence';
import { buildSystemPrompt } from '../../lib/assistant-prompt';
import { useAppStore } from '../../lib/store';

const card = { background: 'var(--color-surface)', border: '1px solid var(--color-border)' };
const muted = { color: 'var(--color-text-secondary)' };
const btn = 'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs cursor-pointer disabled:opacity-50';
const btnStyle = { background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' };

function Toggle({ on, onChange, label, disabled }: { on: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <label className="flex items-center justify-between gap-3 text-sm py-1.5" style={{ color: 'var(--color-text)' }}>
      <span>{label}</span>
      <input type="checkbox" checked={on} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
    </label>
  );
}

const WALLPAPER_ERRORS: Record<string, string> = {
  attach: "Windows wouldn't let it sit behind the desktop icons.",
  window: "The wallpaper window couldn't be created.",
  unsupported: 'The living wallpaper needs Windows.',
};

/**
 * Settings → Security & input → Awareness and Desktop presence (Stonic port,
 * phase 5). Each card renders only when its `[stonic]` flag is on.
 */
export function AwarenessSection() {
  const [flags, setFlags] = useState({ awareness: false, presence: false });
  useEffect(() => {
    void getStonicFlags().then((f) => setFlags({ awareness: f.awareness, presence: f.desktop_presence }));
  }, []);
  return (
    <>
      {flags.awareness && <AwarenessCard />}
      {flags.presence && <PresenceCard />}
    </>
  );
}

function AwarenessCard() {
  const [status, setStatus] = useState<AwarenessStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const settings = useAppStore((s) => s.settings);

  const refresh = useCallback(() => { void awarenessApi.status().then(setStatus); }, []);
  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 10_000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const run = async (fn: () => Promise<unknown>, done?: string) => {
    setBusy(true);
    setNote('');
    try { await fn(); if (done) setNote(done); } finally { setBusy(false); refresh(); }
  };

  const enabled = !!status?.enabled;
  const engine = status?.engine;
  const thoughts = (engine?.thoughts ?? []).slice(-5).reverse();

  return (
    <div className="rounded-xl p-5" style={card}>
      <h3 className="text-sm font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
        <Eye size={15} /> Awareness
      </h3>
      <p className="text-xs mb-3" style={muted}>
        Ghost notices what's going on — idle or away, locked, asleep, the app in front, a call on the mic, battery — and now
        and then says something about it, in its own voice. It never looks at your screen or camera. Calls, video and
        conversations are left alone, and it backs off when you ignore it. Everything it notes stays on this PC.
      </p>
      <Toggle
        label="Let Ghost speak up on its own"
        on={enabled}
        disabled={busy || !status}
        onChange={(v) => void run(() => awarenessApi.configure(v, { id: 'ghost', name: 'Ghost', prompt: buildSystemPrompt(settings) }))}
      />
      {enabled && engine && (
        <>
          <p className="text-xs mt-2" style={muted}>
            {engine.label ?? ''}
            {typeof engine.spokenToday === 'number' && ` · spoke ${engine.spokenToday}× today`}
          </p>
          <div className="flex flex-wrap gap-2 mt-3">
            <button type="button" className={btn} style={btnStyle} disabled={busy} onClick={() => void run(() => awarenessApi.quiet(60), 'Quiet for an hour.')}>
              Quiet for an hour
            </button>
            <button type="button" className={btn} style={btnStyle} disabled={busy} onClick={() => void run(() => awarenessApi.thinkNow(), 'Done — it will speak if it has something to say.')}>
              Think now
            </button>
            <button type="button" className={btn} style={btnStyle} disabled={busy}
              onClick={() => { if (window.confirm('Delete everything awareness has noted (diary, thoughts, notes)?')) void run(() => awarenessApi.forget(), 'Forgotten.'); }}>
              Forget everything
            </button>
            {busy && <Loader2 size={14} className="animate-spin self-center" />}
          </div>
          {thoughts.length > 0 && (
            <div className="mt-4">
              <div className="text-xs font-medium mb-1" style={{ color: 'var(--color-text)' }}>Recent thoughts</div>
              <ul className="text-xs space-y-1" style={muted}>
                {thoughts.map((t) => (
                  <li key={t.t} dir="auto">
                    {new Date(t.t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} —{' '}
                    {t.spoke && t.say ? `said "${t.say}"` : t.note || 'stayed quiet'}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
      {note && <p className="text-xs mt-2" style={muted}>{note}</p>}
    </div>
  );
}

function PresenceCard() {
  const [info, setInfo] = useState<PresenceInfo | null>(null);
  useEffect(() => {
    const read = () => void presenceApi.state().then(setInfo);
    read();
    const t = window.setInterval(read, 5_000);
    return () => window.clearInterval(t);
  }, []);
  const set = async (patch: Parameters<typeof presenceApi.setPrefs>[0]) => {
    const next = await presenceApi.setPrefs(patch);
    if (next) setInfo(next);
  };

  if (!info) {
    return (
      <div className="rounded-xl p-5 text-xs" style={{ ...card, ...muted }}>
        Desktop presence is part of the Ghost desktop app.
      </div>
    );
  }
  const p = info.prefs;
  return (
    <div className="rounded-xl p-5" style={card}>
      <h3 className="text-sm font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
        <Monitor size={15} /> Desktop presence
      </h3>
      <p className="text-xs mb-3" style={muted}>
        When you minimise Ghost, a small notch stays at the top of the screen: hover it to talk, hush or bring Ghost back.
        It steps aside for full-screen apps and never takes focus.
      </p>
      <Toggle label="Stay on the desktop when minimised" on={p.enabled} onChange={(v) => void set({ enabled: v })} />
      <label className="flex items-center justify-between gap-3 text-sm py-1.5" style={{ color: 'var(--color-text)' }}>
        <span>Size</span>
        <select value={p.size} onChange={(e) => void set({ size: e.target.value as 's' | 'm' | 'l' })}>
          <option value="s">Small</option>
          <option value="m">Medium</option>
          <option value="l">Large</option>
        </select>
      </label>
      {info.wallpaper_supported && (
        <>
          <Toggle label="Living wallpaper behind the desktop icons" on={p.wallpaper} onChange={(v) => void set({ wallpaper: v })} />
          <p className="text-xs" style={muted}>
            {info.wallpaper === 'starting' ? 'Starting…'
              : info.wallpaper === 'on' ? 'On — it rests whenever the desktop is covered, and slows down on battery.'
              : info.wallpaper_error ? `Turned off: ${WALLPAPER_ERRORS[info.wallpaper_error] ?? info.wallpaper_error}` : ''}
          </p>
        </>
      )}
    </div>
  );
}
