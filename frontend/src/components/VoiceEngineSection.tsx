import { useCallback, useEffect, useState } from 'react';
import { AudioLines, Check, Download, Loader2, Monitor, Sparkles, Cloud, ExternalLink } from 'lucide-react';
import { useAppStore } from '../lib/store';
import {
  fetchElevenLabsVoices,
  fetchVoiceEngines,
  type ElevenLabsVoice,
  getCloudKeyStatus,
  isTauri,
  saveCloudKey,
  startOfflineVoiceDownload,
  type VoiceEngines,
} from '../lib/api';
import { listSystemVoices, pickSystemVoice, useTtsStore, type VoiceEngine } from '../lib/tts';
import { butlerGreeting, butlerStatusLine } from '../lib/assistant-prompt';

/** ElevenLabs premade voices (IDs are public, from ElevenLabs' voice library). */
const ELEVEN_VOICES: Record<string, string> = {
  JBFqnCBsd6RMkjVDRZzb: 'George — British, warm',
  onwK4e9ZLuTAKqWW03F9: 'Daniel — British, authoritative',
  nPczCjzI2devNBz1zQrb: 'Brian — American, deep',
  pNInz6obpgDQGcFmaJgB: 'Adam — American, narration',
  XB0fDUnXU5powFXDhCwa: 'Charlotte — British/Swedish, calm',
  EXAVITQu4vr4xnSDxMaL: 'Sarah — American, soft',
};

const fieldStyle = {
  background: 'var(--color-bg-secondary)',
  color: 'var(--color-text)',
  border: '1px solid var(--color-border)',
};

const ENGINES: Array<{ key: Exclude<VoiceEngine, 'auto'>; label: string; hint: string; icon: typeof Monitor }> = [
  { key: 'kokoro_onnx', label: 'Natural voice — offline', hint: 'Human-like, runs on your PC. One-time 350 MB download.', icon: Sparkles },
  { key: 'elevenlabs', label: 'ElevenLabs — online', hint: 'Most realistic. Needs internet and your ElevenLabs key.', icon: Cloud },
  { key: 'system', label: 'Windows voice', hint: 'Instant and built in, more robotic.', icon: Monitor },
];

const mb = (n: number) => `${Math.round(n / 1e6)} MB`;

/** Settings → Assistant → Voice engine: system, offline natural (Kokoro), or ElevenLabs. */
export function VoiceEngineSection() {
  const settings = useAppStore((s) => s.settings);
  const updateSettings = useAppStore((s) => s.updateSettings);
  const speak = useTtsStore((s) => s.speak);
  const [engines, setEngines] = useState<VoiceEngines | null>(null);
  const [elevenKey, setElevenKey] = useState('');
  const [elevenConnected, setElevenConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [accountVoices, setAccountVoices] = useState<ElevenLabsVoice[]>([]);
  const [customVoiceId, setCustomVoiceId] = useState('');
  const [systemVoiceList, setSystemVoiceList] = useState<SpeechSynthesisVoice[]>(() => listSystemVoices());

  // System voices load asynchronously in Chromium/WebView2.
  useEffect(() => {
    const synth = typeof window !== 'undefined' && 'speechSynthesis' in window ? window.speechSynthesis : null;
    if (!synth) return;
    const load = () => setSystemVoiceList(synth.getVoices());
    load();
    synth.addEventListener('voiceschanged', load);
    return () => synth.removeEventListener('voiceschanged', load);
  }, []);

  // 'auto' shows as the natural voice when it is ready, else the system voice.
  const engine: Exclude<VoiceEngine, 'auto'> = settings.voiceEngine === 'auto'
    ? (engines?.kokoro_onnx?.available ? 'kokoro_onnx' : 'system')
    : settings.voiceEngine;

  const refresh = useCallback(async () => {
    try {
      setEngines(await fetchVoiceEngines());
    } catch { /* server not running yet */ }
    if (isTauri()) {
      getCloudKeyStatus().then((s) => setElevenConnected(!!s.ELEVENLABS_API_KEY)).catch(() => {});
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  // ElevenLabs: load the voices in the user's account once it is connected.
  const elevenReady = !!engines?.elevenlabs?.available || elevenConnected;
  useEffect(() => {
    if (engine !== 'elevenlabs' || !elevenReady) return;
    fetchElevenLabsVoices().then(setAccountVoices).catch(() => setAccountVoices([]));
  }, [engine, elevenReady]);

  // Poll while the offline voice downloads.
  const downloading = engines?.kokoro_onnx?.download?.state === 'downloading';
  useEffect(() => {
    if (!downloading) return;
    const t = setInterval(() => { void refresh(); }, 1500);
    return () => clearInterval(t);
  }, [downloading, refresh]);

  const sample = settings.replyLanguage === 'urdu'
    ? `${butlerGreeting(settings.honorific, new Date(), 'urdu')} ${butlerStatusLine(true, 'urdu')}`
    : settings.persona === 'jarvis'
      ? `${butlerGreeting(settings.honorific)} ${butlerStatusLine(true)}`
      : 'Hello. This is how Ghost will sound when it reads replies aloud.';

  const download = async () => {
    setError('');
    try {
      await startOfflineVoiceDownload();
      updateSettings({ voiceEngine: 'kokoro_onnx' });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const saveEleven = async () => {
    setBusy(true);
    setError('');
    try {
      await saveCloudKey('ELEVENLABS_API_KEY', elevenKey.trim());
      setElevenKey('');
      updateSettings({ voiceEngine: 'elevenlabs' });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const kokoro = engines?.kokoro_onnx;
  const dl = kokoro?.download;
  const systemVoices = systemVoiceList.filter((v) => /^en/i.test(v.lang));

  return (
    <div className="rounded-xl p-5" style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}>
      <h3 className="text-sm font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
        <AudioLines size={15} /> Voice engine
      </h3>
      <p className="text-xs mb-4" style={{ color: 'var(--color-text-tertiary)' }}>
        The voice Ghost uses to speak. If the chosen engine is unavailable, Ghost falls back to the Windows voice.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 mb-4" role="radiogroup" aria-label="Voice engine">
        {ENGINES.map((e) => {
          const active = engine === e.key;
          const Icon = e.icon;
          const ready = e.key === 'system' || (e.key === 'kokoro_onnx' ? !!kokoro?.available : !!engines?.elevenlabs?.available || elevenConnected);
          return (
            <button
              key={e.key}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => updateSettings({ voiceEngine: e.key })}
              className="text-left px-3 py-3 rounded-lg cursor-pointer transition-colors"
              style={{
                background: active ? 'var(--color-accent-subtle)' : 'var(--color-bg-secondary)',
                border: `1px solid ${active ? 'var(--color-accent)' : 'var(--color-border)'}`,
              }}
            >
              <span className="flex items-center gap-2 text-sm font-medium" style={{ color: active ? 'var(--color-accent)' : 'var(--color-text)' }}>
                <Icon size={14} /> {e.label}
              </span>
              <span className="block text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>{e.hint}</span>
              <span className="block text-[11px] mt-1.5" style={{ color: ready ? 'var(--color-success)' : 'var(--color-text-tertiary)' }}>
                {ready ? '● Ready' : '○ Needs setup'}
              </span>
            </button>
          );
        })}
      </div>

      {engine === 'kokoro_onnx' && (
        <div className="py-2">
          {!kokoro ? (
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>Start a model first (AI Model tab) — the voice runs inside Ghost's server.</p>
          ) : !kokoro.available ? (
            <div>
              {dl?.state === 'downloading' ? (
                <div>
                  <div className="text-xs mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
                    Downloading the natural voice… {mb(dl.downloaded)} of {mb(dl.total)}
                  </div>
                  <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--color-bg-tertiary, var(--color-border))' }}>
                    <div className="h-full rounded-full transition-all" style={{ width: `${Math.min(100, (dl.downloaded / Math.max(1, dl.total)) * 100)}%`, background: 'var(--color-accent)' }} />
                  </div>
                </div>
              ) : (
                <div className="flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    onClick={() => void download()}
                    className="flex items-center gap-2 text-sm px-4 py-2 rounded-lg cursor-pointer"
                    style={{ background: 'var(--color-accent)', color: 'white' }}
                  >
                    <Download size={14} /> Download natural voice (350 MB)
                  </button>
                  <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    Downloaded once; after that it works with no internet.
                  </span>
                  {dl?.state === 'error' && <span className="text-xs w-full" style={{ color: 'var(--color-error)' }}>Download failed: {dl.error}. Try again.</span>}
                </div>
              )}
            </div>
          ) : (
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
              <div className="text-sm flex items-center gap-1.5" style={{ color: 'var(--color-text)' }}>
                <Check size={14} style={{ color: 'var(--color-success)' }} /> Voice
              </div>
              <div className="flex items-center gap-2">
                <select
                  value={settings.kokoroVoice}
                  onChange={(e) => updateSettings({ kokoroVoice: e.target.value })}
                  className="text-sm px-3 py-1.5 rounded-lg outline-none w-64"
                  style={fieldStyle}
                  aria-label="Natural voice"
                >
                  {Object.entries(kokoro.voices).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
                </select>
                <TestButton onClick={() => void speak('voice-test', sample)} />
              </div>
            </div>
          )}
        </div>
      )}

      {engine === 'elevenlabs' && (
        <div className="py-2 flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="password"
              value={elevenKey}
              onChange={(e) => setElevenKey(e.target.value)}
              placeholder={elevenConnected ? 'Key saved in secure storage — paste a new one to replace' : 'ElevenLabs API key'}
              autoComplete="off"
              spellCheck={false}
              aria-label="ElevenLabs API key"
              className="flex-1 min-w-[220px] text-sm px-3 py-1.5 rounded-lg outline-none font-mono"
              style={fieldStyle}
            />
            <button
              type="button"
              onClick={() => void saveEleven()}
              disabled={busy || !elevenKey.trim()}
              className="text-xs px-3 py-1.5 rounded-lg cursor-pointer disabled:opacity-50"
              style={{ background: 'var(--color-accent)', color: 'white' }}
            >
              {busy ? <Loader2 size={12} className="animate-spin" /> : 'Save'}
            </button>
            <a href="https://elevenlabs.io/app/settings/api-keys" target="_blank" rel="noopener noreferrer"
              className="text-xs flex items-center gap-1" style={{ color: 'var(--color-accent)' }}>
              Get a key <ExternalLink size={11} />
            </a>
          </div>
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
            <div className="text-sm" style={{ color: 'var(--color-text)' }}>Voice</div>
            <div className="flex items-center gap-2">
              <select
                value={settings.elevenVoice}
                onChange={(e) => updateSettings({ elevenVoice: e.target.value })}
                className="text-sm px-3 py-1.5 rounded-lg outline-none w-64"
                style={fieldStyle}
                aria-label="ElevenLabs voice"
              >
                {accountVoices.length > 0 && (
                  <optgroup label="Your ElevenLabs voices">
                    {accountVoices.map((v) => (
                      <option key={v.id} value={v.id}>{v.name}{v.description ? ` — ${v.description}` : ''}</option>
                    ))}
                  </optgroup>
                )}
                <optgroup label="Popular voices">
                  {Object.entries(ELEVEN_VOICES).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
                </optgroup>
                {!ELEVEN_VOICES[settings.elevenVoice] && !accountVoices.some((v) => v.id === settings.elevenVoice) && (
                  <option value={settings.elevenVoice}>Custom: {settings.elevenVoice}</option>
                )}
              </select>
              <TestButton onClick={() => void speak('voice-test', sample)} />
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={customVoiceId}
              onChange={(e) => setCustomVoiceId(e.target.value)}
              placeholder="Or paste a voice ID from the ElevenLabs Voice Library"
              spellCheck={false}
              aria-label="ElevenLabs voice ID"
              className="flex-1 min-w-[220px] text-sm px-3 py-1.5 rounded-lg outline-none font-mono"
              style={fieldStyle}
            />
            <button
              type="button"
              disabled={!/^[A-Za-z0-9]{10,40}$/.test(customVoiceId.trim())}
              onClick={() => { updateSettings({ elevenVoice: customVoiceId.trim() }); setCustomVoiceId(''); }}
              className="text-xs px-3 py-1.5 rounded-lg cursor-pointer disabled:opacity-50"
              style={{ color: 'var(--color-accent)', border: '1px solid var(--color-accent)' }}
            >
              Use this voice
            </button>
            <a href="https://elevenlabs.io/app/voice-library" target="_blank" rel="noopener noreferrer"
              className="text-xs flex items-center gap-1" style={{ color: 'var(--color-accent)' }}>
              Browse Voice Library <ExternalLink size={11} />
            </a>
          </div>
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            Voices you add in ElevenLabs show up in the list above. Text is sent to ElevenLabs to be spoken;
            usage counts against your ElevenLabs plan.
          </p>
        </div>
      )}

      {engine === 'system' && (
        <div className="py-2 flex flex-col sm:flex-row sm:items-center justify-between gap-2">
          <div>
            <div className="text-sm" style={{ color: 'var(--color-text)' }}>Voice</div>
            <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              Automatic: {pickSystemVoice('', settings.persona)?.name ?? 'system default'}. More: Windows Settings → Time & language → Speech.
            </div>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={settings.voiceName}
              onChange={(e) => updateSettings({ voiceName: e.target.value })}
              className="text-sm px-3 py-1.5 rounded-lg outline-none w-56"
              style={fieldStyle}
              aria-label="Voice"
            >
              <option value="">Automatic</option>
              {systemVoices.map((v) => <option key={v.name} value={v.name}>{v.name}</option>)}
            </select>
            <TestButton onClick={() => void speak('voice-test', sample)} />
          </div>
        </div>
      )}

      {error && <p role="alert" className="text-xs mt-2" style={{ color: 'var(--color-error)' }}>{error}</p>}
    </div>
  );
}

function TestButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="text-xs px-3 py-1.5 rounded-lg cursor-pointer"
      style={{ color: 'var(--color-accent)', border: '1px solid var(--color-accent)' }}
    >
      Test
    </button>
  );
}
