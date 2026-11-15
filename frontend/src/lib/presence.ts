// Desktop presence (Stonic port, Phase 5) — helpers shared by the main window,
// the notch overlay (`?mode=presence`) and the living wallpaper (`?mode=wallpaper`).

import { isTauri } from './api';
import type { PresencePhase } from './awareness';

export type PresenceSize = 's' | 'm' | 'l';
export type PresenceMode = 'app' | 'presence' | 'wallpaper';

export interface PresencePrefs {
  enabled: boolean;
  size: PresenceSize;
  wallpaper: boolean;
}

export interface PresenceInfo {
  available: boolean;
  prefs: PresencePrefs;
  overlay: boolean;
  wallpaper_supported: boolean;
  wallpaper: 'off' | 'starting' | 'on';
  wallpaper_error: string | null;
}

/** What the main window shares with the overlay and the wallpaper. */
export interface PresenceSnapshot {
  phase: PresencePhase;
  caption: string;
  thought: string;
  thoughtAt: number;
  mood: string;
  voiceOpen: boolean;
}

// Layout, unchanged from Stonic (CSS px; the Rust side sizes the window the same way).
export const STAGE: Record<PresenceSize, number> = { s: 150, m: 210, l: 280 };
export const THOUGHT_ZONE = 112;
export const CAPTION = 34;
export const BAR = 52;
export const PANEL_WIDTH = 376;
export const NOTCH = { width: 236, height: 38 } as const;
export const WINDOW_WIDTH = 412;

/** Which page this window shows, from its URL. Pure. */
export function modeFromSearch(search: string): { mode: PresenceMode; size: PresenceSize } {
  const q = new URLSearchParams(search);
  const m = q.get('mode');
  const s = q.get('size');
  return {
    mode: m === 'presence' || m === 'wallpaper' ? m : 'app',
    size: s === 's' || s === 'l' ? s : 'm',
  };
}

/**
 * The notch's clickable rectangle in CSS px of the overlay window. Pure.
 * Mini: the 236×38 pill at the top. Expanded: the panel (stage + caption +
 * bar), plus the menu below it when open.
 */
export function hitRect(expanded: boolean, size: PresenceSize, menuOpen = false) {
  if (!expanded) {
    return { x: (WINDOW_WIDTH - NOTCH.width) / 2, y: 0, w: NOTCH.width, h: NOTCH.height };
  }
  const h = STAGE[size] + CAPTION + BAR + (menuOpen ? 220 : 0);
  return { x: (WINDOW_WIDTH - PANEL_WIDTH) / 2, y: 0, w: PANEL_WIDTH, h };
}

/** Video tempo per phase (Stonic's female avatar). Pure. */
export function tempoFor(phase: PresencePhase, energy = 0.5): number {
  switch (phase) {
    case 'sleep': return 0.37;
    case 'listen': return 0.36;
    case 'think':
    case 'wake': return 0.6;
    case 'speak': return 0.5 + 0.55 * Math.max(0, Math.min(1, energy));
    default: return 0.4;
  }
}

/** Wallpaper "wake" input per phase (Stonic: live 1, connecting .6, else .25). Pure. */
export function wakeFor(phase: PresencePhase, voiceOpen: boolean): number {
  if (voiceOpen || phase === 'speak' || phase === 'listen') return 1;
  if (phase === 'think' || phase === 'wake') return 0.6;
  return 0.25;
}

/**
 * Night dimming, 0..1: full from 22:00 to 05:00, ramping in from 20:00 and out
 * until 07:00. Straight from Stonic's wallpaper. Pure.
 */
export function nightLevel(d: Date = new Date()): number {
  const h = d.getHours() + d.getMinutes() / 60;
  if (h >= 22 || h < 5) return 1;
  if (h >= 20) return (h - 20) / 2;
  if (h < 7) return 1 - (h - 5) / 2;
  return 0;
}

async function tauriInvoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T | null> {
  if (!isTauri()) return null;
  try {
    const { invoke } = await import('@tauri-apps/api/core');
    return await invoke<T>(cmd, args);
  } catch {
    return null;
  }
}

export const presenceApi = {
  state: () => tauriInvoke<PresenceInfo>('presence_state'),
  setPrefs: (patch: Partial<PresencePrefs>) => tauriInvoke<PresenceInfo>('presence_set_prefs', { patch }),
  hit: (rect: ReturnType<typeof hitRect> | null) => tauriInvoke<void>('presence_hit', { rect }),
  command: (kind: 'toggle' | 'mute' | 'open' | 'leave') => tauriInvoke<void>('presence_command', { kind }),
  snapshot: (snapshot: PresenceSnapshot) => tauriInvoke<void>('presence_snapshot', { snapshot }),
};

/** Listen for a Tauri event; returns an unsubscribe. No-op outside the desktop app. */
export async function onPresenceEvent<T>(name: string, handler: (payload: T) => void): Promise<() => void> {
  if (!isTauri()) return () => {};
  try {
    const { listen } = await import('@tauri-apps/api/event');
    return await listen<T>(name, (e) => handler(e.payload));
  } catch {
    return () => {};
  }
}

/** URL for a bundled Stonic video (asset protocol), or null if not shipped. */
export async function stonicVideoUrl(name: string): Promise<string | null> {
  const path = await tauriInvoke<string | null>('stonic_asset_path', { kind: 'video', name });
  if (!path) return null;
  try {
    const { convertFileSrc } = await import('@tauri-apps/api/core');
    return convertFileSrc(path);
  } catch {
    return null;
  }
}
