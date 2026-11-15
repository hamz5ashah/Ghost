// Face Lock backend calls (ghost.security.facelock). The WebView sends
// measurements and receives decisions; templates and the PIN hash never
// come back.

import { apiFetch } from '../api';

export interface FaceLockStatus {
  available: boolean;
  supported: boolean;
  enabled: boolean;
  locked: boolean;
  templateCount: number;
  attemptsLeft: number;
  maxAttempts: number;
  lockoutMs: number;
  pinLength: number;
}

export interface FaceLockReply {
  ok: boolean;
  reason?: string;
  score?: number;
  ticket?: string;
  status?: FaceLockStatus;
}

export const FACELOCK_OFF: FaceLockStatus = Object.freeze({
  available: false, supported: false, enabled: false, locked: false,
  templateCount: 0, attemptsLeft: 0, maxAttempts: 0, lockoutMs: 0, pinLength: 6,
});

/** Fired on window when the backend reports the app locked. */
export const FACELOCK_LOCKED_EVENT = 'ghost:facelock-locked';

export type Purpose = 'unlock' | 'manage';
export type Bundle = 'facelock' | 'gestures';

const BASE = '/v1/security/facelock';

async function post(path: string, body: unknown = {}): Promise<FaceLockReply> {
  const res = await apiFetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: false, ...data } as FaceLockReply;
}

/** Never rejects: a backend that is still starting reads as "no lock". */
export async function fetchFaceLockStatus(): Promise<FaceLockStatus> {
  try {
    const res = await apiFetch(`${BASE}/status`);
    if (!res.ok) return { ...FACELOCK_OFF };
    return { ...FACELOCK_OFF, ...(await res.json()) };
  } catch {
    return { ...FACELOCK_OFF };
  }
}

export const verifyFace = (embeddings: number[][], purpose: Purpose = 'unlock') =>
  post('/verify-face', { embeddings, purpose });
export const verifyPin = (pin: string, purpose: Purpose = 'unlock') => post('/verify-pin', { pin, purpose });
export const enrollFace = (templates: number[][], pin: string, ticket?: string) =>
  post('/enroll', { templates, pin, ticket: ticket ?? null });
export const changePin = (pin: string, ticket: string) => post('/change-pin', { pin, ticket });
export const disableFaceLock = (ticket: string) => post('/disable', { ticket });
export const lockNow = () => post('/lock');

export interface ModelStatus {
  state: 'missing' | 'downloading' | 'ready' | 'error' | 'idle';
  downloaded: number;
  total: number;
  error: string;
  file?: string;
}

export async function fetchModelStatus(bundle: Bundle): Promise<ModelStatus> {
  const res = await apiFetch(`${BASE}/models?bundle=${bundle}`);
  if (!res.ok) throw new Error(`Model status unavailable (${res.status})`);
  return res.json();
}

export async function startModelDownload(bundle: Bundle): Promise<ModelStatus> {
  const res = await apiFetch(`${BASE}/models/download?bundle=${bundle}`, { method: 'POST' });
  if (!res.ok) throw new Error(`Download could not start (${res.status})`);
  return res.json();
}

/** Plain-English reason for a refusal. */
export function reasonText(reason?: string): string {
  switch (reason) {
    case 'no_match': return 'This face does not match.';
    case 'incoherent': return 'The camera saw more than one face. Try again, alone in the frame.';
    case 'wrong_pin': return 'Wrong passcode.';
    case 'lockout': return 'Too many attempts. Wait, then try again.';
    case 'weak_pin': return 'That passcode is too easy to guess.';
    case 'verify_first': return 'Confirm it is you first.';
    case 'unsupported': return 'Face Lock needs Windows (your face print is encrypted with your Windows account).';
    case 'save_failed': return 'The face print could not be saved. Nothing was changed.';
    case 'bad_input': return 'The scan was incomplete. Try again.';
    default: return reason ? `Not accepted (${reason}).` : 'Not accepted.';
  }
}
