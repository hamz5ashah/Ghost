import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { STONIC_FLAGS_OFF } from './stonic-flags';
import {
  BRAIN_OPTIONS,
  effectiveConversationEngine,
  fetchRealtimeStatus,
  startRealtimeEngine,
} from './realtime-voice';

const respond = (body: unknown, ok = true, status = 200) =>
  ({ ok, status, json: async () => body }) as Response;

describe('effectiveConversationEngine', () => {
  const on = { ...STONIC_FLAGS_OFF, realtime_voice: true };

  it('is Ghost voice (Kokoro) by default', () => {
    expect(effectiveConversationEngine(undefined, STONIC_FLAGS_OFF)).toBe('ghost');
    expect(effectiveConversationEngine('ghost', on)).toBe('ghost');
  });

  it('ignores a realtime choice while the flag is off', () => {
    expect(effectiveConversationEngine('realtime', STONIC_FLAGS_OFF)).toBe('ghost');
  });

  it('uses realtime only when chosen and the flag is on', () => {
    expect(effectiveConversationEngine('realtime', on)).toBe('realtime');
  });
});

describe('brain options', () => {
  it('offers only direct providers, never a Stonic gateway or Groq', () => {
    expect(BRAIN_OPTIONS.map((b) => b.key)).toEqual(['', 'anthropic', 'gemini', 'deepseek', 'openrouter']);
  });
});

describe('API calls', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('start sends preferences but never any key', async () => {
    vi.mocked(fetch).mockResolvedValue(respond({ ok: true, brain: { provider: 'gemini', model: 'm' } }));
    await startRealtimeEngine({ brain: 'gemini', voiceMode: 'tts' });
    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(String(url)).toContain('/v1/speech/realtime/start');
    const body = JSON.parse(String((init as RequestInit).body));
    expect(body).toEqual({ brain: 'gemini', voice_mode: 'tts', character: {} });
    expect(JSON.stringify(body)).not.toMatch(/key/i);
  });

  it('surfaces the server message on refusal', async () => {
    vi.mocked(fetch).mockResolvedValue(
      respond({ ok: false, error: 'key_missing', message: 'Missing keys for: gemini' }, false, 409),
    );
    await expect(startRealtimeEngine({ brain: '', voiceMode: 'tts' })).rejects.toThrow('Missing keys for: gemini');
  });

  it('status hits the realtime status route', async () => {
    vi.mocked(fetch).mockResolvedValue(respond({ enabled: false }));
    await fetchRealtimeStatus();
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('/v1/speech/realtime/status');
  });
});
