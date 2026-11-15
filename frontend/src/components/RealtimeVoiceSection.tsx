import { useCallback, useEffect, useState } from 'react';
import { AudioLines, Check, Download, Ear, Loader2, Radio, Zap } from 'lucide-react';
import { useAppStore } from '../lib/store';
import { getStonicFlags } from '../lib/stonic-flags';
import {
  BRAIN_OPTIONS,
  fetchRealtimeStatus,
  installRealtimeRuntime,
  startWakeWord,
  stopWakeWord,
  type ConversationEngine,
  type RealtimeStatus,
} from '../lib/realtime-voice';

const fieldStyle = {
  background: 'var(--color-bg-secondary)',
  color: 'var(--color-text)',
  border: '1px solid var(--color-border)',
};

const ENGINES: Array<{ key: ConversationEngine; label: string; hint: string; icon: typeof Zap }> = [
  {
    key: 'ghost',
    label: 'Ghost voice (default)',
    hint: 'Kokoro speaks, Whisper listens. Works offline on your PC.',
    icon: AudioLines,
  },
  {
    key: 'realtime',
    label: 'Realtime engine',
    hint: 'Streaming conversation with smart turn-taking. Online, uses your own keys.',
    icon: Zap,
  },
];

const KEY_LABELS: Record<keyof RealtimeStatus['keys'], string> = {
  gemini: 'Gemini (needed for listening and speaking)',
  anthropic: 'Claude',
  deepseek: 'DeepSeek',
  openrouter: 'OpenRouter',
};

/**
 * Settings → Assistant → Conversation engine.
 *
 * Renders nothing unless the `realtime_voice` flag is on, so the Assistant
 * page looks exactly as before for everyone who has not opted in.
 */
export function RealtimeVoiceSection() {
  const settings = useAppStore((s) => s.settings);
  const updateSettings = useAppStore((s) => s.updateSettings);
  const [flagOn, setFlagOn] = useState(false);
  const [wakeFlag, setWakeFlag] = useState(false);
  const [status, setStatus] = useState<RealtimeStatus | null>(null);
  const [wakeOn, setWakeOn] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    void getStonicFlags().then((f) => { setFlagOn(f.realtime_voice); setWakeFlag(f.wake_word); });
  }, []);

  const refresh = useCallback(async () => {
    try { setStatus(await fetchRealtimeStatus()); } catch { /* server still starting */ }
  }, []);

  useEffect(() => {
    if (!flagOn) return;
    void refresh();
    const t = setInterval(() => void refresh(), 3000);
    return () => clearInterval(t);
  }, [flagOn, refresh]);

  if (!flagOn) return null;

  const runtimeState = status?.runtime.state ?? 'unknown';
  const installing = runtimeState === 'installing';
  const installed = runtimeState === 'ready';
  const realtime = settings.conversationEngine === 'realtime';

  const install = async () => {
    setError('');
    try { await installRealtimeRuntime(); await refresh(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const toggleWake = async () => {
    setError('');
    try {
      if (wakeOn) { await stopWakeWord(); setWakeOn(false); }
      else { await startWakeWord(); setWakeOn(true); }
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <div className="rounded-xl p-5" style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}>
      <h3 className="text-sm font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
        <Radio size={15} /> Conversation engine
      </h3>
      <p className="text-xs mb-4" style={{ color: 'var(--color-text-tertiary)' }}>
        Which engine Voice Mode uses. Read-aloud and dictation always use Ghost voice.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-4" role="radiogroup" aria-label="Conversation engine">
        {ENGINES.map((en) => {
          const active = settings.conversationEngine === en.key;
          const Icon = en.icon;
          return (
            <button
              key={en.key}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => updateSettings({ conversationEngine: en.key })}
              className="text-left px-3 py-2.5 rounded-lg cursor-pointer transition-colors"
              style={{
                background: active ? 'var(--color-accent-subtle)' : 'var(--color-bg-secondary)',
                border: `1px solid ${active ? 'var(--color-accent)' : 'var(--color-border)'}`,
              }}
            >
              <div className="text-sm font-medium flex items-center gap-1.5" style={{ color: active ? 'var(--color-accent)' : 'var(--color-text)' }}>
                <Icon size={14} /> {en.label}
              </div>
              <div className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{en.hint}</div>
            </button>
          );
        })}
      </div>

      {realtime && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 text-sm" style={{ color: 'var(--color-text)' }}>
            {installed ? (
              <span className="inline-flex items-center gap-1.5" style={{ color: 'var(--color-success)' }}>
                <Check size={14} /> Engine installed
              </span>
            ) : (
              <button
                type="button"
                onClick={() => void install()}
                disabled={installing}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg cursor-pointer text-sm"
                style={fieldStyle}
              >
                {installing ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                {installing ? 'Installing… (about 375 MB)' : 'Install realtime engine (about 375 MB)'}
              </button>
            )}
            {runtimeState === 'error' && (
              <span className="text-xs" style={{ color: 'var(--color-error)' }}>{status?.runtime.error}</span>
            )}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <label className="text-sm" style={{ color: 'var(--color-text)' }}>
              Thinks with
              <select
                value={settings.realtimeBrain}
                onChange={(e) => updateSettings({ realtimeBrain: e.target.value as typeof settings.realtimeBrain })}
                className="mt-1 w-full text-sm px-3 py-2 rounded-lg outline-none"
                style={fieldStyle}
              >
                {BRAIN_OPTIONS.map((b) => (
                  <option key={b.key || 'auto'} value={b.key} disabled={!!b.key && status ? !status.keys[b.key] : false}>
                    {b.label}{b.key && status && !status.keys[b.key] ? ' — no key saved' : ''}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-sm" style={{ color: 'var(--color-text)' }}>
              Voice
              <select
                value={settings.realtimeVoiceMode}
                onChange={(e) => updateSettings({ realtimeVoiceMode: e.target.value as typeof settings.realtimeVoiceMode })}
                className="mt-1 w-full text-sm px-3 py-2 rounded-lg outline-none"
                style={fieldStyle}
              >
                <option value="tts">Expressive (directed speech)</option>
                <option value="live">Fastest (live voice)</option>
              </select>
            </label>
          </div>

          {status && (
            <ul className="text-xs space-y-0.5" style={{ color: 'var(--color-text-tertiary)' }} aria-label="Saved keys">
              {(Object.keys(KEY_LABELS) as Array<keyof RealtimeStatus['keys']>).map((k) => (
                <li key={k} style={{ color: status.keys[k] ? 'var(--color-text-secondary)' : undefined }}>
                  {status.keys[k] ? '✓' : '·'} {KEY_LABELS[k]}
                </li>
              ))}
              <li className="pt-1">Keys are read from this computer&apos;s keyring (Settings → Cloud keys) and go straight to the provider.</li>
            </ul>
          )}

          {wakeFlag && (
            <button
              type="button"
              onClick={() => void toggleWake()}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg cursor-pointer text-sm"
              style={fieldStyle}
              aria-pressed={wakeOn}
            >
              <Ear size={14} />
              {wakeOn
                ? `Listening for “${status?.wake_word_name || 'Ghost'}” — turn off`
                : `Listen for “${status?.wake_word_name || 'Ghost'}”`}
            </button>
          )}
          {wakeFlag && (
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              The wake word is recognised on this PC with Whisper; nothing is sent anywhere until you start talking.
            </p>
          )}
        </div>
      )}
      {error && <p role="alert" className="text-xs mt-3" style={{ color: 'var(--color-error)' }}>{error}</p>}
    </div>
  );
}
