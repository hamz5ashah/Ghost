// Face geometry for Face Lock (Stonic port, Phase 4). Pure, so it is tested
// without a camera. Ported from Stonic's renderer (recovered from its build):
// the landmark map, the 5-point alignment to SFace's 112×112 crop, head pose,
// and the framing checks that decide when a frame is good enough to read.

/** MediaPipe Face Mesh (478 points incl. irises) indices Face Lock uses. */
export const LM = {
  noseTip: 1,
  forehead: 10,
  chin: 152,
  eyeOuterA: 33,
  eyeOuterB: 263,
  mouthA: 61,
  mouthB: 291,
  irisA: 468,
  irisB: 473,
} as const;

export const CROP = 112;

/** ArcFace / SFace reference points (px in the 112×112 crop). */
export const ARCFACE_TEMPLATE: ReadonlyArray<readonly [number, number]> = [
  [38.2946, 51.6963],
  [73.5318, 51.5014],
  [56.0252, 71.7366],
  [41.5493, 92.3655],
  [70.7299, 92.2041],
];

/** Normal sitting pitch: people look slightly down at a laptop camera. */
export const BASE_PITCH = 6;

export interface Point { x: number; y: number; z?: number }
export interface Box { cx: number; cy: number; w: number; h: number }
export interface Pose { yaw: number; pitch: number; roll: number }
export interface Similarity { a: number; b: number; tx: number; ty: number }

const DEG = 180 / Math.PI;

/** Normalised bounding box of the 468 mesh points (irises excluded). */
export function faceBox(lm: ReadonlyArray<Point>): Box {
  let x0 = 1, y0 = 1, x1 = 0, y1 = 0;
  for (let i = 0; i < 468; i += 1) {
    const p = lm[i];
    if (p.x < x0) x0 = p.x;
    if (p.x > x1) x1 = p.x;
    if (p.y < y0) y0 = p.y;
    if (p.y > y1) y1 = p.y;
  }
  return { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, w: x1 - x0, h: y1 - y0 };
}

/** Head pose in degrees from the eye corners and forehead→chin line. */
export function headPose(lm: ReadonlyArray<Point>, width: number, height: number): Pose {
  const a = lm[LM.eyeOuterA], b = lm[LM.eyeOuterB], top = lm[LM.forehead], bottom = lm[LM.chin];
  const dx = (b.x - a.x) * width;
  const dy = (b.y - a.y) * height;
  const dz = ((b.z ?? 0) - (a.z ?? 0)) * width;
  const vy = (bottom.y - top.y) * height;
  const vz = ((bottom.z ?? 0) - (top.z ?? 0)) * width;
  return {
    yaw: -Math.atan2(dz, Math.abs(dx) || 1e-6) * DEG,
    pitch: Math.atan2(vz, Math.abs(vy) || 1e-6) * DEG,
    roll: -Math.atan2(dy, dx || 1e-6) * DEG,
  };
}

/** Face size as a fraction of frame width (iris to far eye corner). */
export function eyeSpan(lm: ReadonlyArray<Point>, width: number, height: number): number {
  const a = lm[LM.irisA], b = lm[LM.eyeOuterB];
  return Math.hypot((b.x - a.x) * width, (b.y - a.y) * height) / width;
}

/** The 5 alignment points in px: eyes (left→right), nose, mouth (left→right). */
export function fivePoints(lm: ReadonlyArray<Point>, width: number, height: number): Array<[number, number]> {
  const px = (i: number): [number, number] => [lm[i].x * width, lm[i].y * height];
  const byX = (p: [number, number], q: [number, number]) => p[0] - q[0];
  const eyes = [px(LM.irisA), px(LM.irisB)].sort(byX);
  const mouth = [px(LM.mouthA), px(LM.mouthB)].sort(byX);
  return [eyes[0], eyes[1], px(LM.noseTip), mouth[0], mouth[1]];
}

/**
 * Least-squares 2-D similarity (rotation + uniform scale + shift) taking
 * `src` onto `dst`: x' = a·x − b·y + tx, y' = b·x + a·y + ty.
 */
export function similarityTransform(
  src: ReadonlyArray<readonly [number, number]>,
  dst: ReadonlyArray<readonly [number, number]>,
): Similarity {
  const n = src.length;
  let sx = 0, sy = 0, dx = 0, dy = 0;
  for (let i = 0; i < n; i += 1) { sx += src[i][0]; sy += src[i][1]; dx += dst[i][0]; dy += dst[i][1]; }
  sx /= n; sy /= n; dx /= n; dy /= n;
  let num = 0, cross = 0, den = 0;
  for (let i = 0; i < n; i += 1) {
    const px = src[i][0] - sx, py = src[i][1] - sy;
    const qx = dst[i][0] - dx, qy = dst[i][1] - dy;
    num += px * qx + py * qy;
    cross += px * qy - py * qx;
    den += px * px + py * py;
  }
  if (den < 1e-9) return { a: 1, b: 0, tx: dx - sx, ty: dy - sy };
  const a = num / den, b = cross / den;
  return { a, b, tx: dx - (a * sx - b * sy), ty: dy - (b * sx + a * sy) };
}

export function l2normalize(v: ArrayLike<number>): Float32Array {
  const out = new Float32Array(v.length);
  let sum = 0;
  for (let i = 0; i < v.length; i += 1) sum += v[i] * v[i];
  const norm = Math.sqrt(sum) || 1;
  for (let i = 0; i < v.length; i += 1) out[i] = v[i] / norm;
  return out;
}

export function dot(a: ArrayLike<number>, b: ArrayLike<number>): number {
  let s = 0;
  for (let i = 0; i < a.length; i += 1) s += a[i] * b[i];
  return s;
}

/** Mean of unit vectors, renormalised: the "centre" print in enrollment. */
export function meanPrint(prints: ReadonlyArray<ArrayLike<number>>): Float32Array {
  const acc = new Float32Array(prints[0].length);
  for (const p of prints) for (let i = 0; i < acc.length; i += 1) acc[i] += p[i];
  return l2normalize(acc);
}

/** What the camera saw in one frame. `luma` is 0–255, or −1 when not measured. */
export interface FaceFrame {
  t: number;
  present: boolean;
  box: Box | null;
  pose: Pose | null;
  /** Eye openness 0–1 (1 − MediaPipe's eyeBlink blendshape). */
  eyeL: number;
  eyeR: number;
  span: number;
  luma: number;
  /** The mesh, for the engine's alignment step. */
  landmarks: ReadonlyArray<Point> | null;
}

export const NO_FACE: FaceFrame = Object.freeze({
  t: 0, present: false, box: null, pose: null, eyeL: 0, eyeR: 0, span: 0, luma: -1, landmarks: null,
}) as FaceFrame;

export type QualityHint =
  | ''
  | 'Move closer'
  | 'Move back a little'
  | 'Centre your face in the ring'
  | 'Face the camera'
  | 'More light on your face'
  | 'Too bright — turn away from the light';

/** Is this frame good enough to read? Stonic's thresholds, unchanged. */
export function frameQuality(f: FaceFrame): { ok: boolean; hint: QualityHint } {
  if (!f.present || !f.box || !f.pose) return { ok: false, hint: '' };
  if (f.span < 0.085) return { ok: false, hint: 'Move closer' };
  if (f.span > 0.3) return { ok: false, hint: 'Move back a little' };
  if (Math.abs(f.box.cx - 0.5) > 0.2 || Math.abs(f.box.cy - 0.5) > 0.23) {
    return { ok: false, hint: 'Centre your face in the ring' };
  }
  if (Math.abs(f.pose.yaw) > 20 || Math.abs(f.pose.pitch - BASE_PITCH) > 22 || Math.abs(f.pose.roll) > 20) {
    return { ok: false, hint: 'Face the camera' };
  }
  if (f.luma >= 0 && f.luma < 42) return { ok: false, hint: 'More light on your face' };
  if (f.luma > 238) return { ok: false, hint: 'Too bright — turn away from the light' };
  return { ok: true, hint: '' };
}
