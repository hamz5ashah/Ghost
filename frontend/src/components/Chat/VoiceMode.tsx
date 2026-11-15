import { useEffect, useState } from 'react';
import { X, Loader2 } from 'lucide-react';
import { useAppStore } from '../../lib/store';
import { fetchSttStatus, prepareSttModel } from '../../lib/api';
import { stripThinkTags } from '../../lib/message-text';
import { sttModelFor, useVoiceConversation, type ConversationPhase } from '../../hooks/useVoiceConversation';
import { getStonicFlags } from '../../lib/stonic-flags';
import { effectiveConversationEngine } from '../../lib/realtime-voice';
import { RealtimeVoiceMode } from './RealtimeVoiceMode';

const LABELS: Record<'en' | 'ur', Record<ConversationPhase, string>> = {
  en: {
    starting: 'Starting the microphone…',
    listening: 'Listening — just talk',
    hearing: 'Listening…',
    thinking: 'Thinking…',
    speaking: 'Speaking — talk to interrupt',
    error: 'Microphone unavailable',
  },
  ur: {
    starting: 'مائیکروفون شروع ہو رہا ہے…',
    listening: 'سن رہا ہوں — بولیے',
    hearing: 'سن رہا ہوں…',
    thinking: 'سوچ رہا ہوں…',
    speaking: 'بول رہا ہوں — روکنے کے لیے بولیے',
    error: 'مائیکروفون دستیاب نہیں',
  },
};

/** Speech recognition must be downloaded once before talking. */
function useSttReady(model: string) {
  const [state, setState] = useState<'checking' | 'downloading' | 'ready' | 'error' | 'unavailable'>('checking');
  const [error, setError] = useState('');
  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async (started: boolean) => {
      try {
        const s = await fetchSttStatus(model);
        if (stop) return;
        if (s.state === 'ready') return setState('ready');
        if (s.state === 'unavailable') return setState('unavailable');
        if (s.state === 'error') { setError(s.error || ''); return setState('error'); }
        if (!started && s.state === 'missing') await prepareSttModel(model);
        setState('downloading');
        timer = setTimeout(() => void poll(true), 2000);
      } catch (e) {
        if (!stop) { setError(e instanceof Error ? e.message : String(e)); setState('error'); }
      }
    };
    void poll(false);
    return () => { stop = true; clearTimeout(timer); };
  }, [model]);
  return { state, error };
}

/** Ghost's default engine: Kokoro speaks, faster-whisper listens. */
function GhostVoiceMode({ onClose }: { onClose: () => void }) {
  const replyLanguage = useAppStore((s) => s.settings.replyLanguage);
  const streamContent = useAppStore((s) => s.streamState.content);
  const lastReply = useAppStore((s) => {
    const last = s.messages[s.messages.length - 1];
    return last && last.role === 'assistant' ? last.content : '';
  });
  const urdu = replyLanguage === 'urdu';
  const stt = useSttReady(sttModelFor(replyLanguage));
  const { phase, level, heard, error, tap } = useVoiceConversation(stt.state === 'ready');

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const labels = LABELS[urdu ? 'ur' : 'en'];
  const status = stt.state === 'ready' ? labels[phase] : null;
  const reply = stripThinkTags(phase === 'thinking' && streamContent ? streamContent : lastReply);
  const scale = 1 + (phase === 'hearing' || phase === 'listening' ? level * 0.6 : phase === 'speaking' ? 0.08 : 0);

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col items-center justify-center px-6"
      style={{ background: 'color-mix(in srgb, var(--color-bg) 94%, transparent)', backdropFilter: 'blur(8px)' }}
      role="dialog"
      aria-label="Voice mode"
    >
      <button
        onClick={onClose}
        className="absolute top-5 right-5 p-2 rounded-full cursor-pointer"
        style={{ color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
        aria-label="End voice mode"
      >
        <X size={18} />
      </button>

      <button
        type="button"
        onClick={tap}
        aria-label={phase === 'speaking' ? 'Interrupt' : 'Finish speaking'}
        className={`butler-core cursor-pointer ${phase === 'thinking' ? 'voice-thinking' : ''}`}
        style={{ transform: `scale(${scale})`, transition: 'transform 80ms linear', width: 180, height: 180 }}
      >
        <span className="butler-ring butler-ring-outer" />
        <span className="butler-ring butler-ring-mid" />
        <span className="butler-ring butler-ring-inner" />
        <span className="butler-glow" />
      </button>

      <div dir="auto" className="mt-10 text-lg font-medium text-center" style={{ color: 'var(--color-text)' }}>
        {status ?? (
          stt.state === 'downloading' || stt.state === 'checking' ? (
            <span className="inline-flex items-center gap-2">
              <Loader2 size={16} className="animate-spin" />
              {urdu ? 'آواز پہچاننے کا نظام تیار ہو رہا ہے…' : 'Setting up speech recognition…'}
            </span>
          ) : stt.state === 'unavailable'
            ? 'Speech recognition is not available. Start a model in Settings → AI Model first.'
            : `Speech recognition failed to load${stt.error ? `: ${stt.error}` : ''}`
        )}
      </div>
      {stt.state === 'downloading' && (
        <p className="text-xs mt-2 text-center max-w-md" style={{ color: 'var(--color-text-tertiary)' }}>
          The first time, Ghost downloads its speech-recognition model (about 480 MB). After that it works offline.
        </p>
      )}

      {heard && (
        <p dir="auto" className="mt-6 text-sm text-center max-w-xl" style={{ color: 'var(--color-text-secondary)' }}>
          “{heard}”
        </p>
      )}
      {reply && (phase === 'thinking' || phase === 'speaking') && (
        <p dir="auto" className="mt-3 text-sm text-center max-w-xl line-clamp-4" style={{ color: 'var(--color-text-tertiary)' }}>
          {reply}
        </p>
      )}
      {error && <p role="alert" className="mt-4 text-xs text-center" style={{ color: 'var(--color-error)' }}>{error}</p>}

      <p className="absolute bottom-6 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
        {urdu ? 'ختم کرنے کے لیے Esc دبائیں' : 'Press Esc to end'}
      </p>
    </div>
  );
}

/**
 * Full-screen hands-free conversation with Ghost.
 *
 * Kokoro + faster-whisper is the default and renders straight away. The
 * realtime engine is used only when the user picked it in Settings → Assistant
 * and the `realtime_voice` flag is on; if the flag turns out to be off, the
 * default engine is used instead.
 */
export function VoiceMode({ onClose }: { onClose: () => void }) {
  const chosen = useAppStore((s) => s.settings.conversationEngine);
  const [engine, setEngine] = useState<'ghost' | 'realtime' | 'pending'>(
    chosen === 'realtime' ? 'pending' : 'ghost',
  );
  useEffect(() => {
    if (chosen !== 'realtime') { setEngine('ghost'); return; }
    let live = true;
    void getStonicFlags().then((flags) => {
      if (live) setEngine(effectiveConversationEngine(chosen, flags));
    });
    return () => { live = false; };
  }, [chosen]);

  if (engine === 'pending') {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center" role="dialog" aria-label="Voice mode"
        style={{ background: 'color-mix(in srgb, var(--color-bg) 94%, transparent)' }}>
        <Loader2 size={20} className="animate-spin" style={{ color: 'var(--color-text-secondary)' }} />
      </div>
    );
  }
  return engine === 'realtime' ? <RealtimeVoiceMode onClose={onClose} /> : <GhostVoiceMode onClose={onClose} />;
}
