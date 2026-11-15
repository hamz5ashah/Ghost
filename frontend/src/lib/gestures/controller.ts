// Hand control (Stonic port, Phase 4). Pure: hand landmarks in, pointer
// actions out, so every gesture is tested without a camera or a mouse.
//
// Ported from Stonic's gesture controller (recovered from its build), with
// its tuning unchanged:
//
//   two fingers up (index + middle) .... move the cursor
//     + thumb touches index tip ........ left click
//     + thumb touches middle tip ....... right click
//   three fingers up ................... scroll (move the hand up / down)
//   fist ............................... grab and drag
//   anything else / no hand ............ nothing happens
//
// One change from Stonic: a pinch clicks once per pinch. Stonic re-clicked
// every 400 ms while the pinch was held, which turns a slow pinch into a
// double-click; here the fingers must open before the next click.

export interface HandPoint { x: number; y: number; z?: number }

/** Pointer input, carried out by the desktop shell (src-tauri/src/hand_control.rs). */
export interface PointerOutput {
  move(x: number, y: number): void;
  button(button: 'left', down: boolean): void;
  click(button: 'left' | 'right'): void;
  scroll(direction: 'up' | 'down', amount: number): void;
}

export const HAND = {
  activeMargin: 0.15,
  cursorSmoothing: 0.4,
  moveDeadzone: 2,
  clickPinch: 0.5,
  clickCooldownMs: 400,
  scrollStep: 0.01,
  scrollAmount: 10,
  scrollMaxAmount: 80,
  dragSettleMs: 140,
  dragReleaseFrames: 5,
} as const;

// MediaPipe hand landmark indices.
const WRIST = 0, THUMB_TIP = 4, INDEX_PIP = 6, INDEX_TIP = 8, MIDDLE_MCP = 9;
const MIDDLE_PIP = 10, MIDDLE_TIP = 12, RING_PIP = 14, RING_TIP = 16, PINKY_PIP = 18, PINKY_TIP = 20;

export type HandMode = 'NO HAND' | 'IDLE' | 'MOVE' | 'SCROLL' | 'DRAG';

export interface HandTelemetry {
  mode: HandMode;
  pose: string;
  action: string;
  pinch: number;
}

const dist = (a: HandPoint, b: HandPoint) => Math.hypot(a.x - b.x, a.y - b.y);
const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);

export class HandController {
  mode: HandMode = 'NO HAND';
  pose = 'NONE';
  lastAction = '';
  lastPinch = 1;
  private smoothX = 0;
  private smoothY = 0;
  private hasCursor = false;
  private lastSentX = -999;
  private lastSentY = -999;
  private dragging = false;
  private dragOffsetX = 0;
  private dragOffsetY = 0;
  private dragStartedAt = 0;
  private fistMissFrames = 0;
  private lastClick = { left: -Infinity, right: -Infinity };
  private pinchArmed = true;
  private lastScrollY: number | null = null;
  private scrollAccum = 0;

  constructor(
    private readonly out: PointerOutput,
    private screen: { width: number; height: number },
  ) {}

  setScreen(screen: { width: number; height: number }): void {
    this.screen = screen;
  }

  telemetry(): HandTelemetry {
    return { mode: this.mode, pose: this.pose, action: this.lastAction, pinch: this.lastPinch };
  }

  /** One video frame. `hand` = 21 landmarks or null; `gesture` = MediaPipe's label. */
  frame(hand: ReadonlyArray<HandPoint> | null, gesture: string, now: number): HandTelemetry {
    if (!hand || hand.length < 21) {
      this.resetHand();
      this.mode = 'NO HAND';
      this.pose = 'NONE';
      return this.telemetry();
    }
    this.process(hand, gesture, now);
    return this.telemetry();
  }

  /** Stop: let go of anything held. */
  stop(): void {
    this.resetHand();
    this.mode = 'NO HAND';
  }

  private process(t: ReadonlyArray<HandPoint>, gesture: string, now: number): void {
    const scale = dist(t[WRIST], t[MIDDLE_MCP]) || 1e-4;
    const pinchIndex = dist(t[THUMB_TIP], t[INDEX_TIP]) / scale;
    const pinchMiddle = dist(t[THUMB_TIP], t[MIDDLE_TIP]) / scale;
    this.lastPinch = Math.min(pinchIndex, pinchMiddle);
    const up = {
      index: t[INDEX_TIP].y < t[INDEX_PIP].y,
      middle: t[MIDDLE_TIP].y < t[MIDDLE_PIP].y,
      ring: t[RING_TIP].y < t[RING_PIP].y,
      pinky: t[PINKY_TIP].y < t[PINKY_PIP].y,
    };

    // Fist: grab where the cursor is and drag.
    if (gesture === 'Closed_Fist' || (!up.index && !up.middle && !up.ring && !up.pinky)) {
      this.fistMissFrames = 0;
      this.mode = 'DRAG';
      this.pose = 'FIST';
      const p = this.mapPoint(t[MIDDLE_MCP]);
      if (!this.dragging) {
        this.dragging = true;
        this.dragStartedAt = now;
        this.dragOffsetX = this.hasCursor ? this.smoothX - p.x : 0;
        this.dragOffsetY = this.hasCursor ? this.smoothY - p.y : 0;
        this.out.button('left', true);
        this.lastAction = 'grab';
      }
      // Let the press settle before moving, or the drag starts with a jerk.
      if (now - this.dragStartedAt >= HAND.dragSettleMs) this.applyTarget(p.x + this.dragOffsetX, p.y + this.dragOffsetY);
      this.resetScroll();
      return;
    }
    // A fist that flickers open for a frame or two is still a fist.
    if (this.dragging && this.fistMissFrames < HAND.dragReleaseFrames) {
      this.fistMissFrames += 1;
      this.mode = 'DRAG';
      this.pose = 'FIST?';
      this.resetScroll();
      return;
    }
    this.releaseDrag();

    if (up.index && up.middle && up.ring) {
      this.mode = 'SCROLL';
      this.pose = 'THREE-FINGER';
      this.handleScroll(t[INDEX_TIP].y);
      return;
    }
    this.resetScroll();

    if (up.index && up.middle) {
      this.mode = 'MOVE';
      const left = pinchIndex < HAND.clickPinch;
      const right = pinchMiddle < HAND.clickPinch;
      if (left || right) {
        const button = pinchIndex <= pinchMiddle ? 'left' : 'right';
        this.pose = button === 'left' ? 'PINCH (L)' : 'PINCH (R)';
        this.tryClick(button, now);
      } else {
        this.pinchArmed = true;
        this.pose = 'TWO-FINGER';
        this.moveTo({ x: (t[INDEX_TIP].x + t[MIDDLE_TIP].x) / 2, y: (t[INDEX_TIP].y + t[MIDDLE_TIP].y) / 2 });
      }
      return;
    }
    this.pinchArmed = true;
    this.mode = 'IDLE';
    this.pose = 'NONE';
  }

  private tryClick(button: 'left' | 'right', now: number): void {
    if (!this.pinchArmed || now - this.lastClick[button] < HAND.clickCooldownMs) return;
    this.pinchArmed = false;
    this.lastClick[button] = now;
    this.out.click(button);
    this.lastAction = `${button} click`;
  }

  private releaseDrag(): void {
    if (this.dragging) {
      this.dragging = false;
      this.out.button('left', false);
      this.lastAction = 'drop';
    }
    this.fistMissFrames = 0;
  }

  /** Camera space → screen. Mirrored (the camera faces you) with a margin, so
   * the screen edges are reachable without the hand leaving the frame. */
  mapPoint(p: HandPoint): { x: number; y: number } {
    const m = HAND.activeMargin;
    return {
      x: clamp01((1 - p.x - m) / (1 - 2 * m)) * this.screen.width,
      y: clamp01((p.y - m) / (1 - 2 * m)) * this.screen.height,
    };
  }

  private moveTo(p: HandPoint): void {
    const target = this.mapPoint(p);
    this.applyTarget(target.x, target.y);
  }

  private applyTarget(x: number, y: number): void {
    if (!this.hasCursor) {
      this.smoothX = x;
      this.smoothY = y;
      this.hasCursor = true;
    } else {
      this.smoothX += (x - this.smoothX) * HAND.cursorSmoothing;
      this.smoothY += (y - this.smoothY) * HAND.cursorSmoothing;
    }
    if (Math.abs(this.smoothX - this.lastSentX) < HAND.moveDeadzone && Math.abs(this.smoothY - this.lastSentY) < HAND.moveDeadzone) return;
    this.lastSentX = this.smoothX;
    this.lastSentY = this.smoothY;
    this.out.move(Math.round(this.smoothX), Math.round(this.smoothY));
  }

  private handleScroll(y: number): void {
    if (this.lastScrollY === null) {
      this.lastScrollY = y;
      return;
    }
    this.scrollAccum += y - this.lastScrollY;
    this.lastScrollY = y;
    if (Math.abs(this.scrollAccum) >= HAND.scrollStep) {
      const direction = this.scrollAccum < 0 ? 'up' : 'down';
      const steps = Math.abs(this.scrollAccum) / HAND.scrollStep;
      this.out.scroll(direction, Math.min(HAND.scrollMaxAmount, Math.round(HAND.scrollAmount * steps)));
      this.lastAction = `scroll ${direction}`;
      this.scrollAccum = 0;
    }
  }

  private resetScroll(): void {
    this.lastScrollY = null;
    this.scrollAccum = 0;
  }

  private resetHand(): void {
    this.resetScroll();
    this.hasCursor = false;
    this.pinchArmed = true;
    this.releaseDrag();
  }
}
