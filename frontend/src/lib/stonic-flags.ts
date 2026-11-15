// Feature flags for the Stonic port (see /STONIC_PORT_PLAN.md).
//
// Every flag is off until the user turns it on in Settings, so the UI must
// render Ghost's own behaviour whenever these are unknown — a failed or
// pending fetch resolves to all-off rather than throwing. That is what keeps
// each phase of the port independently revertable.

import { apiFetch } from './api';

export interface StonicFlags {
  /** Phase 1 — UI sound effects on interface events. */
  ui_sounds: boolean;
  /** Phase 1 — Stonic character definitions as extra persona options. */
  characters: boolean;
  /** Phase 2 — mouse/keyboard/screen control via the Agent S3 sidecar. */
  computer_use: boolean;
  /** Phase 2 — draw the action overlay while computer use is running. */
  computer_overlay: boolean;
  /** Phase 3 — Pipecat realtime pipeline instead of Kokoro + faster-whisper. */
  realtime_voice: boolean;
  /** Phase 3 — always-listening wake word. */
  wake_word: boolean;
  /** Phase 4 — webcam sign-in. */
  facelock: boolean;
  /** Phase 4 — MediaPipe gesture control. */
  gestures: boolean;
  /** Phase 5 — ambient awareness (idle/lock/focus watching). */
  awareness: boolean;
  /** Phase 5 — character overlay and living wallpaper. */
  desktop_presence: boolean;
  /** Phase 6 — Stonic's richer memory schema. */
  memory_v2: boolean;
}

export const STONIC_FLAGS_OFF: Readonly<StonicFlags> = Object.freeze({
  ui_sounds: false,
  characters: false,
  computer_use: false,
  computer_overlay: false,
  realtime_voice: false,
  wake_word: false,
  facelock: false,
  gestures: false,
  awareness: false,
  desktop_presence: false,
  memory_v2: false,
});

interface FlagsResponse {
  flags?: Partial<Record<keyof StonicFlags, unknown>>;
  available?: boolean;
}

/** Coerce an arbitrary payload into a complete, all-boolean flag set. */
export function normalizeFlags(payload: unknown): StonicFlags {
  const raw = (payload as FlagsResponse | null)?.flags;
  if (!raw || typeof raw !== 'object') return { ...STONIC_FLAGS_OFF };
  const out = { ...STONIC_FLAGS_OFF } as StonicFlags;
  for (const key of Object.keys(STONIC_FLAGS_OFF) as (keyof StonicFlags)[]) {
    out[key] = raw[key] === true;
  }
  return out;
}

let cached: StonicFlags | null = null;
let inFlight: Promise<StonicFlags> | null = null;

/**
 * Read the flag set from the backend, cached for the session.
 *
 * Never rejects: on any failure it resolves to all-off and does not cache, so
 * a backend that is still booting is retried on the next call.
 */
export async function getStonicFlags(): Promise<StonicFlags> {
  if (cached) return cached;
  if (inFlight) return inFlight;
  inFlight = (async () => {
    try {
      const res = await apiFetch('/v1/stonic/flags');
      if (!res.ok) return { ...STONIC_FLAGS_OFF };
      const flags = normalizeFlags(await res.json());
      cached = flags;
      return flags;
    } catch {
      return { ...STONIC_FLAGS_OFF };
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}

/** Synchronous read for render paths; all-off until getStonicFlags resolves. */
export function peekStonicFlags(): StonicFlags {
  return cached ?? { ...STONIC_FLAGS_OFF };
}

/** Drop the cache so the next read re-fetches (after a Settings change). */
export function resetStonicFlags(): void {
  cached = null;
  inFlight = null;
}
