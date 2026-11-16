import { useEffect, useRef, useState } from 'react';
import { Loader2, X } from 'lucide-react';
import { useAppStore } from '../../lib/store';
import {
  RealtimeCall,
  pauseWakeWord,
  startRealtimeEngine,
  type CallState,
} from '../../lib/realtime-voice';

/** Messages the engine sends as RTVI server messages (ghost-error, ghost-idle). */
interface EngineMessage { type?: string; data?: { type?: string; message?: string; code?: string } }

function engineNotice(msg: unknown): { error?: string; idle?: boolean } {
  const m = msg as EngineMessage;
  const data = m?.type === 'server-message' ? m.data : (m as EngineMessage['data']);
  if (!data || typeof data !== 'object') return {};
  if (data.type === 'ghost-error') return { error: data.message || data.code || 'Voice engine error' };
  if (data.type === 'ghost-idle') return { idle: true };
  return {};
}

/** Voice Mode on the realtime engine (Pipecat, Silero VAD, Smart Turn v3). */
export function RealtimeVoiceMode({ onClose }: { onClose: () => void }) {
  const settings = useAppStore((s) => s.settings);
  const [state, setState] = useState<CallState>('idle');
  const [error, setError] = useState('');
  const call = useRef<RealtimeCall | null>(null);
  const urdu = settings.replyLanguage === 'urdu';

  useEffect(() => {
    let live = true;
    const c = new RealtimeCall();
    call.current = c;
    c.onState = (s, detail) => {
      if (!live) return;
      setState(s);
      if (detail) setError(detail);
    };
    c.onServerMessage = (msg) => {
      const notice = engineNotice(msg);
      if (notice.error && live) setError(notice.error);
      if (notice.idle && live) onClose();
    };
    // The wake word shares the microphone; hold it for the length of the call.
    void pauseWakeWord(false).catch(() => {});
    void (async () => {
      try {
        await startRealtimeEngine({
          brain: settings.realtimeBrain,
          voiceMode: settings.realtimeVoiceMode,
          // Companion → Stonic's female_soulmate (Aoede); Jarvis → male_jarvis (Charon).
          character: settings.persona === 'jarvis' ? { id: 'male_jarvis', gender: 'male' }
            : settings.persona === 'companion' ? { id: 'female_soulmate', gender: 'female' } : {},
        });
        if (live) await c.connect();
      } catch (e) {
        if (live) { setState('error'); setError(e instanceof Error ? e.message : String(e)); }
      }
    })();
    return () => {
      live = false;
      c.close();
      void pauseWakeWord(true).catch(() => {});
    };
    // One call per mount; settings changes apply to the next call.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const label = {
    idle: urdu ? 'تیار ہو رہا ہے…' : 'Getting ready…',
    connecting: urdu ? 'جڑ رہا ہے…' : 'Connecting to the realtime engine…',
    connected: urdu ? 'سن رہا ہوں — بولیے' : 'Listening — just talk',
    ended: urdu ? 'کال ختم' : 'Call ended',
    error: urdu ? 'آواز کا انجن دستیاب نہیں' : 'Realtime voice is unavailable',
  }[state];

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col items-center justify-center px-6"
      style={{ background: 'color-mix(in srgb, var(--color-bg) 94%, transparent)', backdropFilter: 'blur(8px)' }}
      role="dialog"
      aria-label="Voice mode (realtime)"
    >
      <button
        onClick={onClose}
        className="absolute top-5 right-5 p-2 rounded-full cursor-pointer"
        style={{ color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
        aria-label="End voice mode"
      >
        <X size={18} />
      </button>

      <div className={`butler-core ${state === 'connecting' ? 'voice-thinking' : ''}`} style={{ width: 180, height: 180 }}>
        <span className="butler-ring butler-ring-outer" />
        <span className="butler-ring butler-ring-mid" />
        <span className="butler-ring butler-ring-inner" />
        <span className="butler-glow" />
      </div>

      <div dir="auto" className="mt-10 text-lg font-medium text-center inline-flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
        {(state === 'idle' || state === 'connecting') && <Loader2 size={16} className="animate-spin" />}
        {label}
      </div>
      <p className="text-xs mt-2" style={{ color: 'var(--color-text-tertiary)' }}>Realtime engine</p>
      {error && <p role="alert" className="mt-4 text-xs text-center max-w-md" style={{ color: 'var(--color-error)' }}>{error}</p>}

      <p className="absolute bottom-6 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
        {urdu ? 'ختم کرنے کے لیے Esc دبائیں' : 'Press Esc to end'}
      </p>
    </div>
  );
}
