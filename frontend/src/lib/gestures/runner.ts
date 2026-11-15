// Hand control runtime: webcam → MediaPipe gesture recognizer → HandController
// → the desktop shell's pointer commands. Desktop only.

import type { GestureRecognizer as GestureRecognizerT } from '@mediapipe/tasks-vision';
import { FACELOCK_LOCKED_EVENT } from '../facelock/api';
import { assetsBase, mediapipeFileset, openCamera } from '../facelock/engine';
import { HandController, type HandTelemetry, type PointerOutput } from './controller';

async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke: tauriInvoke } = await import('@tauri-apps/api/core');
  return tauriInvoke<T>(cmd, args);
}

/** Pointer output through Tauri. Errors are dropped: a refused move is harmless. */
export const tauriPointer: PointerOutput = {
  move: (x, y) => { void invoke('hand_mouse_move', { x, y }).catch(() => {}); },
  button: (button, down) => { void invoke('hand_mouse_button', { button, down }).catch(() => {}); },
  click: (button) => { void invoke('hand_mouse_click', { button }).catch(() => {}); },
  scroll: (direction, amount) => { void invoke('hand_scroll', { direction, amount }).catch(() => {}); },
};

export interface HandFrame extends HandTelemetry {
  landmarks: Array<{ x: number; y: number }> | null;
  fps: number;
}

export class HandControlRunner {
  private recognizer: GestureRecognizerT | null = null;
  private stream: MediaStream | null = null;
  private video: HTMLVideoElement | null = null;
  private raf = 0;
  private lastVideoTime = -1;
  private lastStamp = 0;
  private times: number[] = [];
  readonly controller: HandController;
  running = false;
  onFrame: (f: HandFrame) => void = () => {};

  constructor(private readonly out: PointerOutput = tauriPointer) {
    this.controller = new HandController(out, { width: 1920, height: 1080 });
  }

  async start(video: HTMLVideoElement = document.createElement('video')): Promise<void> {
    if (this.running) return;
    const size = await invoke<{ supported: boolean; width: number; height: number }>('hand_screen_size');
    if (!size.supported) throw new Error('Hand control needs Windows.');
    this.controller.setScreen(size);
    if (!this.recognizer) {
      const { GestureRecognizer } = await import('@mediapipe/tasks-vision');
      const options = (delegate: 'GPU' | 'CPU') => ({
        baseOptions: { modelAssetPath: `${assetsBase()}/gesture_recognizer.task`, delegate },
        runningMode: 'VIDEO' as const,
        numHands: 1,
      });
      try {
        this.recognizer = await GestureRecognizer.createFromOptions(mediapipeFileset(), options('GPU'));
      } catch {
        this.recognizer = await GestureRecognizer.createFromOptions(mediapipeFileset(), options('CPU'));
      }
    }
    this.stream = await openCamera(video);
    this.video = video;
    this.running = true;
    window.addEventListener('keydown', this.onKey);
    window.addEventListener(FACELOCK_LOCKED_EVENT, this.onLocked);
    this.notify();
    this.loop();
  }

  /** Esc in Ghost's window stops hand control at once. */
  private onKey = (e: KeyboardEvent): void => { if (e.key === 'Escape') this.stop(); };
  /** A locked app takes no pointer input from the camera. */
  private onLocked = (): void => this.stop();

  private listeners = new Set<() => void>();
  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }
  private notify(): void { this.listeners.forEach((fn) => fn()); }

  stop(): void {
    if (!this.running && !this.stream) return;
    window.removeEventListener('keydown', this.onKey);
    window.removeEventListener(FACELOCK_LOCKED_EVENT, this.onLocked);
    this.running = false;
    cancelAnimationFrame(this.raf);
    this.controller.stop();
    void invoke('hand_release').catch(() => {});
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    if (this.video) this.video.srcObject = null;
    this.video = null;
    this.notify();
  }

  private loop = (): void => {
    if (!this.running || !this.recognizer || !this.video) return;
    this.raf = requestAnimationFrame(this.loop);
    const v = this.video;
    if (v.readyState < 2 || v.currentTime === this.lastVideoTime) return;
    this.lastVideoTime = v.currentTime;
    const now = performance.now();
    this.lastStamp = Math.max(now, this.lastStamp + 1);
    let result;
    try {
      result = this.recognizer.recognizeForVideo(v, this.lastStamp);
    } catch {
      return;
    }
    this.times.push(now);
    if (this.times.length > 30) this.times.shift();
    const span = this.times[this.times.length - 1] - this.times[0];
    const fps = this.times.length > 1 && span > 0 ? Math.round(((this.times.length - 1) * 1000) / span) : 0;
    const hand = result.landmarks?.[0] ?? null;
    const gesture = result.gestures?.[0]?.[0]?.categoryName ?? 'None';
    const t = this.controller.frame(hand, gesture, now);
    this.onFrame({ ...t, landmarks: hand, fps });
  };
}

let shared: HandControlRunner | null = null;
/** One runner per app: hand control keeps working when Settings is closed. */
export function handControl(): HandControlRunner {
  if (!shared) shared = new HandControlRunner();
  return shared;
}
