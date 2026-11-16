// Memory v2 (Stonic port, phase 6) and the Always-Active preset.
// Every call resolves to null on failure, so the UI never throws on a
// backend that is off, still booting, or older than this build.

import { apiFetch } from './api';

export interface MemoryV2Status {
  available: boolean;
  enabled?: boolean;
  ready?: boolean;
  paused?: boolean;
  stats?: { memories: number; candidates: number; pinned: number; open_intentions: number; turns: number; episodes: number };
  backup?: { count: number; last_at: number | null };
  digest?: { has_model: boolean; last_error: string };
  notes?: { notes?: number; [key: string]: unknown };
}

export interface MemoryItem { id: number; text_original: string; text_en?: string; type: string; pinned: number; status: string; updated_at: number }
export interface IntentionItem { id: number; what: string; due_ts: number | null; status: string }
export interface PresetStatus { active: boolean; flags: Record<string, boolean>; ok?: boolean }

async function call<T>(path: string, init?: RequestInit): Promise<T | null> {
  try {
    const res = await apiFetch(path, init);
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

const post = <T>(path: string, body: unknown = {}) =>
  call<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

const V2 = '/v1/memory/v2';

export const memoryV2Api = {
  status: () => call<MemoryV2Status>(`${V2}/status`),
  memories: (limit = 8) => call<{ items: MemoryItem[] }>(`${V2}/memories?limit=${limit}`),
  intentions: () => call<{ items: IntentionItem[] }>(`${V2}/intentions`),
  pause: (paused: boolean) => post<{ paused: boolean }>(`${V2}/pause`, { paused }),
  backup: () => post<{ ok: boolean }>(`${V2}/backup`),
  rollback: () => post<{ ok: boolean; written: number }>(`${V2}/rollback`),
  forget: (id: number) => call<{ ok: boolean }>(`${V2}/memories/${id}`, { method: 'DELETE' }),
  resolve: (id: number) => post<{ ok: boolean }>(`${V2}/intentions/${id}/resolve`, { status: 'done' }),
};

export const stonicPresetApi = {
  status: () => call<PresetStatus>('/v1/stonic/preset'),
  set: (enabled: boolean) => post<PresetStatus>('/v1/stonic/preset', { enabled }),
};
