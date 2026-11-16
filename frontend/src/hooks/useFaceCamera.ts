import { useEffect, useRef, useState, type RefObject } from 'react';
import { CameraError, FaceEngine, type CameraErrorKind } from '../lib/facelock/engine';
import type { Embed } from '../lib/facelock/flows';
import type { FaceFrame } from '../lib/facelock/geometry';

// Models load once per app session: a re-lock must not wait for 40 MB of
// SFace to be parsed again.
let shared: Promise<FaceEngine> | null = null;

function engine(): Promise<FaceEngine> {
  if (!shared) {
    const e = new FaceEngine();
    shared = e.load().then(() => e, (err) => { shared = null; throw err; });
  }
  return shared;
}

export type FaceCameraPhase = 'loading' | 'ready' | 'error';

/**
 * Camera + face mesh for a Face Lock screen. Calls `onFrame` on every new
 * video frame while `active`; `embed` is null until the models are ready.
 */
export function useFaceCamera(
  videoRef: RefObject<HTMLVideoElement | null>,
  active: boolean,
  onFrame: (frame: FaceFrame, now: number, embed: Embed | null) => void,
) {
  const [phase, setPhase] = useState<FaceCameraPhase>('loading');
  const [cameraError, setCameraError] = useState<CameraErrorKind | null>(null);
  const [modelsError, setModelsError] = useState('');
  const [retry, setRetry] = useState(0);
  const frameRef = useRef(onFrame);
  frameRef.current = onFrame;

  useEffect(() => {
    if (!active) return;
    let live = true;
    let raf = 0;
    let eng: FaceEngine | null = null;
    setPhase('loading');
    setCameraError(null);
    setModelsError('');
    void (async () => {
      try {
        eng = await engine();
      } catch (e) {
        if (live) { setModelsError(e instanceof Error ? e.message : String(e)); setPhase('error'); }
        return;
      }
      const video = videoRef.current;
      if (!live || !video) return;
      try {
        await eng.startCamera(video);
      } catch (e) {
        if (live) { setCameraError(e instanceof CameraError ? e.kind : 'failed'); setPhase('error'); }
        return;
      }
      if (!live) { eng.stopCamera(); return; }
      setPhase('ready');
      const embed: Embed = (f) => eng!.embed(f);
      const tick = () => {
        if (!live || !eng) return;
        const now = performance.now();
        frameRef.current(eng.detect(now), now, eng.ready ? embed : null);
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
    })();
    return () => {
      live = false;
      cancelAnimationFrame(raf);
      eng?.stopCamera();
    };
  }, [active, retry, videoRef]);

  return { phase, cameraError, modelsError, retryCamera: () => setRetry((n) => n + 1) };
}
