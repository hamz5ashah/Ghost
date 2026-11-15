import { useCallback, useEffect, useRef, useState } from 'react';
import { KeyRound, Loader2, ScanFace } from 'lucide-react';
import { useFaceCamera } from '../../hooks/useFaceCamera';
import { UnlockFlow, type UnlockView } from '../../lib/facelock/flows';
import {
  fetchFaceLockStatus,
  reasonText,
  verifyFace,
  verifyPin,
  type FaceLockStatus,
  type Purpose,
} from '../../lib/facelock/api';
import { PinPad } from './PinPad';

const CAMERA_TEXT: Record<string, string> = {
  denied: 'Camera access was refused. Use your passcode.',
  missing: 'No camera was found. Use your passcode.',
  busy: 'The camera is in use by another app. Use your passcode, or close that app and retry.',
  failed: 'The camera could not start. Use your passcode.',
};

const STEP_TEXT: Record<UnlockView['state'], string> = {
  search: 'Look at the camera.',
  acquire: 'Hold still for a moment.',
  liveness: 'Blink once, naturally.',
  capture: 'Reading…',
  verify: 'Checking…',
};

const mmss = (ms: number) => {
  const s = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

/**
 * Prove it is you: face (with a blink) or passcode. `purpose: 'unlock'` opens
 * the lock; `'manage'` returns a short-lived ticket for a Settings change.
 */
export function UnlockPanel({
  purpose,
  onSuccess,
  onCancel,
}: {
  purpose: Purpose;
  onSuccess: (ticket?: string) => void;
  onCancel?: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [status, setStatus] = useState<FaceLockStatus | null>(null);
  const [mode, setMode] = useState<'face' | 'pin'>('face');
  const [view, setView] = useState<UnlockView | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [wrong, setWrong] = useState(0);
  const [lockoutEnds, setLockoutEnds] = useState(0);
  const [now, setNow] = useState(Date.now());
  const resetTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  const settle = useCallback((r: Awaited<ReturnType<typeof verifyPin>>) => {
    if (r.status) setStatus(r.status);
    if (r.ok) { onSuccess(r.ticket); return true; }
    if (r.reason === 'lockout' || (r.status?.lockoutMs ?? 0) > 0) {
      setLockoutEnds(Date.now() + (r.status?.lockoutMs ?? 30_000));
    }
    setMessage(reasonText(r.reason));
    return false;
  }, [onSuccess]);

  const flowRef = useRef<UnlockFlow | null>(null);
  if (!flowRef.current) {
    flowRef.current = new UnlockFlow((probes) => {
      setBusy(true);
      void verifyFace(probes, purpose).then((r) => {
        setBusy(false);
        if (settle(r)) return;
        clearTimeout(resetTimer.current);
        resetTimer.current = setTimeout(() => { setMessage(''); flowRef.current?.reset(); }, 2600);
      }, () => {
        setBusy(false);
        setMessage('Ghost could not check right now. Try again.');
        flowRef.current?.reset();
      });
    });
  }
  useEffect(() => () => clearTimeout(resetTimer.current), []);

  useEffect(() => {
    void fetchFaceLockStatus().then((s) => {
      setStatus(s);
      if (s.lockoutMs > 0) setLockoutEnds(Date.now() + s.lockoutMs);
    });
  }, []);

  const lockedOut = lockoutEnds > now;
  useEffect(() => {
    if (!lockoutEnds) return;
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, [lockoutEnds]);

  const lastView = useRef('');
  const camera = useFaceCamera(videoRef, mode === 'face' && !lockedOut, (frame, t, embed) => {
    const v = flowRef.current!.frame(frame, t, embed);
    const key = `${v.state}|${v.hint}|${v.probes}`;
    if (key !== lastView.current) { lastView.current = key; setView(v); }
  });

  // No usable camera → straight to the passcode.
  useEffect(() => {
    if (camera.cameraError) setMode('pin');
  }, [camera.cameraError]);

  const onPin = useCallback((pin: string) => {
    setBusy(true);
    void verifyPin(pin, purpose).then((r) => {
      setBusy(false);
      if (!settle(r)) setWrong((n) => n + 1);
    }, () => { setBusy(false); setWrong((n) => n + 1); setMessage('Ghost could not check right now. Try again.'); });
  }, [purpose, settle]);

  const attempts = status ? `${status.attemptsLeft} of ${status.maxAttempts} attempts left` : '';
  const muted = { color: 'var(--color-text-tertiary)' };

  return (
    <div className="flex flex-col items-center gap-4 w-full max-w-sm" dir="auto">
      {lockedOut ? (
        <div className="flex flex-col items-center gap-2 py-10" role="alert">
          <div className="text-3xl font-semibold tabular-nums" style={{ color: 'var(--color-text)' }}>{mmss(lockoutEnds - now)}</div>
          <div className="text-sm" style={muted}>Too many attempts. Try again when the timer ends.</div>
        </div>
      ) : mode === 'face' ? (
        <>
          <div className="relative w-64 h-64 rounded-full overflow-hidden" style={{ border: '2px solid var(--color-accent)', background: 'var(--color-bg-secondary)' }}>
            <video ref={videoRef} className="w-full h-full object-cover" style={{ transform: 'scaleX(-1)' }} muted playsInline aria-hidden="true" />
            {camera.phase === 'loading' && (
              <div className="absolute inset-0 flex items-center justify-center"><Loader2 size={22} className="animate-spin" style={muted} /></div>
            )}
          </div>
          <div className="text-base font-medium text-center min-h-6" style={{ color: 'var(--color-text)' }} aria-live="polite">
            {busy ? 'Checking…' : message || view?.hint || (view ? STEP_TEXT[view.state] : 'Starting the camera…')}
          </div>
          {camera.modelsError && (
            <p className="text-xs text-center" style={{ color: 'var(--color-error)' }}>
              The recognition models could not be loaded. Use your passcode.
            </p>
          )}
          <p className="text-xs text-center" style={muted}>On this PC only. No picture is taken or stored.</p>
        </>
      ) : (
        <>
          {camera.cameraError && <p className="text-xs text-center" style={muted}>{CAMERA_TEXT[camera.cameraError]}</p>}
          <PinPad length={status?.pinLength ?? 6} label="Enter your passcode" busy={busy} wrong={wrong} onComplete={onPin} />
          {message && <p role="alert" className="text-xs" style={{ color: 'var(--color-error)' }}>{message}</p>}
        </>
      )}

      {status && status.attemptsLeft < status.maxAttempts && !lockedOut && <p className="text-xs" style={muted}>{attempts}</p>}

      <div className="flex gap-2">
        {!lockedOut && (
          <button
            type="button"
            onClick={() => { setMessage(''); setMode(mode === 'face' ? 'pin' : 'face'); flowRef.current?.reset(); }}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm cursor-pointer"
            style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}
          >
            {mode === 'face' ? <><KeyRound size={14} /> Use passcode</> : <><ScanFace size={14} /> Use face</>}
          </button>
        )}
        {camera.cameraError && mode === 'pin' && (
          <button type="button" onClick={() => { setMode('face'); camera.retryCamera(); }} className="px-3 py-1.5 rounded-lg text-sm cursor-pointer" style={muted}>
            Retry camera
          </button>
        )}
        {onCancel && (
          <button type="button" onClick={onCancel} className="px-3 py-1.5 rounded-lg text-sm cursor-pointer" style={muted}>Cancel</button>
        )}
      </div>
    </div>
  );
}
