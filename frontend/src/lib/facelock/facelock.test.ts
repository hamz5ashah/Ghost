import { describe, expect, it } from 'vitest';
import { BlinkDetector } from './blink';
import { ENROLL, EnrollFlow, UNLOCK, UnlockFlow, pinProblem, type Embed } from './flows';
import {
  ARCFACE_TEMPLATE,
  NO_FACE,
  dot,
  frameQuality,
  l2normalize,
  similarityTransform,
  type FaceFrame,
} from './geometry';

// ── geometry ──────────────────────────────────────────────────────────────

describe('similarityTransform', () => {
  it('recovers a known rotation, scale and shift', () => {
    const theta = 0.3, scale = 1.7, tx = 12, ty = -5;
    const a = scale * Math.cos(theta), b = scale * Math.sin(theta);
    const src = ARCFACE_TEMPLATE.map(([x, y]) => [x, y] as [number, number]);
    const dst = src.map(([x, y]) => [a * x - b * y + tx, b * x + a * y + ty] as [number, number]);
    const m = similarityTransform(src, dst);
    expect(m.a).toBeCloseTo(a, 6);
    expect(m.b).toBeCloseTo(b, 6);
    expect(m.tx).toBeCloseTo(tx, 6);
    expect(m.ty).toBeCloseTo(ty, 6);
  });

  it('falls back to a pure shift for degenerate input', () => {
    const p: [number, number][] = [[3, 3], [3, 3]];
    expect(similarityTransform(p, [[5, 1], [5, 1]])).toEqual({ a: 1, b: 0, tx: 2, ty: -2 });
  });
});

const goodFrame = (over: Partial<FaceFrame> = {}): FaceFrame => ({
  t: 0,
  present: true,
  box: { cx: 0.5, cy: 0.5, w: 0.3, h: 0.4 },
  pose: { yaw: 0, pitch: 6, roll: 0 },
  eyeL: 0.9,
  eyeR: 0.9,
  span: 0.15,
  luma: 120,
  landmarks: [],
  ...over,
});

describe('frameQuality (Stonic thresholds)', () => {
  it.each([
    [{ span: 0.05 }, 'Move closer'],
    [{ span: 0.35 }, 'Move back a little'],
    [{ box: { cx: 0.8, cy: 0.5, w: 0.3, h: 0.3 } }, 'Centre your face in the ring'],
    [{ pose: { yaw: 30, pitch: 6, roll: 0 } }, 'Face the camera'],
    [{ luma: 20 }, 'More light on your face'],
    [{ luma: 250 }, 'Too bright — turn away from the light'],
  ])('%o → %s', (over, hint) => {
    expect(frameQuality(goodFrame(over as Partial<FaceFrame>))).toEqual({ ok: false, hint });
  });

  it('accepts a good frame, and an unmeasured luma', () => {
    expect(frameQuality(goodFrame()).ok).toBe(true);
    expect(frameQuality(goodFrame({ luma: -1 })).ok).toBe(true);
    expect(frameQuality(NO_FACE).ok).toBe(false);
  });
});

// ── blink ─────────────────────────────────────────────────────────────────

function runEyes(d: BlinkDetector, values: number[], stepMs = 33, start = 0, pose = { yaw: 0, pitch: 6, roll: 0 }) {
  let blinks = 0;
  values.forEach((v, i) => { if (d.update(v, v, start + i * stepMs, pose)) blinks += 1; });
  return blinks;
}

describe('BlinkDetector', () => {
  const open = Array(10).fill(0.9);

  it('counts a natural blink', () => {
    const d = new BlinkDetector();
    expect(runEyes(d, [...open, 0.1, 0.05, 0.1, 0.9, 0.9])).toBe(1);
    expect(d.count).toBe(1);
  });

  it('ignores eyes that never open first (a photo with closed eyes)', () => {
    expect(runEyes(new BlinkDetector(), Array(20).fill(0.05))).toBe(0);
  });

  it('ignores a closure longer than a blink', () => {
    expect(runEyes(new BlinkDetector(), [...open, ...Array(40).fill(0.05), 0.9])).toBe(0);
  });

  it('ignores a "blink" made by turning the head', () => {
    const d = new BlinkDetector();
    runEyes(d, open);
    d.update(0.05, 0.05, 400, { yaw: 0, pitch: 6, roll: 0 });
    expect(d.update(0.9, 0.9, 500, { yaw: 25, pitch: 6, roll: 0 })).toBe(false);
  });

  it('needs a warm-up before judging', () => {
    expect(runEyes(new BlinkDetector(), [0.9, 0.9, 0.05, 0.9])).toBe(0);
  });
});

// ── flows ─────────────────────────────────────────────────────────────────

const unit = (seed: number) => {
  let s = seed;
  const v = Array.from({ length: 128 }, () => { s = (s * 16807) % 2147483647; return s / 2147483647 - 0.5; });
  return l2normalize(v);
};

/** Drive a flow with synchronous-ish embeds; resolves pending promises between frames. */
async function drive(
  flow: { frame: (f: FaceFrame, now: number, e: Embed | null) => unknown },
  frames: Array<(t: number) => FaceFrame>,
  embed: Embed,
  stepMs = 33,
  start = 0,
) {
  let t = start;
  for (const make of frames) {
    flow.frame(make(t), t, embed);
    await Promise.resolve();
    await Promise.resolve();
    t += stepMs;
  }
  return t;
}

const repeat = <T,>(n: number, make: (i: number) => T) => Array.from({ length: n }, (_, i) => make(i));

describe('UnlockFlow', () => {
  const owner = unit(7);
  const embed: Embed = async () => owner;
  const face = (eye = 0.9) => () => goodFrame({ eyeL: eye, eyeR: eye });

  it('waits for a blink, then sends probes from before and after it', async () => {
    const sent: number[][][] = [];
    const flow = new UnlockFlow((p) => sent.push(p));
    // Steady face → liveness, no blink yet: at most 2 probes, nothing sent.
    let t = await drive(flow, repeat(40, () => face()), embed);
    expect(flow.state).toBe('liveness');
    expect(flow.view().probes).toBe(UNLOCK.PRE_BLINK_PROBES);
    expect(sent).toHaveLength(0);
    // Blink, then keep looking.
    t = await drive(flow, [face(0.05), face(0.05), face(0.05)], embed, 33, t);
    await drive(flow, repeat(40, () => face()), embed, 33, t);
    expect(sent).toHaveLength(1);
    expect(sent[0].length).toBe(UNLOCK.MAX_PROBES);
    expect(flow.state).toBe('verify');
  });

  it('a photo (no blink) never reaches the backend', async () => {
    const sent: number[][][] = [];
    const flow = new UnlockFlow((p) => sent.push(p));
    await drive(flow, repeat(400, () => face()), embed);
    expect(sent).toHaveLength(0);
  });

  it('starts over when the face jumps (someone else steps in)', async () => {
    const flow = new UnlockFlow(() => {});
    await drive(flow, repeat(30, () => face()), embed);
    expect(flow.state).toBe('liveness');
    flow.frame(goodFrame({ box: { cx: 0.85, cy: 0.5, w: 0.3, h: 0.4 } }), 5000, embed);
    expect(['search', 'acquire']).toContain(flow.state);
  });

  it('starts over when the face is lost', async () => {
    const flow = new UnlockFlow(() => {});
    const t = await drive(flow, repeat(30, () => face()), embed);
    flow.frame(NO_FACE, t + UNLOCK.LOST_MS + 1, embed);
    expect(flow.state).toBe('search');
  });
});

describe('EnrollFlow', () => {
  const owner = unit(11);
  // Each read is the owner's print with a little noise.
  let n = 0;
  const embed: Embed = async () => {
    n += 1;
    const noise = unit(1000 + n);
    return l2normalize(Array.from(owner, (v, i) => v * 0.9 + noise[i] * 0.1));
  };
  const at = (yaw: number, pitch: number, eye = 0.9) => () => goodFrame({ pose: { yaw, pitch, roll: 0 }, eyeL: eye, eyeR: eye });

  it('centre → 8 directions → blink → done, centre template first', async () => {
    const flow = new EnrollFlow();
    let t = await drive(flow, repeat(60, () => at(0, 6)), embed);
    expect(flow.state).toBe('angles');
    for (let s = 0; s < ENROLL.SECTORS; s += 1) {
      const a = (s / ENROLL.SECTORS) * Math.PI * 2;
      t = await drive(flow, repeat(8, () => at(Math.cos(a) * 18, 6 + (Math.sin(a) * 18) / ENROLL.PITCH_SCALE)), embed, 33, t);
    }
    t = await drive(flow, repeat(3, () => at(0, 6)), embed, 33, t);
    expect(flow.state).toBe('blink');
    t = await drive(flow, [at(0, 6, 0.05), at(0, 6, 0.05), at(0, 6, 0.05)], embed, 33, t);
    await drive(flow, repeat(30, () => at(0, 6)), embed, 33, t);
    expect(flow.state).toBe('done');
    const templates = flow.templates();
    expect(templates).toHaveLength(1 + ENROLL.SECTORS);
    expect(dot(templates[0], owner)).toBeGreaterThan(0.9);
    expect(flow.view().confirmScore).toBeGreaterThanOrEqual(ENROLL.CONFIRM_MIN);
  });

  it('refuses an angle that is someone else', async () => {
    const flow = new EnrollFlow();
    let t = await drive(flow, repeat(60, () => at(0, 6)), embed);
    const stranger: Embed = async () => unit(999);
    t = await drive(flow, repeat(10, () => at(18, 6)), stranger, 33, t);
    expect(flow.view().sectors.filter(Boolean)).toHaveLength(0);
  });

  it('a different face at the confirm step fails the scan', async () => {
    const flow = new EnrollFlow();
    let t = await drive(flow, repeat(60, () => at(0, 6)), embed);
    for (let s = 0; s < ENROLL.SECTORS; s += 1) {
      const a = (s / ENROLL.SECTORS) * Math.PI * 2;
      t = await drive(flow, repeat(8, () => at(Math.cos(a) * 18, 6 + (Math.sin(a) * 18) / ENROLL.PITCH_SCALE)), embed, 33, t);
    }
    t = await drive(flow, repeat(3, () => at(0, 6)), embed, 33, t);
    const other: Embed = async () => unit(4242);
    t = await drive(flow, [at(0, 6, 0.05), at(0, 6, 0.05), at(0, 6, 0.05)], other, 33, t);
    await drive(flow, repeat(30, () => at(0, 6)), other, 33, t);
    expect(flow.state).toBe('failed');
    expect(flow.view().error).toMatch(/not steady/);
  });
});

describe('pinProblem (mirrors the backend)', () => {
  it.each(['123456', '000000', '12345', 'abcdef', '1234567', '654321'])('%s is refused', (pin) => {
    expect(pinProblem(pin)).not.toBe('');
  });
  it('accepts a good passcode', () => {
    expect(pinProblem('482915')).toBe('');
  });
});
