import { afterEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.fn();
vi.mock('./api', () => ({ apiFetch: (...a: unknown[]) => apiFetch(...a) }));

import { fetchRealtimeStatus } from './realtime-voice';

afterEach(() => apiFetch.mockReset());

describe('fetchRealtimeStatus', () => {
  it('rejects an incomplete reply instead of letting Settings crash', async () => {
    apiFetch.mockResolvedValue(new Response('{}', { status: 200 }));
    await expect(fetchRealtimeStatus()).rejects.toThrow(/not available/);
  });

  it('returns a complete status', async () => {
    const status = { runtime: { state: 'missing' }, keys: { gemini: true, anthropic: false, deepseek: false, openrouter: false } };
    apiFetch.mockResolvedValue(new Response(JSON.stringify(status), { status: 200 }));
    await expect(fetchRealtimeStatus()).resolves.toMatchObject({ runtime: { state: 'missing' } });
  });
});
