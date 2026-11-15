// Blink liveness for Face Lock (Stonic port, Phase 4). Pure.
//
// A photo or a paused video does not blink. The detector learns this person's
// open-eye level (the baseline) over the first frames, then counts one blink
// when the eyes drop well below it and come back within a natural blink's
// duration — with the head held still, so turning away is not a "blink".

import type { Pose } from './geometry';

export class BlinkDetector {
  static readonly MAX_MS = 900;
  static readonly MIN_MS = 40;
  static readonly WARMUP = 6;
  /** Degrees the head may move during the blink. */
  static readonly POSE_SLACK = 8;

  baseline = 0;
  samples = 0;
  count = 0;
  private closedAt = 0;
  private poseAtClose: { yaw: number; pitch: number } | null = null;
  private wasOpen = false;

  /** How far below the baseline counts as closed (adapts to narrow eyes). */
  private drop(): number {
    return Math.max(0.3, this.baseline * 0.45);
  }

  isOpen(eyeL: number, eyeR: number): boolean {
    return this.samples >= BlinkDetector.WARMUP && Math.min(eyeL, eyeR) > this.baseline - 0.15;
  }

  isClosed(eyeL: number, eyeR: number): boolean {
    return this.samples >= BlinkDetector.WARMUP && Math.max(eyeL, eyeR) < this.baseline - this.drop();
  }

  /** Feed one frame. Returns true on the frame a blink completes. */
  update(eyeL: number, eyeR: number, now: number, pose: Pose | null): boolean {
    const open = Math.min(eyeL, eyeR);
    // The baseline rises quickly to a wider-open level and drifts slowly down,
    // but never follows the eyes as they close.
    if (this.samples === 0) this.baseline = open;
    else if (open > this.baseline) this.baseline += (open - this.baseline) * 0.5;
    else if (open > this.baseline - 0.12) this.baseline += (open - this.baseline) * 0.1;
    this.samples += 1;

    if (this.isClosed(eyeL, eyeR)) {
      if (this.wasOpen && !this.closedAt) {
        this.closedAt = now;
        this.poseAtClose = pose ? { yaw: pose.yaw, pitch: pose.pitch } : null;
      }
      return false;
    }
    if (!this.isOpen(eyeL, eyeR)) return false;

    let blinked = false;
    if (this.closedAt) {
      const held = now - this.closedAt;
      const before = this.poseAtClose;
      const still = !pose || !before
        || (Math.abs(pose.yaw - before.yaw) < BlinkDetector.POSE_SLACK
          && Math.abs(pose.pitch - before.pitch) < BlinkDetector.POSE_SLACK);
      blinked = held >= BlinkDetector.MIN_MS && held <= BlinkDetector.MAX_MS && still;
      if (blinked) this.count += 1;
    }
    this.closedAt = 0;
    this.poseAtClose = null;
    this.wasOpen = true;
    return blinked;
  }

  reset(): void {
    this.baseline = 0;
    this.samples = 0;
    this.count = 0;
    this.closedAt = 0;
    this.poseAtClose = null;
    this.wasOpen = false;
  }
}
