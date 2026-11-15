// Face Lock camera engine (Stonic port, Phase 4).
//
// Runs in the WebView, next to the camera: MediaPipe's face mesh (478 points +
// blink blendshapes) and SFace (the 128-number face print) via ONNX Runtime.
// It only *measures*; the backend decides. No frame or picture is stored.
//
// The heavy files (models and WASM runtimes, ~68 MB) are downloaded once on
// demand by the backend into <config>/models/facelock and served, hash-checked,
// from /facelock-assets/. Only MediaPipe's small loader script ships with the
// app, because the WebView's CSP allows scripts from the app's own origin only.

import type { FaceLandmarker as FaceLandmarkerT } from '@mediapipe/tasks-vision';
import { getBase } from '../api';
import {
  ARCFACE_TEMPLATE,
  CROP,
  NO_FACE,
  eyeSpan,
  faceBox,
  fivePoints,
  headPose,
  l2normalize,
  similarityTransform,
  type Box,
  type FaceFrame,
  type Point,
} from './geometry';

export const EMBEDDING_DIM = 128;

/** Where the downloaded, hash-verified model files are served. */
export const assetsBase = (): string => `${getBase()}/facelock-assets`;
/** MediaPipe's loader script, shipped with the app (see vite.config.ts). */
export const MEDIAPIPE_LOADER = '/facelock/vision_wasm_internal.js';

export function mediapipeFileset() {
  return {
    wasmLoaderPath: MEDIAPIPE_LOADER,
    wasmBinaryPath: `${assetsBase()}/vision_wasm_internal.wasm`,
  };
}

export type CameraErrorKind = 'denied' | 'missing' | 'busy' | 'failed';

export class CameraError extends Error {
  constructor(readonly kind: CameraErrorKind) {
    super(`camera ${kind}`);
  }
}

export function classifyCameraError(err: unknown): CameraErrorKind {
  const name = String((err as { name?: string })?.name || '');
  if (/NotAllowed|Security/i.test(name)) return 'denied';
  if (/NotFound|Overconstrained|DevicesNotFound/i.test(name)) return 'missing';
  if (/NotReadable|TrackStart|Abort/i.test(name)) return 'busy';
  return 'failed';
}

/** Open the webcam (640×480 is plenty and keeps the mesh fast). */
export async function openCamera(video: HTMLVideoElement, deviceId?: string): Promise<MediaStream> {
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: deviceId
        ? { deviceId: { exact: deviceId }, width: { ideal: 640 }, height: { ideal: 480 } }
        : { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: 'user' },
      audio: false,
    });
  } catch (err) {
    throw new CameraError(classifyCameraError(err));
  }
  video.srcObject = stream;
  video.muted = true;
  video.playsInline = true;
  try { await video.play(); } catch { /* autoplay is allowed for muted video */ }
  return stream;
}

type Ort = typeof import('onnxruntime-web/wasm');

export class FaceEngine {
  private landmarker: FaceLandmarkerT | null = null;
  private ort: Ort | null = null;
  private session: import('onnxruntime-web/wasm').InferenceSession | null = null;
  private video: HTMLVideoElement | null = null;
  private stream: MediaStream | null = null;
  private crop = document.createElement('canvas');
  private cropCtx: CanvasRenderingContext2D;
  private probe = document.createElement('canvas');
  private probeCtx: CanvasRenderingContext2D;
  private input = new Float32Array(3 * CROP * CROP);
  private lastVideoTime = -1;
  private lastStamp = 0;
  private frameIndex = 0;
  private luma = -1;
  private embedding = false;
  last: FaceFrame = NO_FACE;

  constructor() {
    this.crop.width = this.crop.height = CROP;
    this.cropCtx = this.crop.getContext('2d', { willReadFrequently: true })!;
    this.probe.width = this.probe.height = 16;
    this.probeCtx = this.probe.getContext('2d', { willReadFrequently: true })!;
  }

  get ready(): boolean {
    return !!(this.landmarker && this.session);
  }

  /** Load both models. GPU first for the mesh, CPU if the GPU path fails. */
  async load(): Promise<void> {
    const base = assetsBase();
    const mesh = async () => {
      const { FaceLandmarker } = await import('@mediapipe/tasks-vision');
      const options = (delegate: 'GPU' | 'CPU') => ({
        baseOptions: { modelAssetPath: `${base}/face_landmarker.task`, delegate },
        runningMode: 'VIDEO' as const,
        numFaces: 1,
        outputFaceBlendshapes: true,
      });
      try {
        this.landmarker = await FaceLandmarker.createFromOptions(mediapipeFileset(), options('GPU'));
      } catch {
        this.landmarker = await FaceLandmarker.createFromOptions(mediapipeFileset(), options('CPU'));
      }
    };
    const identity = async () => {
      const ort = await import('onnxruntime-web/wasm');
      ort.env.wasm.wasmPaths = { wasm: `${base}/ort-wasm-simd-threaded.wasm` };
      ort.env.wasm.numThreads = 1;
      ort.env.wasm.proxy = false;
      this.ort = ort;
      this.session = await ort.InferenceSession.create(`${base}/sface.onnx`, {
        executionProviders: ['wasm'],
        graphOptimizationLevel: 'all',
      });
    };
    await Promise.all([mesh(), identity()]);
  }

  async startCamera(video: HTMLVideoElement, deviceId?: string): Promise<void> {
    this.stopCamera();
    this.stream = await openCamera(video, deviceId);
    this.video = video;
    this.lastVideoTime = -1;
  }

  stopCamera(): void {
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    if (this.video) {
      try { this.video.srcObject = null; } catch { /* already detached */ }
    }
    this.video = null;
    this.last = NO_FACE;
  }

  /** MediaPipe needs strictly increasing timestamps. */
  private stamp(): number {
    this.lastStamp = Math.max(performance.now(), this.lastStamp + 1);
    return this.lastStamp;
  }

  /** Read the current video frame (only when it changed). */
  detect(now: number): FaceFrame {
    const v = this.video;
    if (!this.landmarker || !v || v.readyState < 2 || !v.videoWidth) return this.last;
    if (v.currentTime === this.lastVideoTime) return this.last;
    this.lastVideoTime = v.currentTime;
    let result;
    try {
      result = this.landmarker.detectForVideo(v, this.stamp());
    } catch {
      return this.last;
    }
    const lm = result.faceLandmarks?.[0] as Point[] | undefined;
    if (!lm || lm.length < 478) {
      this.luma = -1;
      this.last = { ...NO_FACE, t: now };
      return this.last;
    }
    const w = v.videoWidth, h = v.videoHeight;
    const box = faceBox(lm);
    let blinkL = 0, blinkR = 0;
    for (const c of result.faceBlendshapes?.[0]?.categories ?? []) {
      if (c.categoryName === 'eyeBlinkLeft') blinkL = c.score;
      else if (c.categoryName === 'eyeBlinkRight') blinkR = c.score;
    }
    this.frameIndex += 1;
    if (this.frameIndex % 8 === 1) this.luma = this.measureLuma(v, box, w, h);
    this.last = {
      t: now,
      present: true,
      box,
      pose: headPose(lm, w, h),
      eyeL: 1 - blinkL,
      eyeR: 1 - blinkR,
      span: eyeSpan(lm, w, h),
      luma: this.luma,
      landmarks: lm,
    };
    return this.last;
  }

  /** Align the face to SFace's 112×112 crop and read its 128-number print. */
  async embed(frame: FaceFrame): Promise<Float32Array | null> {
    const v = this.video;
    if (!this.session || !this.ort || !v || !frame.landmarks || this.embedding) return null;
    this.embedding = true;
    try {
      const w = v.videoWidth, h = v.videoHeight;
      const m = similarityTransform(fivePoints(frame.landmarks, w, h), ARCFACE_TEMPLATE);
      const ctx = this.cropCtx;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, CROP, CROP);
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'high';
      ctx.setTransform(m.a, m.b, -m.b, m.a, m.tx, m.ty);
      ctx.drawImage(v, 0, 0, w, h);
      const { data } = ctx.getImageData(0, 0, CROP, CROP);
      const n = CROP * CROP;
      // Planar RGB, 0–255: what OpenCV's FaceRecognizerSF feeds this model.
      for (let i = 0, j = 0; i < n; i += 1, j += 4) {
        this.input[i] = data[j];
        this.input[n + i] = data[j + 1];
        this.input[2 * n + i] = data[j + 2];
      }
      const session = this.session;
      const tensor = new this.ort.Tensor('float32', this.input, [1, 3, CROP, CROP]);
      const out = await session.run({ [session.inputNames[0]]: tensor });
      const raw = out[session.outputNames[0]]?.data as Float32Array | undefined;
      return raw && raw.length === EMBEDDING_DIM ? l2normalize(raw) : null;
    } catch {
      return null;
    } finally {
      this.embedding = false;
    }
  }

  /** Mean brightness (0–255) of the middle of the face, on a 16×16 sample. */
  private measureLuma(v: HTMLVideoElement, box: Box, w: number, h: number): number {
    try {
      const x = Math.max(0, (box.cx - box.w * 0.3) * w);
      const y = Math.max(0, (box.cy - box.h * 0.3) * h);
      const cw = Math.min(w - x, box.w * 0.6 * w);
      const ch = Math.min(h - y, box.h * 0.6 * h);
      if (cw < 4 || ch < 4) return -1;
      this.probeCtx.drawImage(v, x, y, cw, ch, 0, 0, 16, 16);
      const { data } = this.probeCtx.getImageData(0, 0, 16, 16);
      let sum = 0;
      for (let i = 0; i < data.length; i += 4) sum += 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
      return sum / (data.length / 4);
    } catch {
      return -1;
    }
  }

  close(): void {
    this.stopCamera();
    try { this.landmarker?.close(); } catch { /* already closed */ }
    this.landmarker = null;
    void this.session?.release?.();
    this.session = null;
  }
}
