import { describe, expect, it } from 'vitest';
import { HAND, HandController, type HandPoint, type PointerOutput } from './controller';

/** A hand with chosen fingers up. Camera space: y grows downward. */
function hand(up: { index?: boolean; middle?: boolean; ring?: boolean; pinky?: boolean }, opts: { at?: HandPoint; pinch?: 'index' | 'middle' } = {}) {
  const base = opts.at ?? { x: 0.5, y: 0.5 };
  const pts: HandPoint[] = Array.from({ length: 21 }, () => ({ ...base }));
  pts[0] = { x: base.x, y: base.y + 0.2 }; // wrist
  pts[9] = { x: base.x, y: base.y }; // middle MCP (scale = 0.2)
  const finger = (pip: number, tip: number, isUp: boolean | undefined, dx: number) => {
    pts[pip] = { x: base.x + dx, y: base.y - 0.05 };
    pts[tip] = { x: base.x + dx, y: isUp ? base.y - 0.15 : base.y };
  };
  finger(6, 8, up.index, -0.03);
  finger(10, 12, up.middle, 0);
  finger(14, 16, up.ring, 0.03);
  finger(18, 20, up.pinky, 0.06);
  pts[4] = { x: base.x - 0.2, y: base.y }; // thumb, well away
  if (opts.pinch === 'index') pts[4] = { ...pts[8] };
  if (opts.pinch === 'middle') pts[4] = { ...pts[12] };
  return pts;
}

function recorder() {
  const log: string[] = [];
  const out: PointerOutput = {
    move: (x, y) => log.push(`move ${x},${y}`),
    button: (b, down) => log.push(`${b} ${down ? 'down' : 'up'}`),
    click: (b) => log.push(`click ${b}`),
    scroll: (d, a) => log.push(`scroll ${d} ${a}`),
  };
  return { log, out };
}

const SCREEN = { width: 1000, height: 800 };
const TWO = { index: true, middle: true };

describe('HandController', () => {
  it('does nothing without a hand, or with an idle hand', () => {
    const { log, out } = recorder();
    const c = new HandController(out, SCREEN);
    expect(c.frame(null, 'None', 0).mode).toBe('NO HAND');
    expect(c.frame(hand({ index: true }), 'Pointing_Up', 10).mode).toBe('IDLE');
    expect(log).toEqual([]);
  });

  it('two fingers move a smoothed, mirrored cursor', () => {
    const { log, out } = recorder();
    const c = new HandController(out, SCREEN);
    c.frame(hand(TWO, { at: { x: 0.3, y: 0.5 } }), 'Victory', 0);
    // x mirrored: camera 0.3 (minus the finger offset) → right half of the screen.
    const [, first] = log[0].split(' ');
    expect(Number(first.split(',')[0])).toBeGreaterThan(SCREEN.width / 2);
    c.frame(hand(TWO, { at: { x: 0.301, y: 0.5 } }), 'Victory', 33);
    c.frame(hand(TWO, { at: { x: 0.6, y: 0.5 } }), 'Victory', 66);
    expect(log.length).toBe(2); // tiny move swallowed by the deadzone
  });

  it('pinch clicks once per pinch, not every 400 ms while held', () => {
    const { log, out } = recorder();
    const c = new HandController(out, SCREEN);
    for (let t = 0; t < 2000; t += 33) c.frame(hand(TWO, { pinch: 'index' }), 'None', t);
    expect(log.filter((l) => l.startsWith('click'))).toEqual(['click left']);
    c.frame(hand(TWO), 'Victory', 2100); // fingers open
    c.frame(hand(TWO, { pinch: 'middle' }), 'None', 2600);
    expect(log.filter((l) => l.startsWith('click'))).toEqual(['click left', 'click right']);
  });

  it('respects the click cooldown even across quick re-pinches', () => {
    const { log, out } = recorder();
    const c = new HandController(out, SCREEN);
    c.frame(hand(TWO, { pinch: 'index' }), 'None', 0);
    c.frame(hand(TWO), 'Victory', 50);
    c.frame(hand(TWO, { pinch: 'index' }), 'None', 100);
    expect(log.filter((l) => l === 'click left')).toHaveLength(1);
    c.frame(hand(TWO), 'Victory', 200);
    c.frame(hand(TWO, { pinch: 'index' }), 'None', HAND.clickCooldownMs + 10);
    expect(log.filter((l) => l === 'click left')).toHaveLength(2);
  });

  it('three fingers scroll with the hand', () => {
    const { log, out } = recorder();
    const c = new HandController(out, SCREEN);
    const three = { index: true, middle: true, ring: true };
    c.frame(hand(three, { at: { x: 0.5, y: 0.5 } }), 'None', 0);
    c.frame(hand(three, { at: { x: 0.5, y: 0.47 } }), 'None', 33);
    c.frame(hand(three, { at: { x: 0.5, y: 0.53 } }), 'None', 66);
    expect(log).toEqual(['scroll up 30', 'scroll down 60']);
  });

  it('a fist grabs, drags after settling, and lets go', () => {
    const { log, out } = recorder();
    const c = new HandController(out, SCREEN);
    c.frame(hand(TWO, { at: { x: 0.5, y: 0.5 } }), 'Victory', 0);
    c.frame(hand({}, { at: { x: 0.5, y: 0.5 } }), 'Closed_Fist', 33);
    expect(log).toContain('left down');
    const moves = log.length;
    c.frame(hand({}, { at: { x: 0.4, y: 0.5 } }), 'Closed_Fist', 66); // still settling
    expect(log.length).toBe(moves);
    c.frame(hand({}, { at: { x: 0.4, y: 0.5 } }), 'Closed_Fist', 33 + HAND.dragSettleMs);
    expect(log[log.length - 1]).toMatch(/^move /);
    // A one-frame flicker open does not drop the item…
    c.frame(hand(TWO), 'None', 300);
    expect(log).not.toContain('left up');
    // …but a clear release does.
    for (let i = 0; i <= HAND.dragReleaseFrames; i += 1) c.frame(hand(TWO), 'None', 340 + i * 33);
    expect(log).toContain('left up');
  });

  it('losing the hand mid-drag releases the button', () => {
    const { log, out } = recorder();
    const c = new HandController(out, SCREEN);
    c.frame(hand({}), 'Closed_Fist', 0);
    c.frame(null, 'None', 33);
    expect(log).toEqual(['left down', 'left up']);
  });

  it('stop() releases a held drag', () => {
    const { log, out } = recorder();
    const c = new HandController(out, SCREEN);
    c.frame(hand({}), 'Closed_Fist', 0);
    c.stop();
    expect(log).toContain('left up');
  });

  it('maps with a margin so screen edges are reachable', () => {
    const c = new HandController(recorder().out, SCREEN);
    const topLeft = c.mapPoint({ x: 0.85, y: 0.15 });
    expect(topLeft.x).toBeCloseTo(0, 6);
    expect(topLeft.y).toBeCloseTo(0, 6);
    const bottomRight = c.mapPoint({ x: 0.15, y: 0.85 });
    expect(bottomRight.x).toBeCloseTo(SCREEN.width, 6);
    expect(bottomRight.y).toBeCloseTo(SCREEN.height, 6);
    expect(c.mapPoint({ x: 1.2, y: -1 })).toEqual({ x: 0, y: 0 });
  });
});
