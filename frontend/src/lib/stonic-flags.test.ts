import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  STONIC_FLAGS_OFF,
  getStonicFlags,
  normalizeFlags,
  peekStonicFlags,
  resetStonicFlags,
} from './stonic-flags';

const FLAG_KEYS = Object.keys(STONIC_FLAGS_OFF) as (keyof typeof STONIC_FLAGS_OFF)[];

const respond = (body: unknown, ok = true) =>
  ({ ok, json: async () => body }) as Response;

describe('normalizeFlags', () => {
  it('defaults every flag to false for an empty payload', () => {
    expect(normalizeFlags({})).toEqual(STONIC_FLAGS_OFF);
  });

  it.each([null, undefined, 'nope', 42, [], { flags: 'nope' }])(
    'falls back to all-off for malformed payload %p',
    (payload) => {
      expect(normalizeFlags(payload)).toEqual(STONIC_FLAGS_OFF);
    },
  );

  it('treats only exact true as enabled', () => {
    const flags = normalizeFlags({
      flags: { computer_use: 'true', facelock: 1, awareness: true },
    });
    expect(flags.computer_use).toBe(false);
    expect(flags.facelock).toBe(false);
    expect(flags.awareness).toBe(true);
  });

  it('ignores unknown keys instead of widening the flag set', () => {
    const flags = normalizeFlags({ flags: { not_a_flag: true } });
    expect(Object.keys(flags).sort()).toEqual([...FLAG_KEYS].sort());
  });

  it('never mutates the frozen all-off constant', () => {
    normalizeFlags({ flags: { ui_sounds: true } });
    expect(STONIC_FLAGS_OFF.ui_sounds).toBe(false);
  });
});

describe('getStonicFlags', () => {
  beforeEach(() => {
    resetStonicFlags();
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    resetStonicFlags();
    vi.unstubAllGlobals();
  });

  it('reads flags from the backend', async () => {
    vi.mocked(fetch).mockResolvedValue(respond({ flags: { computer_use: true } }));
    const flags = await getStonicFlags();
    expect(flags.computer_use).toBe(true);
    expect(flags.memory_v2).toBe(false);
  });

  it('resolves all-off when the endpoint errors', async () => {
    vi.mocked(fetch).mockResolvedValue(respond({}, false));
    await expect(getStonicFlags()).resolves.toEqual(STONIC_FLAGS_OFF);
  });

  it('resolves all-off instead of rejecting when fetch throws', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('backend still booting'));
    await expect(getStonicFlags()).resolves.toEqual(STONIC_FLAGS_OFF);
  });

  it('caches a successful read', async () => {
    vi.mocked(fetch).mockResolvedValue(respond({ flags: { ui_sounds: true } }));
    await getStonicFlags();
    await getStonicFlags();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('does not cache a failure, so a booting backend is retried', async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error('not up yet'));
    expect(await getStonicFlags()).toEqual(STONIC_FLAGS_OFF);
    vi.mocked(fetch).mockResolvedValue(respond({ flags: { awareness: true } }));
    expect((await getStonicFlags()).awareness).toBe(true);
  });

  it('shares one request across concurrent callers', async () => {
    vi.mocked(fetch).mockResolvedValue(respond({ flags: { gestures: true } }));
    const [a, b] = await Promise.all([getStonicFlags(), getStonicFlags()]);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(a.gestures).toBe(true);
    expect(b.gestures).toBe(true);
  });
});

describe('peekStonicFlags', () => {
  beforeEach(() => {
    resetStonicFlags();
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    resetStonicFlags();
    vi.unstubAllGlobals();
  });

  it('reports all-off before any read resolves', () => {
    expect(peekStonicFlags()).toEqual(STONIC_FLAGS_OFF);
  });

  it('reports the cached set after a read', async () => {
    vi.mocked(fetch).mockResolvedValue(respond({ flags: { facelock: true } }));
    await getStonicFlags();
    expect(peekStonicFlags().facelock).toBe(true);
  });

  it('reverts to all-off after a reset', async () => {
    vi.mocked(fetch).mockResolvedValue(respond({ flags: { facelock: true } }));
    await getStonicFlags();
    resetStonicFlags();
    expect(peekStonicFlags()).toEqual(STONIC_FLAGS_OFF);
  });
});
