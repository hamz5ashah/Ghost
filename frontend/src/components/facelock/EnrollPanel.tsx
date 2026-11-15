import { useCallback, useRef, useState } from 'react';
import { Check, Loader2 } from 'lucide-react';
import { useFaceCamera } from '../../hooks/useFaceCamera';
import { ENROLL, EnrollFlow, pinProblem, type EnrollView } from '../../lib/facelock/flows';
import { enrollFace, reasonText } from '../../lib/facelock/api';
import { PinPad } from './PinPad';

const STEP: Record<EnrollView['state'], string> = {
  search: 'Step into view.',
  centre: 'Look straight at the camera.',
  angles: 'Slowly turn your head in a small circle, eyes on the screen.',
  blink: 'Blink once, naturally.',
  done: 'Face print ready.',
  failed: 'Scan failed.',
};

/** Direction dots around the preview, one per head-turn sector. */
function SectorRing({ sectors, pointing }: { sectors: boolean[]; pointing: number }) {
  return (
    <>
      {sectors.map((on, i) => {
        // Sector 0 = head turned toward +yaw; the preview is mirrored.
        const angle = (i / ENROLL.SECTORS) * Math.PI * 2;
        const x = 50 - Math.cos(angle) * 54;
        const y = 50 + Math.sin(angle) * 54;
        return (
          <span
            key={i}
            className="absolute w-3 h-3 rounded-full -translate-x-1/2 -translate-y-1/2"
            style={{
              left: `${x}%`,
              top: `${y}%`,
              background: on ? 'var(--color-success)' : i === pointing ? 'var(--color-accent)' : 'var(--color-border)',
            }}
          />
        );
      })}
    </>
  );
}

/**
 * Register a face, then a passcode. `ticket` is required when Face Lock is
 * already on (re-scan); the backend refuses without it.
 */
export function EnrollPanel({ ticket, onDone, onCancel }: { ticket?: string; onDone: () => void; onCancel: () => void }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const flowRef = useRef(new EnrollFlow());
  const [view, setView] = useState<EnrollView>(flowRef.current.view());
  const [step, setStep] = useState<'scan' | 'pin' | 'confirm' | 'saving'>('scan');
  const [firstPin, setFirstPin] = useState('');
  const [wrong, setWrong] = useState(0);
  const [error, setError] = useState('');

  const last = useRef('');
  const camera = useFaceCamera(videoRef, step === 'scan', (frame, t, embed) => {
    const v = flowRef.current.frame(frame, t, embed);
    const key = `${v.state}|${v.hint}|${v.centreShots}|${v.sectors.join()}|${v.pointing}`;
    if (key !== last.current) {
      last.current = key;
      setView(v);
      if (v.state === 'done') setStep('pin');
    }
  });

  const onFirst = useCallback((pin: string) => {
    const problem = pinProblem(pin);
    if (problem) { setError(problem); setWrong((n) => n + 1); return; }
    setError('');
    setFirstPin(pin);
    setStep('confirm');
  }, []);

  const onConfirm = useCallback((pin: string) => {
    if (pin !== firstPin) {
      setError('The two passcodes did not match. Start again.');
      setFirstPin('');
      setStep('pin');
      setWrong((n) => n + 1);
      return;
    }
    setStep('saving');
    void enrollFace(flowRef.current.templates(), pin, ticket).then((r) => {
      if (r.ok) onDone();
      else { setError(reasonText(r.reason)); setStep('pin'); setFirstPin(''); }
    }, () => { setError('Ghost could not save right now.'); setStep('pin'); });
  }, [firstPin, onDone, ticket]);

  const rescan = () => { flowRef.current.reset(); setView(flowRef.current.view()); setError(''); setStep('scan'); };
  const muted = { color: 'var(--color-text-tertiary)' };

  return (
    <div className="flex flex-col items-center gap-4 py-2">
      {step === 'scan' && (
        <>
          <div className="relative w-56 h-56 m-4">
            <div className="w-full h-full rounded-full overflow-hidden" style={{ border: '2px solid var(--color-accent)', background: 'var(--color-bg-secondary)' }}>
              <video ref={videoRef} className="w-full h-full object-cover" style={{ transform: 'scaleX(-1)' }} muted playsInline aria-hidden="true" />
            </div>
            {view.state === 'angles' && <SectorRing sectors={view.sectors} pointing={view.pointing} />}
            {camera.phase === 'loading' && (
              <div className="absolute inset-0 flex items-center justify-center"><Loader2 size={22} className="animate-spin" style={muted} /></div>
            )}
          </div>
          <div className="text-sm font-medium text-center" style={{ color: 'var(--color-text)' }} aria-live="polite">
            {view.state === 'failed' ? view.error : view.hint || STEP[view.state]}
          </div>
          <div className="text-xs" style={muted}>
            {view.state === 'centre' && `Straight-on ${view.centreShots} of ${ENROLL.CENTRE_SHOTS}`}
            {view.state === 'angles' && `Angles ${view.sectors.filter(Boolean).length} of ${ENROLL.SECTORS}`}
            {view.state === 'blink' && 'Last step'}
          </div>
          {(camera.cameraError || camera.modelsError) && (
            <p role="alert" className="text-xs text-center" style={{ color: 'var(--color-error)' }}>
              {camera.cameraError ? 'Face Lock needs a working camera.' : 'The recognition models could not be loaded.'}
            </p>
          )}
          {view.state === 'failed' && (
            <button type="button" onClick={rescan} className="px-3 py-1.5 rounded-lg text-sm cursor-pointer" style={{ border: '1px solid var(--color-border)', color: 'var(--color-text)' }}>
              Scan again
            </button>
          )}
        </>
      )}
      {step === 'pin' && (
        <>
          <div className="text-sm inline-flex items-center gap-1.5" style={{ color: 'var(--color-success)' }}><Check size={14} /> Face print ready</div>
          <PinPad length={6} label="Choose a 6-digit passcode — it opens Ghost when the camera cannot" wrong={wrong} onComplete={onFirst} />
        </>
      )}
      {step === 'confirm' && <PinPad length={6} label="Repeat the passcode" onComplete={onConfirm} />}
      {step === 'saving' && <div className="text-sm inline-flex items-center gap-2" style={muted}><Loader2 size={14} className="animate-spin" /> Encrypting with Windows on this PC…</div>}
      {error && step !== 'scan' && <p role="alert" className="text-xs" style={{ color: 'var(--color-error)' }}>{error}</p>}
      <button type="button" onClick={onCancel} className="text-xs cursor-pointer" style={muted}>Cancel</button>
    </div>
  );
}
