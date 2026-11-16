// UI sound effects ported from Stonic (Phase 1 of /STONIC_PORT_PLAN.md).
//
// Gated on the `ui_sounds` flag, which is off by default — Ghost shipped with
// no interface sounds, so this must stay silent until the user asks for it.
//
// The semantic names and the files behind them are the ones Stonic's own
// renderer used (recovered from its bundle, which mapped
// `{ hover, click, success }` to hover.wav / button.wav / access_granted.wav).
//
// Clips are fetched lazily and cached: the full set is 8.8 MB of WAV, so
// preloading it would cost more than every sound is worth. Playback never
// throws — a blocked autoplay policy or a missing file degrades to silence.

import { peekStonicFlags } from './stonic-flags';

const BASE = '/stonic/audio';

/** Semantic event → clip. Keep the names stable; the files are Stonic's. */
export const SOUNDS = {
  // Short interaction clips — the set Stonic's renderer wired up.
  hover: `${BASE}/hover.wav`,
  hoverButton: `${BASE}/hover_button.wav`,
  click: `${BASE}/button.wav`,
  uiClick: `${BASE}/04_ui_click.wav`,
  success: `${BASE}/access_granted.wav`,
  action: `${BASE}/action.wav`,
  mic: `${BASE}/mic.wav`,
  loading: `${BASE}/loading.wav`,
  appearanceLoading: `${BASE}/apperance_loading.wav`,
  initialize: `${BASE}/initialize.wav`,
  alert: `${BASE}/alert.wav`,
  // Longer cinematic cues from Stonic's designed set.
  panelOpen: `${BASE}/02_hologram_panel_open.wav`,
  dataScan: `${BASE}/03_data_scan.wav`,
  accessGrantedLong: `${BASE}/05_access_granted.wav`,
  softError: `${BASE}/06_soft_error.wav`,
  roomTone: `${BASE}/10_room_tone_ambience_LOOP.wav`,
} as const;

export type SoundName = keyof typeof SOUNDS;

/** The ambience bed loops; everything else is one-shot. */
export const LOOPING: ReadonlySet<SoundName> = new Set<SoundName>(['roomTone']);

const DEFAULT_VOLUME = 0.35;

let volume = DEFAULT_VOLUME;
let muted = false;
// Element cache keyed by name, so repeated clicks reuse one decoded clip.
const cache = new Map<SoundName, HTMLAudioElement>();
const loops = new Map<SoundName, HTMLAudioElement>();

/** 0..1, clamped. Values outside the range are ignored rather than thrown. */
export function setVolume(next: number): void {
  if (!Number.isFinite(next)) return;
  volume = Math.min(1, Math.max(0, next));
  for (const el of cache.values()) el.volume = volume;
  for (const el of loops.values()) el.volume = volume;
}

export function getVolume(): number {
  return volume;
}

export function setMuted(next: boolean): void {
  muted = next;
  if (muted) stopAllLoops();
}

export function isMuted(): boolean {
  return muted;
}

/** True when sounds should actually be audible right now. */
export function soundsEnabled(): boolean {
  return peekStonicFlags().ui_sounds && !muted;
}

function element(name: SoundName): HTMLAudioElement | null {
  const cached = cache.get(name);
  if (cached) return cached;
  if (typeof Audio === 'undefined') return null;
  try {
    const el = new Audio(SOUNDS[name]);
    el.preload = 'none';
    el.volume = volume;
    if (LOOPING.has(name)) el.loop = true;
    cache.set(name, el);
    return el;
  } catch {
    return null;
  }
}

/**
 * Play a one-shot cue. Resolves to whether it actually played, so callers can
 * tell "disabled" from "failed" in tests without inspecting the DOM.
 */
export function play(name: SoundName): boolean {
  if (!soundsEnabled()) return false;
  if (LOOPING.has(name)) return startLoop(name);
  const el = element(name);
  if (!el) return false;
  try {
    el.currentTime = 0;
    const result = el.play() as unknown as Promise<void> | undefined;
    // Autoplay policy rejects until the first user gesture; that is normal and
    // must not surface as an unhandled rejection.
    if (result && typeof result.catch === 'function') result.catch(() => {});
    return true;
  } catch {
    return false;
  }
}

export function startLoop(name: SoundName): boolean {
  if (!soundsEnabled()) return false;
  if (loops.has(name)) return true;
  const el = element(name);
  if (!el) return false;
  el.loop = true;
  try {
    const result = el.play() as unknown as Promise<void> | undefined;
    if (result && typeof result.catch === 'function') result.catch(() => {});
    loops.set(name, el);
    return true;
  } catch {
    return false;
  }
}

export function stopLoop(name: SoundName): void {
  const el = loops.get(name);
  if (!el) return;
  try {
    el.pause();
    el.currentTime = 0;
  } catch {
    /* element may already be detached */
  }
  loops.delete(name);
}

export function stopAllLoops(): void {
  for (const name of [...loops.keys()]) stopLoop(name);
}

/** Reset module state. Tests only. */
export function __resetSoundsForTests(): void {
  stopAllLoops();
  cache.clear();
  loops.clear();
  volume = DEFAULT_VOLUME;
  muted = false;
}
