// Unlock and enrollment flows for Face Lock (Stonic port, Phase 4).
//
// Pure state machines driven one camera frame at a time, so the timing logic
// recovered from Stonic's renderer is tested with synthetic frames. Reading a
// face print (`embed`) is the only async step and is passed in.
//
// Neither flow decides anything: the unlock flow hands its probes to the
// backend (`/v1/security/facelock/verify-face`), and the enrollment flow
// hands its templates to `/enroll`. The thresholds that matter live there.

import { BlinkDetector } from './blink';
import { BASE_PITCH, dot, frameQuality, meanPrint, type Box, type FaceFrame, type QualityHint } from './geometry';

export type Embed = (frame: FaceFrame) => Promise<Float32Array | null>;

// ── Unlock ───────────────────────────────────────────────────────────────────

export const UNLOCK = {
  /** Face steady and well framed this long before liveness starts. */
  ACQUIRE_MS: 320,
  /** Minimum gap between face-print reads. */
  EMBED_GAP_MS: 150,
  /** Prints taken before the blink (the rest come after it). */
  PRE_BLINK_PROBES: 2,
  MAX_PROBES: 4,
  MIN_PROBES: 3,
  /** After the blink: send what we have once this long has passed. */
  SETTLE_MS: 4500,
  /** Face missing this long → start over. */
  LOST_MS: 450,
  /** Face jumped this far between frames → a different face, start over. */
  JUMP: 0.22,
} as const;

export type UnlockState = 'search' | 'acquire' | 'liveness' | 'capture' | 'verify';

export interface UnlockView {
  state: UnlockState;
  hint: QualityHint;
  probes: number;
  blinked: boolean;
}

export class UnlockFlow {
  state: UnlockState = 'search';
  hint: QualityHint = '';
  readonly blink = new BlinkDetector();
  private probes: Float32Array[] = [];
  private epoch = 0;
  private since = 0;
  private lastSeen = 0;
  private lastEmbed = 0;
  private lastBox: Pick<Box, 'cx' | 'cy'> | null = null;
  private busy = false;

  /** Called once with the probes when it is time to ask the backend. */
  constructor(private readonly onVerify: (probes: number[][]) => void) {}

  reset(): void {
    this.epoch += 1;
    this.probes = [];
    this.lastBox = null;
    this.busy = false;
    this.blink.reset();
    this.state = 'search';
  }

  view(): UnlockView {
    return { state: this.state, hint: this.hint, probes: this.probes.length, blinked: this.blink.count > 0 };
  }

  frame(f: FaceFrame, now: number, embed: Embed | null): UnlockView {
    if (this.state === 'verify' || !embed) return this.view();
    const quality = frameQuality(f);
    this.hint = quality.hint;

    if (f.present && f.box) {
      if (this.lastBox && this.state !== 'search'
        && Math.hypot(f.box.cx - this.lastBox.cx, f.box.cy - this.lastBox.cy) > UNLOCK.JUMP) {
        this.reset();
      }
      this.lastBox = { cx: f.box.cx, cy: f.box.cy };
      this.lastSeen = now;
    } else if (this.state !== 'search' && now - this.lastSeen > UNLOCK.LOST_MS) {
      this.reset();
    }

    const blinked = f.present && this.blink.update(f.eyeL, f.eyeR, now, f.pose);
    const open = this.blink.isOpen(f.eyeL, f.eyeR);

    if (this.state === 'search' && f.present) {
      this.since = now;
      this.state = 'acquire';
    } else if (this.state === 'acquire') {
      if (!quality.ok) this.since = now;
      else if (now - this.since > UNLOCK.ACQUIRE_MS) {
        this.blink.count = 0;
        this.state = 'liveness';
      }
    } else if (this.state === 'liveness' || this.state === 'capture') {
      if (this.state === 'liveness' && blinked) {
        this.since = now;
        this.state = 'capture';
      }
      const cap = this.state === 'capture' ? UNLOCK.MAX_PROBES : UNLOCK.PRE_BLINK_PROBES;
      if (quality.ok && open && f.landmarks && !this.busy && this.probes.length < cap
        && now - this.lastEmbed > UNLOCK.EMBED_GAP_MS) {
        this.read(f, now, embed);
      }
      if (this.state === 'capture' && !this.busy) {
        const enough = this.probes.length >= UNLOCK.MAX_PROBES
          || (this.probes.length >= UNLOCK.MIN_PROBES && now - this.since > UNLOCK.SETTLE_MS);
        if (enough) {
          this.state = 'verify';
          this.onVerify(this.probes.map((p) => Array.from(p)));
        } else if (now - this.since > UNLOCK.SETTLE_MS * 2) {
          this.reset();
        }
      }
    }
    return this.view();
  }

  private read(f: FaceFrame, now: number, embed: Embed): void {
    this.busy = true;
    this.lastEmbed = now;
    const epoch = this.epoch;
    void embed(f).then((print) => {
      this.busy = false;
      if (print && epoch === this.epoch) this.probes.push(print);
    }, () => { this.busy = false; });
  }
}

// ── Enrollment ──────────────────────────────────────────────────────────────

export const ENROLL = {
  SECTORS: 8,
  CENTRE_STEADY_MS: 450,
  CENTRE_SHOTS: 3,
  EMBED_GAP_MS: 150,
  /** Head turn (degrees, pitch scaled) that fills a direction. */
  MIN_TURN: 9,
  MAX_TURN: 34,
  PITCH_SCALE: 1.35,
  /** Accept after this many directions once ANGLES_MS has passed. */
  MIN_SECTORS: 6,
  ANGLES_MS: 22_000,
  /** An angle print this unlike the centre is someone else (or a bad read). */
  ANGLE_MIN_SIMILARITY: 0.3,
  /** After the blink, 3 fresh prints must match the new templates this well. */
  CONFIRM_MIN: 0.62,
  CONFIRM_PROBES: 3,
} as const;

export type EnrollState = 'search' | 'centre' | 'angles' | 'blink' | 'done' | 'failed';

export interface EnrollView {
  state: EnrollState;
  hint: string;
  centreShots: number;
  sectors: boolean[];
  /** Sector the head is pointing at now, −1 = none. */
  pointing: number;
  confirmScore: number | null;
  error: string;
}

export class EnrollFlow {
  state: EnrollState = 'search';
  hint = '';
  error = '';
  confirmScore: number | null = null;
  readonly blink = new BlinkDetector();
  private epoch = 0;
  private since = 0;
  private lastEmbed = 0;
  private busy = false;
  private shots: Float32Array[] = [];
  private shotPoses: Array<{ yaw: number; pitch: number }> = [];
  private centre: Float32Array | null = null;
  private base = { yaw: 0, pitch: BASE_PITCH };
  private sectors: Array<Float32Array | null> = Array(ENROLL.SECTORS).fill(null);
  private pointing = -1;
  private probes: Float32Array[] = [];

  reset(): void {
    this.epoch += 1;
    this.shots = [];
    this.shotPoses = [];
    this.centre = null;
    this.sectors = Array(ENROLL.SECTORS).fill(null);
    this.pointing = -1;
    this.probes = [];
    this.busy = false;
    this.blink.reset();
    this.confirmScore = null;
    this.error = '';
    this.state = 'search';
  }

  view(): EnrollView {
    return {
      state: this.state,
      hint: this.hint,
      centreShots: this.shots.length,
      sectors: this.sectors.map(Boolean),
      pointing: this.pointing,
      confirmScore: this.confirmScore,
      error: this.error,
    };
  }

  /** [centre, ...filled directions] — the straight-on print first, as the backend expects. */
  templates(): number[][] {
    if (!this.centre) return [];
    return [this.centre, ...this.sectors.filter((s): s is Float32Array => !!s)].map((t) => Array.from(t));
  }

  frame(f: FaceFrame, now: number, embed: Embed | null): EnrollView {
    if (!embed || this.state === 'done' || this.state === 'failed') return this.view();
    const live = this.state === 'centre' || this.state === 'angles' || this.state === 'blink';
    if (live && f.present) this.blink.update(f.eyeL, f.eyeR, now, f.pose);
    const open = this.blink.isOpen(f.eyeL, f.eyeR);

    if (this.state === 'search' && f.present) {
      this.since = now;
      this.state = 'centre';
    } else if (this.state === 'centre') {
      const q = frameQuality(f);
      const straight = f.pose
        ? Math.abs(f.pose.yaw) < 9 && Math.abs(f.pose.pitch - BASE_PITCH) < 12
        : false;
      this.hint = f.present ? (q.hint || (straight ? '' : 'Look straight at the camera')) : 'Step into view';
      if (!q.ok || !straight || !open) this.since = now;
      else if (now - this.since > ENROLL.CENTRE_STEADY_MS && this.shots.length < ENROLL.CENTRE_SHOTS) {
        const pose = f.pose!;
        this.read(f, now, embed, (print) => {
          this.shots.push(print);
          this.shotPoses.push({ yaw: pose.yaw, pitch: pose.pitch });
          if (this.shots.length < ENROLL.CENTRE_SHOTS) return;
          this.centre = meanPrint(this.shots);
          const n = this.shotPoses.length;
          this.base = {
            yaw: this.shotPoses.reduce((s, p) => s + p.yaw, 0) / n,
            pitch: this.shotPoses.reduce((s, p) => s + p.pitch, 0) / n,
          };
          this.since = now;
          this.state = 'angles';
        });
      }
    } else if (this.state === 'angles' && this.centre) {
      const filled = this.sectors.filter(Boolean).length;
      if (!f.present || !f.pose) {
        this.hint = 'Come back into view';
        this.pointing = -1;
      } else {
        const dyaw = f.pose.yaw - this.base.yaw;
        const dpitch = (f.pose.pitch - this.base.pitch) * ENROLL.PITCH_SCALE;
        const turn = Math.hypot(dyaw, dpitch);
        const step = (Math.PI * 2) / ENROLL.SECTORS;
        const sector = ((Math.round(Math.atan2(dpitch, dyaw) / step) % ENROLL.SECTORS) + ENROLL.SECTORS) % ENROLL.SECTORS;
        const inRange = turn >= ENROLL.MIN_TURN && turn <= ENROLL.MAX_TURN;
        this.pointing = inRange ? sector : -1;
        this.hint = f.span < 0.08 ? 'Move closer' : turn > ENROLL.MAX_TURN ? 'A little less — keep your eyes on the screen' : '';
        if (inRange && !this.sectors[sector] && f.span >= 0.08 && !this.blink.isClosed(f.eyeL, f.eyeR)) {
          const centre = this.centre;
          this.read(f, now, embed, (print) => {
            if (dot(print, centre) < ENROLL.ANGLE_MIN_SIMILARITY || this.sectors[sector]) return;
            this.sectors[sector] = print;
          });
        }
      }
      if (!this.busy && (filled === ENROLL.SECTORS || (filled >= ENROLL.MIN_SECTORS && now - this.since > ENROLL.ANGLES_MS))) {
        this.pointing = -1;
        this.since = now;
        this.blink.count = 0;
        this.state = 'blink';
      }
    } else if (this.state === 'blink' && this.centre) {
      const q = frameQuality(f);
      this.hint = f.present ? (this.blink.count > 0 ? q.hint : q.hint || 'Blink once, naturally') : 'Step into view';
      if (this.blink.count > 0 && q.ok && open && this.probes.length < ENROLL.CONFIRM_PROBES) {
        this.read(f, now, embed, (print) => {
          this.probes.push(print);
          if (this.probes.length < ENROLL.CONFIRM_PROBES) return;
          const refs = [this.centre!, ...this.sectors.filter((s): s is Float32Array => !!s)];
          const scores = this.probes
            .map((p) => Math.max(...refs.map((r) => dot(p, r))))
            .sort((a, b) => a - b);
          this.confirmScore = scores[1];
          if (scores[1] < ENROLL.CONFIRM_MIN) {
            this.error = 'The scan was not steady enough to rely on. Try again with more light on your face.';
            this.state = 'failed';
          } else {
            this.state = 'done';
          }
        });
      }
    }
    return this.view();
  }

  private read(f: FaceFrame, now: number, embed: Embed, use: (print: Float32Array) => void): void {
    if (this.busy || now - this.lastEmbed < ENROLL.EMBED_GAP_MS || !f.landmarks) return;
    this.busy = true;
    this.lastEmbed = now;
    const epoch = this.epoch;
    void embed(f).then((print) => {
      this.busy = false;
      if (print && epoch === this.epoch) use(print);
    }, () => { this.busy = false; });
  }
}

// ── PIN rules (mirrors ghost.security.facelock.is_valid_pin) ────────────────

const WEAK_PINS = new Set(['123456', '654321', '123123', '112233', '121212']);

export function pinProblem(pin: string, length = 6): string {
  if (!new RegExp(`^\\d{${length}}$`).test(pin)) return `Use exactly ${length} digits.`;
  if (/^(\d)\1+$/.test(pin) || WEAK_PINS.has(pin)) return 'That passcode is too easy to guess.';
  return '';
}
