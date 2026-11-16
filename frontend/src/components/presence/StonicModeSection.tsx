import { useCallback, useEffect, useState } from 'react';
import { Brain, Loader2, Sparkles } from 'lucide-react';
import { getStonicFlags, resetStonicFlags } from '../../lib/stonic-flags';
import { memoryV2Api, stonicPresetApi, type IntentionItem, type MemoryItem, type MemoryV2Status } from '../../lib/memory-v2';
import { awarenessApi } from '../../lib/awareness';
import { presenceApi } from '../../lib/presence';
import { buildSystemPrompt } from '../../lib/assistant-prompt';
import { useAppStore } from '../../lib/store';
import { saveAvatarStyle } from './PresenceAvatar';

const card = { background: 'var(--color-surface)', border: '1px solid var(--color-border)' };
const muted = { color: 'var(--color-text-secondary)' };
const btn = 'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs cursor-pointer disabled:opacity-50';
const btnStyle = { background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' };

/** The flags one switch turns on (mirrors ghost.core.stonic_preset). */
export const ALWAYS_ACTIVE = ['realtime_voice', 'wake_word', 'awareness', 'desktop_presence', 'ui_sounds', 'characters', 'memory_v2'] as const;

/**
 * Settings → Always-Active Stonic Mode, and the Memory card (phase 6).
 *
 * The switch flips the seven `[stonic]` flags together on the backend (which
 * remembers what they were, and restores exactly that when switched off),
 * then sets up what lives on this side: awareness on, the notch and living
 * wallpaper on, the female companion as persona and avatar. Each flag also
 * stays individually switchable; nothing here changes their defaults.
 */
export function StonicModeSection() {
  const [active, setActive] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [memoryOn, setMemoryOn] = useState(false);
  const settings = useAppStore((s) => s.settings);
  const updateSettings = useAppStore((s) => s.updateSettings);

  useEffect(() => {
    void stonicPresetApi.status().then((s) => setActive(s ? s.active : null));
    void getStonicFlags().then((f) => setMemoryOn(f.memory_v2));
  }, []);

  const toggle = async (on: boolean) => {
    setBusy(true);
    setNote('');
    try {
      const res = await stonicPresetApi.set(on);
      if (!res) { setNote("Couldn't reach Ghost's server."); return; }
      resetStonicFlags();
      setActive(res.active);
      setMemoryOn(!!res.flags?.memory_v2);
      if (on) {
        updateSettings({ persona: 'companion' });
        saveAvatarStyle('light');
        const prompt = buildSystemPrompt({ ...settings, persona: 'companion' });
        await awarenessApi.configure(true, { id: 'female_soulmate', name: 'Ghost', prompt });
        await presenceApi.setPrefs({ enabled: true, wallpaper: true });
        setNote('Always-Active is on. Voice, wake word, awareness, the notch, sounds and long-term memory are all running.');
      } else {
        await awarenessApi.configure(false);
        await presenceApi.setPrefs({ enabled: false, wallpaper: false });
        setNote('Always-Active is off. Your earlier settings are back.');
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="rounded-xl p-5" style={card}>
        <h3 className="text-sm font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
          <Sparkles size={15} /> Always-Active Stonic Mode
        </h3>
        <p className="text-xs mb-3" style={muted}>
          One switch for the full companion: realtime voice, "Hey Ghost" wake word, awareness, the desktop notch and living
          wallpaper, interface sounds, characters and long-term memory. Turning it off puts every setting back the way it was.
          Computer control stays separate and off.
        </p>
        <label className="flex items-center justify-between gap-3 text-sm py-1.5" style={{ color: 'var(--color-text)' }}>
          <span className="flex items-center gap-2">{busy && <Loader2 size={13} className="animate-spin" />}Always-Active</span>
          <input
            type="checkbox"
            aria-label="Always-Active Stonic Mode"
            checked={!!active}
            disabled={busy || active === null}
            onChange={(e) => void toggle(e.target.checked)}
          />
        </label>
        {active === null && <p className="text-xs" style={muted}>Needs Ghost's server.</p>}
        {note && <p className="text-xs mt-1" style={muted}>{note}</p>}
      </div>
      {memoryOn && <MemoryV2Card />}
    </>
  );
}

function MemoryV2Card() {
  const [status, setStatus] = useState<MemoryV2Status | null>(null);
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [promises, setPromises] = useState<IntentionItem[]>([]);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    void memoryV2Api.status().then(setStatus);
    void memoryV2Api.memories().then((r) => setItems(r?.items ?? []));
    void memoryV2Api.intentions().then((r) => setPromises(r?.items ?? []));
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  const run = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try { await fn(); setNote(done); } finally { setBusy(false); refresh(); }
  };

  const s = status?.stats;
  return (
    <div className="rounded-xl p-5" style={card}>
      <h3 className="text-sm font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
        <Brain size={15} /> Long-term memory
      </h3>
      <p className="text-xs mb-3" style={muted}>
        Ghost keeps what you tell it — people, plans, preferences and promises — in a private database on this PC.
        Passwords, codes and card numbers are removed before anything is saved.
      </p>
      {s && (
        <p className="text-xs mb-3" style={{ color: 'var(--color-text)' }}>
          {s.memories} memories · {s.pinned} pinned · {s.open_intentions} open promises · {status?.backup?.count ?? 0} backups
          {status?.paused ? ' · paused' : ''}
        </p>
      )}
      {items.length > 0 && (
        <ul className="text-xs mb-3 space-y-1" style={{ color: 'var(--color-text)' }}>
          {items.map((m) => (
            <li key={m.id} className="flex items-start justify-between gap-2">
              <span>{m.pinned ? '📌 ' : ''}{m.text_original}</span>
              <button type="button" className="text-xs cursor-pointer" style={muted} disabled={busy}
                onClick={() => void run(() => memoryV2Api.forget(m.id), 'Forgotten.')}>Forget</button>
            </li>
          ))}
        </ul>
      )}
      {promises.length > 0 && (
        <ul className="text-xs mb-3 space-y-1" style={{ color: 'var(--color-text)' }}>
          {promises.map((p) => (
            <li key={p.id} className="flex items-start justify-between gap-2">
              <span>⏰ {p.what}{p.due_ts ? ` — ${new Date(p.due_ts).toLocaleString()}` : ''}</span>
              <button type="button" className="text-xs cursor-pointer" style={muted} disabled={busy}
                onClick={() => void run(() => memoryV2Api.resolve(p.id), 'Marked done.')}>Done</button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap gap-2">
        <button type="button" className={btn} style={btnStyle} disabled={busy}
          onClick={() => void run(() => memoryV2Api.pause(!status?.paused), status?.paused ? 'Remembering again.' : 'Paused — nothing new is saved.')}>
          {status?.paused ? 'Resume' : 'Pause'}
        </button>
        <button type="button" className={btn} style={btnStyle} disabled={busy}
          onClick={() => void run(() => memoryV2Api.backup(), 'Backed up.')}>Back up now</button>
        <button type="button" className={btn} style={btnStyle} disabled={busy}
          onClick={() => void run(() => memoryV2Api.rollback(), 'Copied into the classic memory — safe to switch memory v2 off.')}>
          Prepare rollback
        </button>
      </div>
      {note && <p className="text-xs mt-2" style={muted}>{note}</p>}
    </div>
  );
}
