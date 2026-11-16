// Awareness (Stonic port, Phase 5) — the app side of the awareness engine.
//
// The backend's eyes and brain decide *whether* to speak; this module is the
// voice and ears around them: it fetches `speak` events, says the line with
// Ghost's own TTS, tells the brain how the line landed, and reports what the
// conversation is doing so the brain never talks over the user.
//
// Everything here is inert until `[stonic].awareness` is on and the user turns
// awareness on in Settings (the backend refuses with 403 otherwise).

import { create } from 'zustand';
import { apiFetch } from './api';
import type { ChatMessage } from '../types';

/** A line has this long to be answered before it counts as ignored (the engine's own window). */
export const ANSWER_WINDOW_MS = 2 * 60_000;
export const EVENTS_POLL_MS = 3_000;
export const ACTIVITY_EVERY_MS = 10_000;
/** A thought stays on the overlay this long (Stonic: 3 min). */
export const THOUGHT_TTL_MS = 3 * 60_000;

export interface SpeakEvent {
  seq: number;
  type: 'speak';
  t: number;
  say: string;
  mood?: string;
  kind?: string;
  trigger?: string;
  character?: string;
}

export interface MindEvent {
  seq: number;
  type: 'mind';
  /** thinking | thought | idle | off */
  phase?: string;
  /** The brain's private note (phase 'thought'). */
  text?: string;
  mood?: string;
  at?: number;
}

export type AwarenessEvent = SpeakEvent | MindEvent | { seq: number; type: string; [key: string]: unknown };

export interface AwarenessStatus {
  available: boolean;
  enabled: boolean;
  eyes?: Record<string, unknown>;
  engine?: {
    on?: boolean;
    label?: string;
    mood?: string;
    thoughtsToday?: number;
    spokenToday?: number;
    restUntil?: number;
    thoughts?: Array<{ t: number; say?: string; spoke?: boolean; note?: string; mood?: string; trigger?: string }>;
    [key: string]: unknown;
  };
}

async function call<T>(path: string, init?: RequestInit): Promise<T | null> {
  try {
    const res = await apiFetch(`/v1/awareness${path}`, init);
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

const post = <T>(path: string, body: unknown = {}) =>
  call<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

export const awarenessApi = {
  status: () => call<AwarenessStatus>('/status'),
  configure: (enabled?: boolean, character?: Record<string, string>) =>
    post<AwarenessStatus>('/configure', { enabled, character }),
  events: (after: number) => call<{ events: AwarenessEvent[]; last: number }>(`/events?after=${after}`),
  activity: (body: ActivityReport) => post<{ ok: boolean }>('/activity', body),
  outcome: (t: number, outcome: Outcome, reply = '') => post<{ ok: boolean }>('/outcome', { t, outcome, reply }),
  quiet: (minutes: number) => post<AwarenessStatus['engine']>('/quiet', { minutes }),
  thinkNow: () => post<{ ok: boolean; decision: unknown }>('/think-now'),
  forget: () => post<Record<string, unknown>>('/forget'),
};

export type Outcome = 'answered' | 'ignored' | 'dismissed';

export interface ActivityReport {
  voiceOpen: boolean;
  speaking: boolean;
  lastUserAt: number;
  recentTalk: Array<{ role: 'user' | 'assistant'; text: string }>;
}

/** The last few turns, trimmed the way the backend keeps them. Pure. */
export function recentTalk(messages: ChatMessage[], keep = 8): ActivityReport['recentTalk'] {
  return messages
    .filter((m) => m.content.trim())
    .slice(-keep)
    .map((m) => ({ role: m.role, text: m.content.slice(0, 400) }));
}

/** When the user last said something (ms), 0 if never. Pure. */
export function lastUserAt(messages: ChatMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'user') return messages[i].timestamp;
  }
  return 0;
}

/**
 * Did the user answer a line spoken at `spokeAt`? The first user message
 * inside the answer window counts. Pure.
 */
export function answerTo(messages: ChatMessage[], spokeAt: number, now: number): string | null {
  if (now - spokeAt > ANSWER_WINDOW_MS + 5_000) return null;
  const reply = messages.find(
    (m) => m.role === 'user' && m.timestamp >= spokeAt && m.timestamp <= spokeAt + ANSWER_WINDOW_MS,
  );
  return reply ? reply.content.slice(0, 400) : null;
}

/**
 * Split a batch of events into lines to speak and thoughts to show. On the
 * first poll (`primed` false) nothing is spoken: a line from before the app
 * opened is stale. Pure.
 */
export function triage(events: AwarenessEvent[], primed: boolean): { speak: SpeakEvent[]; minds: MindEvent[] } {
  const speak: SpeakEvent[] = [];
  const minds: MindEvent[] = [];
  for (const e of events) {
    if (e.type === 'speak' && typeof (e as SpeakEvent).say === 'string' && primed) speak.push(e as SpeakEvent);
    else if (e.type === 'mind') minds.push(e as MindEvent);
  }
  // Two lines in one poll: only the newest is still worth saying.
  return { speak: speak.slice(-1), minds };
}

// -- live state shared with the desktop overlay and wallpaper -----------------

export type PresencePhase = 'idle' | 'listen' | 'think' | 'speak' | 'sleep' | 'wake';

export interface LiveState {
  phase: PresencePhase;
  caption: string;
  mood: string;
  thought: string;
  thoughtAt: number;
  voiceOpen: boolean;
  /** The backend refused or awareness is off: nothing proactive. */
  awake: boolean;
}

interface LiveStore extends LiveState {
  patch: (p: Partial<LiveState>) => void;
}

export const useAwarenessLive = create<LiveStore>((set) => ({
  phase: 'idle',
  caption: '',
  mood: '',
  thought: '',
  thoughtAt: 0,
  voiceOpen: false,
  awake: false,
  patch: (p) => set(p),
}));

/** The phase the character shows. Pure. */
export function phaseFor(s: { speaking: boolean; voiceOpen: boolean; streaming: boolean; locked?: boolean }): PresencePhase {
  if (s.locked) return 'sleep';
  if (s.speaking) return 'speak';
  if (s.streaming) return 'think';
  if (s.voiceOpen) return 'listen';
  return 'idle';
}
