import { create } from 'zustand';
import { synthesizeSpeech, fetchTtsHealth } from './api';
import { toSpeakableText } from './message-text';

export type TtsState = 'idle' | 'loading' | 'speaking';

/**
 * Voice output is a single shared resource: one utterance at a time, for the
 * whole app. The state lives in one store rather than per component, because a
 * per-component hook would give every message its own audio element -- two
 * replies would then talk over each other, and autoplay would make that the
 * normal case rather than the exception.
 */
interface TtsStore {
  state: TtsState;
  /** id of the message currently loading or speaking, if any. */
  speakingId: string | null;
  error: string | null;
  /** Message whose read-aloud attempt failed, if any. */
  errorId: string | null;
  /** null until the health probe has answered. */
  available: boolean | null;
  /** Last message spoken by autoplay, so a re-render never repeats it. */
  autoSpokenId: string | null;
  speak: (id: string, text: string) => Promise<void>;
  stop: () => void;
  ensureHealth: () => Promise<void>;
  markAutoSpoken: (id: string) => void;
  /** Voice mode is running: it speaks replies itself (no autoplay). */
  conversationActive: boolean;
  setConversationActive: (active: boolean) => void;
}

// Playback handles are not render state -- keeping them out of the store avoids
// re-rendering every subscriber when an audio element is swapped.
let audio: HTMLAudioElement | null = null;
let objectUrl: string | null = null;
let controller: AbortController | null = null;
let token = 0;
let healthProbe: Promise<void> | null = null;
/** Speak with the system's built-in voices when the server has no TTS engine. */
let useBrowserVoice = false;

const browserSpeech = (): SpeechSynthesis | null =>
  typeof window !== 'undefined' && 'speechSynthesis' in window ? window.speechSynthesis : null;

/** Installed system voices (may be empty until the browser loads them). */
export function listSystemVoices(): SpeechSynthesisVoice[] {
  return browserSpeech()?.getVoices() ?? [];
}

/**
 * The voice to use: the one the user picked, else (for the JARVIS persona) a
 * British male voice such as "Microsoft George/Ryan" or "Daniel", else any
 * English voice.
 */
export function pickSystemVoice(preferredName: string, persona: string): SpeechSynthesisVoice | null {
  const voices = listSystemVoices();
  if (!voices.length) return null;
  const byName = voices.find((v) => v.name === preferredName);
  if (byName) return byName;
  if (voicePrefs.language === 'urdu') {
    const urdu = voices.find((v) => /^ur/i.test(v.lang)) ?? voices.find((v) => /^hi/i.test(v.lang));
    if (urdu) return urdu;
  }
  if (persona === 'jarvis') {
    const british = voices.filter((v) => /en[-_]GB/i.test(v.lang));
    const male = british.find((v) => /george|ryan|daniel|thomas|arthur|oliver|male/i.test(v.name));
    if (male) return male;
    if (british[0]) return british[0];
  }
  return voices.find((v) => /^en/i.test(v.lang)) ?? voices[0];
}

/**
 * Voice engine chosen in Settings → Assistant → Voice:
 *  - 'system': the computer's own voices (instant, always available)
 *  - 'kokoro_onnx': Ghost's offline natural voice (downloaded once)
 *  - 'elevenlabs': ElevenLabs, online, with the user's API key
 *  - 'auto': the server's voice if it has one, otherwise the system voice
 */
export type VoiceEngine = 'auto' | 'system' | 'kokoro_onnx' | 'elevenlabs';

interface VoicePrefs {
  voiceName: string;
  persona: string;
  engine?: VoiceEngine;
  kokoroVoice?: string;
  elevenVoice?: string;
  language?: string;
}

/** Voice preferences, read at speak time (set from Settings → Assistant). */
let voicePrefs: VoicePrefs = { voiceName: '', persona: 'professional' };
export function setVoicePreferences(prefs: VoicePrefs): void {
  voicePrefs = prefs;
}

/**
 * Split text into speakable pieces (sentences, merged up to ~maxLen chars) so
 * playback can start after the first piece instead of the whole reply.
 */
export function splitForSpeech(text: string, maxLen = 260): string[] {
  // Includes the Urdu full stop "۔" and question mark "؟".
  const sentences = text.replace(/\s+/g, ' ').trim().match(/[^.!?…۔؟]+[.!?…۔؟]+["')\]]*\s*|[^.!?…۔؟]+$/g) ?? [];
  const chunks: string[] = [];
  let current = '';
  for (const raw of sentences) {
    const s = raw.trim();
    if (!s) continue;
    if (current && (current + ' ' + s).length > maxLen) {
      chunks.push(current);
      current = s;
    } else {
      current = current ? `${current} ${s}` : s;
    }
  }
  if (current) chunks.push(current);
  return chunks;
}

/** Only a stream ending in the active conversation may trigger autoplay. */
export function shouldAutoplayFinishedReply(
  previousStreamingConversationId: string | null,
  activeId: string | null,
  streamIsActive: boolean,
  lastMessage: { id: string; role: string } | undefined,
  autoSpokenId: string | null,
): boolean {
  return previousStreamingConversationId !== null
    && previousStreamingConversationId === activeId
    && !streamIsActive
    && lastMessage !== undefined
    && lastMessage.role === 'assistant'
    && lastMessage.id !== autoSpokenId;
}

function teardown(): void {
  browserSpeech()?.cancel();
  if (audio) {
    // Detach first: clearing src re-runs the media load algorithm, which fails
    // on an empty source and dispatches an `error` event. With the handler
    // still attached that surfaces as a bogus "Playback failed" after every
    // successful utterance.
    audio.onended = null;
    audio.onerror = null;
    audio.pause();
    audio.src = '';
    audio = null;
  }
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }
  if (controller) {
    controller.abort();
    controller = null;
  }
}

export const useTtsStore = create<TtsStore>((set, get) => ({
  state: 'idle',
  speakingId: null,
  error: null,
  errorId: null,
  available: null,
  autoSpokenId: null,

  ensureHealth: () => {
    if (healthProbe) return healthProbe;
    // Prefer the server's voice engine; fall back to the system's voices.
    healthProbe = fetchTtsHealth()
      .then((health) => {
        useBrowserVoice = !health.available && !!browserSpeech();
        set({ available: health.available || useBrowserVoice });
        // Ask the server again next time: its voice engine may come up later.
        if (!health.available) healthProbe = null;
      })
      .catch(() => {
        useBrowserVoice = !!browserSpeech();
        set({ available: useBrowserVoice });
        healthProbe = null;
      });
    return healthProbe;
  },

  markAutoSpoken: (id: string) => set({ autoSpokenId: id }),
  conversationActive: false,
  setConversationActive: (active: boolean) => set({ conversationActive: active }),

  speak: async (id: string, text: string) => {
    const trimmed = toSpeakableText(text);
    if (!trimmed) return;

    // Bump before teardown so a synthesis still in flight is both aborted and
    // fenced off by the token, even if the abort loses the race.
    token += 1;
    const mine = token;
    teardown();
    set({ state: 'loading', speakingId: id, error: null, errorId: null });

    const engine = voicePrefs.engine ?? 'auto';
    const serverEngine = engine === 'kokoro_onnx' || engine === 'elevenlabs' ? engine : undefined;
    const viaSystem = engine === 'system' || (engine === 'auto' && useBrowserVoice);

    if (!viaSystem) {
      const voiceId = serverEngine === 'kokoro_onnx'
        ? voicePrefs.kokoroVoice
        : serverEngine === 'elevenlabs' ? voicePrefs.elevenVoice : undefined;
      const result = await speakViaServer(trimmed, id, mine, serverEngine, voiceId || undefined, set);
      if (result.ok || mine !== token) return;
      // Server voice failed (engine not ready, offline, no key): fall back to
      // the system voice, or report the failure if there is none.
      if (!browserSpeech()) {
        set({ state: 'idle', speakingId: null, error: result.error ?? 'Speech synthesis failed', errorId: id });
        return;
      }
    }

    {
      const synth = browserSpeech();
      if (!synth) return;
      const utterance = new SpeechSynthesisUtterance(trimmed);
      const voice = pickSystemVoice(voicePrefs.voiceName, voicePrefs.persona);
      if (voice) { utterance.voice = voice; utterance.lang = voice.lang; }
      if (voicePrefs.persona === 'jarvis') { utterance.rate = 1.02; utterance.pitch = 0.9; }
      utterance.onstart = () => { if (mine === token) set({ state: 'speaking', speakingId: id }); };
      utterance.onend = () => { if (mine === token) set({ state: 'idle', speakingId: null }); };
      utterance.onerror = (e) => {
        if (mine !== token || e.error === 'canceled' || e.error === 'interrupted') return;
        set({ state: 'idle', speakingId: null, error: 'Speech failed', errorId: id });
      };
      synth.speak(utterance);
      return;
    }
  },

  stop: () => {
    token += 1;
    teardown();
    set({ state: 'idle', speakingId: null, error: null, errorId: null });
  },
}));

type SetState = (partial: Partial<TtsStore>) => void;

/** Play one audio blob; resolves when it ends (or rejects on failure). */
function playBlob(blob: Blob, mine: number, onEnded?: () => void): Promise<void> {
  return new Promise((resolve, reject) => {
    if (mine !== token) return resolve();
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    const url = URL.createObjectURL(blob);
    objectUrl = url;
    const el = new Audio(url);
    audio = el;
    el.onended = () => { onEnded?.(); resolve(); };
    el.onerror = () => reject(new Error('Playback failed'));
    el.play().catch(reject);
  });
}

/**
 * Speak with the server's voice engine, sentence by sentence: the next piece
 * is synthesized while the current one plays. Resolves as soon as playback
 * starts (`{ ok: true }`), or with the error if nothing could be synthesized,
 * so the caller can fall back to the system voice. Playback of the remaining
 * pieces continues in the background until done or superseded.
 */
function speakViaServer(
  text: string,
  id: string,
  mine: number,
  backend: string | undefined,
  voiceId: string | undefined,
  set: SetState,
): Promise<{ ok: boolean; error?: string }> {
  const ac = new AbortController();
  controller = ac;
  const chunks = splitForSpeech(text);
  const synth = (t: string) => synthesizeSpeech(t, { signal: ac.signal, backend, voiceId });

  let announce!: (r: { ok: boolean; error?: string }) => void;
  const startedPromise = new Promise<{ ok: boolean; error?: string }>((res) => { announce = res; });
  let started = false;

  const finish = () => {
    if (mine !== token) return;
    teardown();
    set({ state: 'idle', speakingId: null });
  };

  (async () => {
    let next: Promise<Blob> | null = chunks.length ? synth(chunks[0]) : null;
    for (let i = 0; i < chunks.length && next; i += 1) {
      const blob = await next;
      if (mine !== token) return announce({ ok: true });
      next = i + 1 < chunks.length ? synth(chunks[i + 1]) : null;
      next?.catch(() => { /* surfaced when awaited */ });
      // The last piece finishes synchronously on `ended`, so the UI never
      // lingers in "speaking" after the audio stops.
      const playing = playBlob(blob, mine, i + 1 >= chunks.length ? finish : undefined);
      if (!started) {
        started = true;
        set({ state: 'speaking', speakingId: id });
        announce({ ok: true });
      }
      await playing;
      if (mine !== token) return;
    }
    if (!started) announce({ ok: true });
  })().catch((err) => {
    if (mine !== token || (err instanceof DOMException && err.name === 'AbortError')) {
      announce({ ok: true });
      return;
    }
    const message = err instanceof Error ? err.message : 'Speech synthesis failed';
    teardown();
    if (started) {
      set({ state: 'idle', speakingId: null, error: message, errorId: id });
    } else {
      set({ state: 'idle', speakingId: null });
      announce({ ok: false, error: message });
    }
  });

  return startedPromise;
}

/** Test seam: reset module-level playback handles between cases. */
export function __resetTtsForTests(): void {
  teardown();
  token = 0;
  healthProbe = null;
  useBrowserVoice = false;
  useTtsStore.setState({
    state: 'idle',
    speakingId: null,
    error: null,
    errorId: null,
    available: null,
    autoSpokenId: null,
  });
}
