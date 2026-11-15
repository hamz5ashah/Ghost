import { useCallback, useEffect, useState } from 'react';
import { Hand, Loader2 } from 'lucide-react';
import { isTauri } from '../../lib/api';
import { getStonicFlags } from '../../lib/stonic-flags';
import { handControl, type HandFrame } from '../../lib/gestures/runner';
import { ModelDownload } from '../facelock/ModelDownload';

const GUIDE: Array<[string, string]> = [
  ['Two fingers up', 'move the cursor'],
  ['…and touch thumb to index tip', 'left click'],
  ['…and touch thumb to middle tip', 'right click'],
  ['Three fingers up, move up / down', 'scroll'],
  ['Fist', 'grab and drag'],
  ['Hand down, or Esc in Ghost', 'stop'],
];

/** Settings → Security & input → Hand control. Nothing renders unless the flag is on. */
export function HandControlSection() {
  const [flag, setFlag] = useState(false);
  const [ready, setReady] = useState(false);
  const [running, setRunning] = useState(() => handControl().running);
  const [starting, setStarting] = useState(false);
  const [frame, setFrame] = useState<HandFrame | null>(null);
  const [error, setError] = useState('');

  useEffect(() => { void getStonicFlags().then((f) => setFlag(f.gestures)); }, []);

  useEffect(() => {
    const runner = handControl();
    const unsubscribe = runner.subscribe(() => setRunning(runner.running));
    let last = 0;
    runner.onFrame = (f) => {
      // The telemetry line does not need 30 renders a second.
      const now = performance.now();
      if (now - last > 120) { last = now; setFrame(f); }
    };
    return () => { unsubscribe(); runner.onFrame = () => {}; };
  }, []);

  const toggle = useCallback(async () => {
    const runner = handControl();
    setError('');
    if (runner.running) { runner.stop(); return; }
    setStarting(true);
    try {
      await runner.start();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  }, []);

  if (!flag) return null;

  return (
    <div className="rounded-xl p-5" style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}>
      <h3 className="text-sm font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
        <Hand size={15} /> Hand control
      </h3>
      <p className="text-xs mb-4" style={{ color: 'var(--color-text-tertiary)' }}>
        Control the mouse with your hand in front of the webcam. The camera is read on this PC only; nothing is recorded.
      </p>
      {!isTauri() ? (
        <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>Hand control works in the Ghost desktop app on Windows.</p>
      ) : (
        <div className="flex flex-col gap-3">
          <ModelDownload bundle="gestures" onReady={setReady} />
          <button
            type="button"
            disabled={!ready || starting}
            onClick={() => void toggle()}
            aria-pressed={running}
            className="self-start inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm cursor-pointer disabled:opacity-50"
            style={{
              background: running ? 'var(--color-accent-subtle)' : 'var(--color-bg-secondary)',
              color: running ? 'var(--color-accent)' : 'var(--color-text)',
              border: `1px solid ${running ? 'var(--color-accent)' : 'var(--color-border)'}`,
            }}
          >
            {starting ? <Loader2 size={14} className="animate-spin" /> : <Hand size={14} />}
            {running ? 'Stop hand control' : 'Start hand control'}
          </button>
          {running && frame && (
            <p className="text-xs tabular-nums" style={{ color: 'var(--color-text-secondary)' }} aria-live="polite">
              {frame.mode} · {frame.pose}{frame.action ? ` · ${frame.action}` : ''} · {frame.fps} fps
            </p>
          )}
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {GUIDE.map(([g, a]) => (
              <div key={g} className="contents"><dt>{g}</dt><dd style={{ color: 'var(--color-text-secondary)' }}>{a}</dd></div>
            ))}
          </dl>
          {error && <p role="alert" className="text-xs" style={{ color: 'var(--color-error)' }}>{error}</p>}
        </div>
      )}
    </div>
  );
}
