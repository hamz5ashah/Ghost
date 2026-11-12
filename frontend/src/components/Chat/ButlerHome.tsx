import { useEffect, useState } from 'react';
import { useAppStore } from '../../lib/store';
import { butlerGreeting } from '../../lib/assistant-prompt';
import { useTtsStore } from '../../lib/tts';

/**
 * Start screen for the butler (JARVIS-style) persona: an animated core, a
 * time-of-day greeting with the user's honorific, a live clock and the model
 * status. Optionally greets the user aloud once per app launch.
 */
export function ButlerHome() {
  const honorific = useAppStore((s) => s.settings.honorific);
  const greetOnStartup = useAppStore((s) => s.settings.greetOnStartup);
  const voiceOutputEnabled = useAppStore((s) => s.settings.voiceOutputEnabled);
  const selectedModel = useAppStore((s) => s.selectedModel);
  const modelsLoading = useAppStore((s) => s.modelsLoading);
  const ttsAvailable = useTtsStore((s) => s.available);
  const speak = useTtsStore((s) => s.speak);
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(t);
  }, []);

  const greeting = butlerGreeting(honorific, now);
  const online = !!selectedModel;

  // Spoken greeting, once per launch.
  useEffect(() => {
    // Wait for the model list so the greeting reports the real status.
    if (!greetOnStartup || !voiceOutputEnabled || ttsAvailable !== true || modelsLoading) return;
    try {
      if (sessionStorage.getItem('ghost-greeted')) return;
      sessionStorage.setItem('ghost-greeted', '1');
    } catch { /* ignore */ }
    const line = online
      ? `${butlerGreeting(honorific)} All systems are online. How may I assist you?`
      : `${butlerGreeting(honorific)} I'm awaiting a model. You'll find the options in Settings.`;
    void speak('ghost-greeting', line);
  }, [greetOnStartup, voiceOutputEnabled, ttsAvailable, modelsLoading, online, honorific, speak]);

  return (
    <div className="flex flex-col items-center text-center">
      <div className="butler-core mb-7" aria-hidden="true">
        <span className="butler-ring butler-ring-outer" />
        <span className="butler-ring butler-ring-mid" />
        <span className="butler-ring butler-ring-inner" />
        <span className="butler-glow" />
      </div>
      <h2 className="text-2xl font-semibold mb-2 tracking-tight" style={{ color: 'var(--color-text)' }}>
        {greeting}
      </h2>
      <p className="text-sm max-w-md mb-4" style={{ color: 'var(--color-text-secondary)' }}>
        {online ? 'All systems online. How may I assist you today?' : 'Standing by. Set up a model in Settings to begin.'}
      </p>
      <div className="flex flex-wrap justify-center gap-2 text-[11px] font-mono uppercase tracking-wider" style={{ color: 'var(--color-text-tertiary)' }}>
        <span className="px-2.5 py-1 rounded-full" style={{ border: '1px solid var(--color-border)' }}>
          {now.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })} · {now.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
        </span>
        <span className="px-2.5 py-1 rounded-full flex items-center gap-1.5" style={{ border: '1px solid var(--color-border)' }}>
          <span className="w-1.5 h-1.5 rounded-full" style={{ background: online ? 'var(--color-success)' : 'var(--color-text-tertiary)' }} />
          {online ? selectedModel : 'No model'}
        </span>
      </div>
    </div>
  );
}
