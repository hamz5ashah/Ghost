import { useEffect, useState } from 'react';
import { Sparkles } from 'lucide-react';
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
  );
}
