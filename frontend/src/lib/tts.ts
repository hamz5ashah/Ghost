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
  if (persona === 'jarvis') {
    const british = voices.filter((v) => /en[-_]GB/i.test(v.lang));
    const male = british.find((v) => /george|ryan|daniel|thomas|arthur|oliver|male/i.test(v.name));
    if (male) return male;
    if (british[0]) return british[0];
  }
  return voices.find((v) => /^en/i.test(v.lang)) ?? voices[0];
}

/** Voice preferences, read at speak time (set from Settings → Assistant). */
let voicePrefs = { voiceName: '', persona: 'professional' };
export function setVoicePreferences(prefs: { voiceName: string; persona: string }): void {
  voicePrefs = prefs;
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

  speak: async (id: string, text: string) => {
    const trimmed = toSpeakableText(text);
    if (!trimmed) return;

    // Bump before teardown so a synthesis still in flight is both aborted and
    // fenced off by the token, even if the abort loses the race.
    token += 1;
    const mine = token;
    teardown();
    set({ state: 'loading', speakingId: id, error: null, errorId: null });

    if (useBrowserVoice) {
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

    const ac = new AbortController();
    controller = ac;

    try {
      const blob = await synthesizeSpeech(trimmed, { signal: ac.signal });
      if (mine !== token) return;

      const url = URL.createObjectURL(blob);
      objectUrl = url;
      const el = new Audio(url);
      audio = el;

      el.onended = () => {
        if (mine !== token) return;
        teardown();
        set({ state: 'idle', speakingId: null });
      };
      el.onerror = () => {
        if (mine !== token) return;
        teardown();
        set({ state: 'idle', speakingId: null, error: 'Playback failed', errorId: id });
      };

      await el.play();
      if (mine === token) set({ state: 'speaking', speakingId: id });
    } catch (err) {
      if (mine !== token) return;
      if (err instanceof DOMException && err.name === 'AbortError') return;
      teardown();
      set({
        state: 'idle',
        speakingId: null,
        error: err instanceof Error ? err.message : 'Speech synthesis failed',
        errorId: id,
      });
    }
  },

  stop: () => {
    token += 1;
    teardown();
    set({ state: 'idle', speakingId: null, error: null, errorId: null });
  },
}));

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
