import { useCallback, useEffect, useRef, useState } from 'react';
import { transcribeAudio } from '../lib/api';
import { useAppStore } from '../lib/store';
import { useTtsStore } from '../lib/tts';
import { stripThinkTags } from '../lib/message-text';
import { VOICE_SEND_EVENT, VoiceActivityDetector, sttLanguageFor } from '../lib/voice-activity';

export type ConversationPhase = 'starting' | 'listening' | 'hearing' | 'thinking' | 'speaking' | 'error';

/** Whisper size for voice commands: "small" handles Urdu; "base" is faster for English. */
export function sttModelFor(replyLanguage: string): string {
  return replyLanguage === 'english' ? 'base' : 'small';
}

const MAX_LISTEN_MS = 20_000; // recycle the recording if nobody speaks
const MAX_UTTERANCE_MS = 45_000; // cut very long monologues

/**
 * Hands-free conversation: listen → detect the end of what the user said →
 * transcribe → send to the chat → speak the reply → listen again. Speaking
 * while Ghost talks interrupts it ("barge-in").
 */
export function useVoiceConversation(active: boolean) {
  const [phase, setPhase] = useState<ConversationPhase>('starting');
  const [level, setLevel] = useState(0);
  const [heard, setHeard] = useState('');
  const [error, setError] = useState('');

  const phaseRef = useRef<ConversationPhase>('starting');
  const go = useCallback((p: ConversationPhase) => { phaseRef.current = p; setPhase(p); }, []);

  const streamRef = useRef<MediaStream | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const recStartRef = useRef(0);
  const rafRef = useRef(0);
  const alive = useRef(false);

  const startRecorder = useCallback(() => {
    const stream = streamRef.current;
    if (!stream) return;
    try { recorderRef.current?.stop(); } catch { /* not recording */ }
    const rec = new MediaRecorder(stream);
    chunksRef.current = [];
    rec.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data); };
    rec.start(250);
    recorderRef.current = rec;
    recStartRef.current = performance.now();
  }, []);

  const finishUtterance = useCallback(async () => {
    const rec = recorderRef.current;
    if (!rec) return;
    go('thinking');
    const stopped = new Promise<void>((resolve) => { rec.onstop = () => resolve(); });
    try { rec.stop(); } catch { /* already stopped */ }
    await stopped;
    const blob = new Blob(chunksRef.current, { type: rec.mimeType || 'audio/webm' });
    chunksRef.current = [];
    if (!alive.current) return;

    const { replyLanguage } = useAppStore.getState().settings;
    try {
      const result = await transcribeAudio(blob, 'voice.webm', {
        language: sttLanguageFor(replyLanguage),
        model: sttModelFor(replyLanguage),
      });
      const text = (result.text || '').trim();
      if (!alive.current) return;
      if (!text) { go('listening'); startRecorder(); return; }
      setHeard(text);
      // Hand the words to the normal chat send path; the reply is spoken
      // when the stream finishes (see the store subscription below).
      window.dispatchEvent(new CustomEvent(VOICE_SEND_EVENT, { detail: text }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      go('listening');
      startRecorder();
    }
  }, [go, startRecorder]);

  // Microphone + level meter + voice activity detection.
  useEffect(() => {
    if (!active) return;
    alive.current = true;
    setError('');
    setHeard('');
    go('starting');
    const vad = new VoiceActivityDetector();
    const bargeIn = new VoiceActivityDetector({ threshold: 0.06, minSpeechMs: 300 });
    let shownLevel = 0;

    (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        if (!alive.current) { stream.getTracks().forEach((t) => t.stop()); return; }
        streamRef.current = stream;
        const ctx = new AudioContext();
        ctxRef.current = ctx;
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        ctx.createMediaStreamSource(stream).connect(analyser);
        const buf = new Float32Array(analyser.fftSize);
        useTtsStore.getState().setConversationActive(true);
        go('listening');
        startRecorder();

        const tick = () => {
          if (!alive.current) return;
          analyser.getFloatTimeDomainData(buf);
          let sum = 0;
          for (let i = 0; i < buf.length; i += 1) sum += buf[i] * buf[i];
          const rms = Math.sqrt(sum / buf.length);
          const now = performance.now();
          shownLevel = shownLevel * 0.7 + Math.min(1, rms * 8) * 0.3;
          setLevel(shownLevel);

          const p = phaseRef.current;
          if (p === 'listening' || p === 'hearing') {
            const ev = vad.update(rms, now);
            if (ev === 'speech-start') go('hearing');
            if (ev === 'speech-end' || (p === 'hearing' && now - recStartRef.current > MAX_UTTERANCE_MS)) {
              vad.reset();
              void finishUtterance();
            } else if (p === 'listening' && now - recStartRef.current > MAX_LISTEN_MS) {
              startRecorder(); // drop the silent recording, keep listening
            }
          } else if (p === 'speaking') {
            if (bargeIn.update(rms, now) === 'speech-start') {
              // The user started talking over Ghost: stop and listen.
              bargeIn.reset();
              useTtsStore.getState().stop();
              vad.reset();
              go('hearing');
              startRecorder();
              vad.markSpeaking(now); // already mid-sentence
            }
          }
          rafRef.current = requestAnimationFrame(tick);
        };
        rafRef.current = requestAnimationFrame(tick);
      } catch {
        setError('Microphone access was blocked. Allow microphone access for Ghost and try again.');
        go('error');
      }
    })();

    return () => {
      alive.current = false;
      cancelAnimationFrame(rafRef.current);
      try { recorderRef.current?.stop(); } catch { /* ignore */ }
      recorderRef.current = null;
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      void ctxRef.current?.close().catch(() => {});
      ctxRef.current = null;
      useTtsStore.getState().stop();
      useTtsStore.getState().setConversationActive(false);
    };
  }, [active, go, startRecorder, finishUtterance]);

  // When the reply finishes streaming, speak it; when speech ends, listen again.
  useEffect(() => {
    if (!active) return;
    let wasStreaming = useAppStore.getState().streamState.isStreaming;
    const unsubApp = useAppStore.subscribe((s) => {
      const streaming = s.streamState.isStreaming;
      if (wasStreaming && !streaming && phaseRef.current === 'thinking') {
        const last = s.messages[s.messages.length - 1];
        const text = last && last.role === 'assistant' ? stripThinkTags(last.content) : '';
        if (text) {
          const tts = useTtsStore.getState();
          tts.markAutoSpoken(last.id);
          go('speaking');
          void tts.speak(last.id, text).then(() => {
            // speak() resolves when playback starts; if nothing could play,
            // go straight back to listening.
            if (useTtsStore.getState().state === 'idle' && phaseRef.current === 'speaking') {
              go('listening');
              startRecorder();
            }
          });
        } else {
          go('listening');
          startRecorder();
        }
      }
      wasStreaming = streaming;
    });
    const unsubTts = useTtsStore.subscribe((t, prev) => {
      if (prev.state !== 'idle' && t.state === 'idle' && phaseRef.current === 'speaking') {
        go('listening');
        startRecorder();
      }
    });
    return () => { unsubApp(); unsubTts(); };
  }, [active, go, startRecorder]);

  /** Tap the orb: interrupt Ghost, or finish talking now. */
  const tap = useCallback(() => {
    const p = phaseRef.current;
    if (p === 'speaking') {
      useTtsStore.getState().stop();
    } else if (p === 'hearing') {
      void finishUtterance();
    }
  }, [finishUtterance]);

  return { phase, level, heard, error, tap };
}
