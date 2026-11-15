import { useCallback, useEffect, useRef, useState } from 'react';
import { Delete } from 'lucide-react';

/** Fixed-length passcode entry: on-screen keys or the keyboard. */
export function PinPad({
  length,
  label,
  busy = false,
  wrong = 0,
  onComplete,
}: {
  length: number;
  label: string;
  busy?: boolean;
  /** Bump to clear the entry and shake. */
  wrong?: number;
  onComplete: (pin: string) => void;
}) {
  const [pin, setPin] = useState('');
  const [shake, setShake] = useState(false);
  const sent = useRef(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!wrong) return;
    setPin('');
    sent.current = false;
    setShake(true);
    const t = setTimeout(() => setShake(false), 320);
    return () => clearTimeout(t);
  }, [wrong]);

  const press = useCallback((d: string) => {
    if (busy || sent.current) return;
    setPin((p) => (p.length >= length ? p : p + d));
  }, [busy, length]);
  const erase = useCallback(() => {
    if (busy || sent.current) return;
    setPin((p) => p.slice(0, -1));
  }, [busy]);

  useEffect(() => {
    if (pin.length !== length || sent.current) return;
    sent.current = true;
    onComplete(pin);
  }, [pin, length, onComplete]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (/^\d$/.test(e.key)) { e.preventDefault(); press(e.key); }
      else if (e.key === 'Backspace') { e.preventDefault(); erase(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [press, erase]);

  const key = 'h-12 rounded-lg text-lg font-medium cursor-pointer disabled:opacity-50';
  const keyStyle = { background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' };

  return (
    <div ref={root} className="flex flex-col items-center gap-4">
      <div className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>{label}</div>
      <div
        className={`flex gap-2 ${shake ? 'animate-pulse' : ''}`}
        aria-label={`${pin.length} of ${length} digits entered`}
        role="status"
      >
        {Array.from({ length }, (_, i) => (
          <span
            key={i}
            className="w-3 h-3 rounded-full"
            style={{ background: i < pin.length ? 'var(--color-accent)' : 'var(--color-border)' }}
          />
        ))}
      </div>
      <div className="grid grid-cols-3 gap-2 w-56">
        {['1', '2', '3', '4', '5', '6', '7', '8', '9'].map((d) => (
          <button key={d} type="button" tabIndex={-1} disabled={busy} onClick={() => press(d)} className={key} style={keyStyle}>{d}</button>
        ))}
        <span />
        <button type="button" tabIndex={-1} disabled={busy} onClick={() => press('0')} className={key} style={keyStyle}>0</button>
        <button type="button" tabIndex={-1} disabled={busy} onClick={erase} className={`${key} flex items-center justify-center`} style={keyStyle} aria-label="Delete digit">
          <Delete size={18} />
        </button>
      </div>
    </div>
  );
}
