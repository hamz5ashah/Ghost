import { useCallback, useEffect, useState } from 'react';
import { Lock, ScanFace } from 'lucide-react';
import { getStonicFlags } from '../../lib/stonic-flags';
import {
  changePin,
  disableFaceLock,
  fetchFaceLockStatus,
  lockNow,
  reasonText,
  type FaceLockStatus,
} from '../../lib/facelock/api';
import { pinProblem } from '../../lib/facelock/flows';
import { EnrollPanel } from './EnrollPanel';
import { ModelDownload } from './ModelDownload';
import { PinPad } from './PinPad';
import { UnlockPanel } from './UnlockPanel';

type Action = 'rescan' | 'pin' | 'disable';
type Mode =
  | { kind: 'idle' }
  | { kind: 'enroll'; ticket?: string }
  | { kind: 'verify'; action: Action }
  | { kind: 'newpin'; ticket: string };

const button = 'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm cursor-pointer';
const buttonStyle = { background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' };

/** Settings → Security & input → Face Lock. Nothing renders unless the flag is on. */
export function FaceLockSection() {
  const [flag, setFlag] = useState(false);
  const [status, setStatus] = useState<FaceLockStatus | null>(null);
  const [modelsReady, setModelsReady] = useState(false);
  const [mode, setMode] = useState<Mode>({ kind: 'idle' });
  const [note, setNote] = useState('');
  const [wrong, setWrong] = useState(0);

  const refresh = useCallback(async () => setStatus(await fetchFaceLockStatus()), []);
  useEffect(() => { void getStonicFlags().then((f) => setFlag(f.facelock)); }, []);
  useEffect(() => { if (flag) void refresh(); }, [flag, refresh]);

  if (!flag || !status) return null;

  const done = (text: string) => { setMode({ kind: 'idle' }); setNote(text); void refresh(); };

  const afterVerify = (action: Action) => (ticket?: string) => {
    if (!ticket) { setNote('Confirm it is you first.'); setMode({ kind: 'idle' }); return; }
    if (action === 'rescan') setMode({ kind: 'enroll', ticket });
    else if (action === 'pin') setMode({ kind: 'newpin', ticket });
    else void disableFaceLock(ticket).then((r) => (r.ok ? done('Face Lock is off. Your face print was deleted.') : done(reasonText(r.reason))));
  };

  const onNewPin = (ticket: string) => (pin: string) => {
    const problem = pinProblem(pin);
    if (problem) { setNote(problem); setWrong((n) => n + 1); return; }
    void changePin(pin, ticket).then((r) => done(r.ok ? 'Passcode changed.' : reasonText(r.reason)));
  };

  return (
    <div className="rounded-xl p-5" style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}>
      <h3 className="text-sm font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
        <ScanFace size={15} /> Face Lock
      </h3>
      <p className="text-xs mb-4" style={{ color: 'var(--color-text-tertiary)' }}>
        Ghost opens only for your face (with a blink) or your passcode, and locks again when Windows locks or the PC sleeps.
        Your face print is encrypted with your Windows account; no picture is stored and nothing leaves this PC.
        An ordinary webcam has no depth sensor, so treat this as a lock for the people around you, not a bank vault.
      </p>

      {!status.supported ? (
        <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>{reasonText('unsupported')}</p>
      ) : mode.kind === 'enroll' ? (
        <EnrollPanel ticket={mode.ticket} onDone={() => done('Face Lock is on. From the next launch, Ghost opens only for you.')} onCancel={() => setMode({ kind: 'idle' })} />
      ) : mode.kind === 'verify' ? (
        <div className="flex justify-center"><UnlockPanel purpose="manage" onSuccess={afterVerify(mode.action)} onCancel={() => setMode({ kind: 'idle' })} /></div>
      ) : mode.kind === 'newpin' ? (
        <div className="flex flex-col items-center gap-2">
          <PinPad length={status.pinLength} label="New 6-digit passcode" wrong={wrong} onComplete={onNewPin(mode.ticket)} />
          <button type="button" onClick={() => setMode({ kind: 'idle' })} className="text-xs cursor-pointer" style={{ color: 'var(--color-text-tertiary)' }}>Cancel</button>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <ModelDownload bundle="facelock" onReady={setModelsReady} />
          {status.enabled ? (
            <div className="flex flex-wrap gap-2">
              <button type="button" className={button} style={buttonStyle} onClick={() => void lockNow().then(() => refresh())}><Lock size={14} /> Lock now</button>
              <button type="button" className={button} style={buttonStyle} disabled={!modelsReady} onClick={() => setMode({ kind: 'verify', action: 'rescan' })}>Scan face again</button>
              <button type="button" className={button} style={buttonStyle} onClick={() => setMode({ kind: 'verify', action: 'pin' })}>Change passcode</button>
              <button type="button" className={button} style={{ ...buttonStyle, color: 'var(--color-error)' }} onClick={() => setMode({ kind: 'verify', action: 'disable' })}>Turn off</button>
            </div>
          ) : (
            <button type="button" className={`${button} self-start`} style={buttonStyle} disabled={!modelsReady} onClick={() => { setNote(''); setMode({ kind: 'enroll' }); }}>
              <ScanFace size={14} /> Set up Face Lock
            </button>
          )}
          {status.enabled && <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{status.templateCount} face angles registered. Forgot the passcode? Delete <code>face_lock.dpapi</code> from Ghost&apos;s config folder.</p>}
        </div>
      )}
      {note && mode.kind === 'idle' && <p className="text-xs mt-3" role="status" style={{ color: 'var(--color-text-secondary)' }}>{note}</p>}
    </div>
  );
}
