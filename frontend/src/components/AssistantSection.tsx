import { useEffect, useState } from 'react';
import { Sparkles, Volume2, Crown, Briefcase } from 'lucide-react';
import { useTtsStore } from '../lib/tts';
import { VoiceEngineSection } from './VoiceEngineSection';
import { butlerGreeting, type Persona } from '../lib/assistant-prompt';
import { useAppStore } from '../lib/store';
import type { ResponseStyle } from '../lib/assistant-prompt';

const STYLES: Array<{ key: ResponseStyle; label: string; hint: string }> = [
  { key: 'balanced', label: 'Balanced', hint: 'Clear answers with the detail that matters' },
  { key: 'concise', label: 'Concise', hint: 'Short and to the point' },
  { key: 'detailed', label: 'Detailed', hint: 'Thorough, with steps and examples' },
];

const fieldStyle = {
  background: 'var(--color-bg-secondary)',
  color: 'var(--color-text)',
  border: '1px solid var(--color-border)',
};

const PERSONAS: Array<{ key: Persona; label: string; hint: string; icon: typeof Crown }> = [
  { key: 'professional', label: 'Professional', hint: 'Clear, neutral and to the point', icon: Briefcase },
  { key: 'jarvis', label: 'AI butler (JARVIS-style)', hint: 'Composed, British, dry wit — calls you "sir"', icon: Crown },
];

function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      onClick={() => onChange(!on)}
      className="relative w-10 h-6 rounded-full transition-colors cursor-pointer shrink-0"
      style={{ background: on ? 'var(--color-accent)' : 'var(--color-bg-tertiary, var(--color-border))' }}
    >
      <span className="absolute top-0.5 w-5 h-5 rounded-full bg-white transition-all" style={{ left: on ? '18px' : '2px' }} />
    </button>
  );
}

/** Personality (professional / JARVIS-style butler) and voice. */
function PersonaAndVoice() {
  const settings = useAppStore((s) => s.settings);
  const updateSettings = useAppStore((s) => s.updateSettings);
  const ttsAvailable = useTtsStore((s) => s.available);
  const ensureHealth = useTtsStore((s) => s.ensureHealth);
  const [honorific, setHonorific] = useState(settings.honorific);

  useEffect(() => { void ensureHealth(); }, [ensureHealth]);
  useEffect(() => {
    if (honorific === settings.honorific) return;
    const t = setTimeout(() => updateSettings({ honorific }), 400);
    return () => clearTimeout(t);
  }, [honorific, settings.honorific, updateSettings]);

  const voiceOn = settings.voiceOutputEnabled && settings.voiceAutoplay;

  return (
    <div className="rounded-xl p-5" style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}>
      <h3 className="text-sm font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
        <Crown size={15} /> Personality & voice
      </h3>
      <p className="text-xs mb-4" style={{ color: 'var(--color-text-tertiary)' }}>
        Choose how Ghost talks to you, and let it speak its replies.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-4" role="radiogroup" aria-label="Personality">
        {PERSONAS.map((p) => {
          const active = settings.persona === p.key;
          const Icon = p.icon;
          return (
            <button
              key={p.key}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => updateSettings({ persona: p.key })}
              className="flex items-start gap-3 text-left px-3 py-3 rounded-lg cursor-pointer transition-colors"
              style={{
                background: active ? 'var(--color-accent-subtle)' : 'var(--color-bg-secondary)',
                border: `1px solid ${active ? 'var(--color-accent)' : 'var(--color-border)'}`,
              }}
            >
              <Icon size={16} className="mt-0.5 shrink-0" style={{ color: active ? 'var(--color-accent)' : 'var(--color-text-tertiary)' }} />
              <span>
                <span className="block text-sm font-medium" style={{ color: active ? 'var(--color-accent)' : 'var(--color-text)' }}>{p.label}</span>
                <span className="block text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{p.hint}</span>
              </span>
            </button>
          );
        })}
      </div>

      {settings.persona === 'jarvis' && (
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 py-3" style={{ borderTop: '1px solid var(--color-border-subtle)' }}>
          <div>
            <div className="text-sm" style={{ color: 'var(--color-text)' }}>How should Ghost address you?</div>
            <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>e.g. sir, ma'am, boss, Mr. Shah</div>
          </div>
          <input
            value={honorific}
            onChange={(e) => setHonorific(e.target.value)}
            maxLength={40}
            placeholder="sir"
            aria-label="Honorific"
            className="text-sm px-3 py-1.5 rounded-lg outline-none w-56"
            style={fieldStyle}
          />
        </div>
      )}

      <div className="flex items-center justify-between gap-3 py-3" style={{ borderTop: '1px solid var(--color-border-subtle)' }}>
        <div>
          <div className="text-sm flex items-center gap-1.5" style={{ color: 'var(--color-text)' }}><Volume2 size={14} /> Speak replies aloud</div>
          <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {ttsAvailable === false ? 'No voice is available on this device.' : 'Ghost reads each new reply out loud.'}
          </div>
        </div>
        <Toggle
          on={voiceOn}
          label="Speak replies aloud"
          onChange={(v) => updateSettings({ voiceOutputEnabled: v, voiceAutoplay: v })}
        />
      </div>

      <div className="flex items-center justify-between gap-3 py-3" style={{ borderTop: '1px solid var(--color-border-subtle)' }}>
        <div>
          <div className="text-sm" style={{ color: 'var(--color-text)' }}>Greet me when Ghost opens</div>
          <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {settings.persona === 'jarvis' ? `"${butlerGreeting(settings.honorific)} All systems are online."` : 'A short spoken greeting at startup.'}
          </div>
        </div>
        <Toggle
          on={settings.greetOnStartup}
          label="Greet me when Ghost opens"
          onChange={(v) => updateSettings({ greetOnStartup: v, ...(v ? { voiceOutputEnabled: true } : {}) })}
        />
      </div>
    </div>
  );
}

/** Settings → Assistant: response style, what Ghost knows about you, custom instructions. */
export function AssistantSection() {
  const settings = useAppStore((s) => s.settings);
  const updateSettings = useAppStore((s) => s.updateSettings);
  const [about, setAbout] = useState(settings.aboutUser);
  const [custom, setCustom] = useState(settings.customInstructions);
  const [savedAt, setSavedAt] = useState(0);

  // Save typing after a short pause.
  useEffect(() => {
    if (about === settings.aboutUser && custom === settings.customInstructions) return;
    const t = setTimeout(() => {
      updateSettings({ aboutUser: about, customInstructions: custom });
      setSavedAt(Date.now());
    }, 500);
    return () => clearTimeout(t);
  }, [about, custom, settings.aboutUser, settings.customInstructions, updateSettings]);

  return (
    <>
    <PersonaAndVoice />
    <VoiceEngineSection />
    <div className="rounded-xl p-5" style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}>
      <h3 className="text-sm font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
        <Sparkles size={15} /> Personalise Ghost
      </h3>
      <p className="text-xs mb-4" style={{ color: 'var(--color-text-tertiary)' }}>
        Applies to new messages with every model, local or cloud. Stored only on this device.
      </p>

      <div className="text-sm mb-2" style={{ color: 'var(--color-text)' }}>Response style</div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 mb-5" role="radiogroup" aria-label="Response style">
        {STYLES.map((st) => {
          const active = settings.responseStyle === st.key;
          return (
            <button
              key={st.key}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => updateSettings({ responseStyle: st.key })}
              className="text-left px-3 py-2.5 rounded-lg cursor-pointer transition-colors"
              style={{
                background: active ? 'var(--color-accent-subtle)' : 'var(--color-bg-secondary)',
                border: `1px solid ${active ? 'var(--color-accent)' : 'var(--color-border)'}`,
              }}
            >
              <div className="text-sm font-medium" style={{ color: active ? 'var(--color-accent)' : 'var(--color-text)' }}>{st.label}</div>
              <div className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{st.hint}</div>
            </button>
          );
        })}
      </div>

      <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text)' }} htmlFor="about-user">
        What should Ghost know about you?
      </label>
      <textarea
        id="about-user"
        value={about}
        onChange={(e) => setAbout(e.target.value)}
        maxLength={2000}
        rows={3}
        placeholder="e.g. I'm a software developer in Lahore. I work with Python and React."
        className="w-full text-sm px-3 py-2 rounded-lg outline-none mb-4 resize-y"
        style={fieldStyle}
      />

      <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text)' }} htmlFor="custom-instructions">
        How should Ghost respond?
      </label>
      <textarea
        id="custom-instructions"
        value={custom}
        onChange={(e) => setCustom(e.target.value)}
        maxLength={2000}
        rows={3}
        placeholder="e.g. Reply in English and Urdu. Use bullet points. Explain code step by step."
        className="w-full text-sm px-3 py-2 rounded-lg outline-none resize-y"
        style={fieldStyle}
      />
      <div className="text-xs mt-1.5 h-4" style={{ color: 'var(--color-success)' }}>
        {savedAt > 0 && 'Saved'}
      </div>
    </div>
    </>
  );
}
