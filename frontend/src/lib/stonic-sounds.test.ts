import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getStonicFlags, resetStonicFlags } from './stonic-flags';
import {
  LOOPING,
  SOUNDS,
  __resetSoundsForTests,
  getVolume,
  isMuted,
  play,
  setMuted,
  setVolume,
  soundsEnabled,
  startLoop,
  stopAllLoops,
  stopLoop,
  type SoundName,
} from './stonic-sounds';

// Minimal HTMLAudioElement stand-in: records calls, lets a test force a
// rejected or throwing play() to prove neither escapes.
class FakeAudio {
  static instances: FakeAudio[] = [];
  static playBehaviour: 'resolve' | 'reject' | 'throw' = 'resolve';

  src: string;
  volume = 1;
  loop = false;
  preload = '';
  currentTime = 0;
  playCalls = 0;
  pauseCalls = 0;

  constructor(src: string) {
    this.src = src;
    FakeAudio.instances.push(this);
  }

  play() {
    this.playCalls += 1;
    if (FakeAudio.playBehaviour === 'throw') throw new Error('no audio device');
    if (FakeAudio.playBehaviour === 'reject') {
      return Promise.reject(new Error('autoplay blocked'));
    }
    return Promise.resolve();
  }

  pause() {
    this.pauseCalls += 1;
  }
}

const enableFlag = async (ui_sounds: boolean) => {
  resetStonicFlags();
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: async () => ({ flags: { ui_sounds } }) }),
  );
  await getStonicFlags();
};

beforeEach(() => {
  FakeAudio.instances = [];
  FakeAudio.playBehaviour = 'resolve';
  vi.stubGlobal('Audio', FakeAudio as unknown as typeof Audio);
  __resetSoundsForTests();
});

afterEach(() => {
  __resetSoundsForTests();
  resetStonicFlags();
  vi.unstubAllGlobals();
});

describe('sound manifest', () => {
  it('points every cue at the stonic audio directory', () => {
    for (const path of Object.values(SOUNDS)) {
      expect(path).toMatch(/^\/stonic\/audio\/[\w.]+\.wav$/);
    }
  });

  it('covers all 16 recovered clips with no duplicates', () => {
    const paths = Object.values(SOUNDS);
    expect(paths).toHaveLength(16);
    expect(new Set(paths).size).toBe(16);
  });

  it('maps the three cues Stonic itself wired up', () => {
    // Recovered from Stonic's bundle: { hover, click, success }.
    expect(SOUNDS.hover).toBe('/stonic/audio/hover.wav');
    expect(SOUNDS.click).toBe('/stonic/audio/button.wav');
    expect(SOUNDS.success).toBe('/stonic/audio/access_granted.wav');
  });

  it('treats only the ambience bed as looping', () => {
    expect([...LOOPING]).toEqual(['roomTone']);
  });
});

describe('flag gating', () => {
  it('stays silent while ui_sounds is off', async () => {
    await enableFlag(false);
    expect(soundsEnabled()).toBe(false);
    expect(play('click')).toBe(false);
    expect(FakeAudio.instances).toHaveLength(0);
  });

  it('stays silent before the flags have been read', () => {
    resetStonicFlags();
    expect(soundsEnabled()).toBe(false);
    expect(play('click')).toBe(false);
  });

  it('plays once ui_sounds is on', async () => {
    await enableFlag(true);
    expect(play('click')).toBe(true);
    expect(FakeAudio.instances).toHaveLength(1);
    expect(FakeAudio.instances[0].src).toBe(SOUNDS.click);
    expect(FakeAudio.instances[0].playCalls).toBe(1);
  });
});

describe('playback', () => {
  beforeEach(async () => {
    await enableFlag(true);
  });

  it('reuses one element per cue', () => {
    play('click');
    play('click');
    expect(FakeAudio.instances).toHaveLength(1);
    expect(FakeAudio.instances[0].playCalls).toBe(2);
  });

  it('creates separate elements for different cues', () => {
    play('click');
    play('hover');
    expect(FakeAudio.instances.map((a) => a.src)).toEqual([SOUNDS.click, SOUNDS.hover]);
  });

  it('restarts a cue from the beginning', () => {
    play('click');
    FakeAudio.instances[0].currentTime = 2;
    play('click');
    expect(FakeAudio.instances[0].currentTime).toBe(0);
  });

  it('does not preload, so 8.8 MB is never fetched up front', () => {
    play('click');
    expect(FakeAudio.instances[0].preload).toBe('none');
  });

  it('swallows a blocked autoplay rejection', async () => {
    FakeAudio.playBehaviour = 'reject';
    expect(play('click')).toBe(true);
    // An unhandled rejection would fail the suite on the next tick.
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  it('reports failure instead of throwing when play() throws', () => {
    FakeAudio.playBehaviour = 'throw';
    expect(play('click')).toBe(false);
  });

  it('reports failure when the environment has no Audio', () => {
    vi.stubGlobal('Audio', undefined);
    expect(play('click')).toBe(false);
  });

  it.each(Object.keys(SOUNDS) as SoundName[])('can play %s', (name) => {
    expect(play(name)).toBe(true);
  });
});

describe('volume and mute', () => {
  beforeEach(async () => {
    await enableFlag(true);
  });

  it('applies the default volume to new elements', () => {
    play('click');
    expect(FakeAudio.instances[0].volume).toBe(0.35);
  });

  it('clamps volume into 0..1', () => {
    setVolume(5);
    expect(getVolume()).toBe(1);
    setVolume(-2);
    expect(getVolume()).toBe(0);
  });

  it('ignores a non-finite volume rather than corrupting state', () => {
    setVolume(0.5);
    setVolume(Number.NaN);
    expect(getVolume()).toBe(0.5);
  });

  it('applies a volume change to already-cached elements', () => {
    play('click');
    setVolume(0.8);
    expect(FakeAudio.instances[0].volume).toBeCloseTo(0.8);
  });

  it('mute silences playback without clearing the flag', () => {
    setMuted(true);
    expect(isMuted()).toBe(true);
    expect(soundsEnabled()).toBe(false);
    expect(play('click')).toBe(false);
    setMuted(false);
    expect(play('click')).toBe(true);
  });
});

describe('looping ambience', () => {
  beforeEach(async () => {
    await enableFlag(true);
  });

  it('starts the bed on loop', () => {
    expect(startLoop('roomTone')).toBe(true);
    expect(FakeAudio.instances[0].loop).toBe(true);
  });

  it('routing play() to a looping cue does not stack elements', () => {
    play('roomTone');
    play('roomTone');
    expect(FakeAudio.instances).toHaveLength(1);
    expect(FakeAudio.instances[0].playCalls).toBe(1);
  });

  it('stops and rewinds the bed', () => {
    startLoop('roomTone');
    stopLoop('roomTone');
    expect(FakeAudio.instances[0].pauseCalls).toBe(1);
    expect(FakeAudio.instances[0].currentTime).toBe(0);
  });

  it('stopping a loop that is not running is a no-op', () => {
    expect(() => stopLoop('roomTone')).not.toThrow();
  });

  it('muting stops a running loop', () => {
    startLoop('roomTone');
    setMuted(true);
    expect(FakeAudio.instances[0].pauseCalls).toBe(1);
  });

  it('stopAllLoops clears everything', () => {
    startLoop('roomTone');
    stopAllLoops();
    expect(FakeAudio.instances[0].pauseCalls).toBe(1);
    // Restartable afterwards.
    expect(startLoop('roomTone')).toBe(true);
  });
});
