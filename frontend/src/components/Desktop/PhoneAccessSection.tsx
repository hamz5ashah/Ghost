import { useEffect, useState } from 'react';
import { Smartphone, Copy, Check, Loader2, RefreshCw } from 'lucide-react';
import { getPhoneAccess, setPhoneAccess, type PhoneAccess } from '../../lib/api';

function CopyField({ label, value, secret }: { label: string; value: string; secret?: boolean }) {
  const [copied, setCopied] = useState(false);
  const [shown, setShown] = useState(!secret);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* clipboard unavailable */ }
  };
  return (
    <div className="mb-3">
      <div className="text-xs mb-1" style={{ color: 'var(--color-text-tertiary)' }}>{label}</div>
      <div className="flex items-center gap-2">
        <code
          className="flex-1 min-w-0 truncate text-sm px-3 py-2 rounded-lg select-all"
          style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}
        >
          {shown ? value : '•'.repeat(24)}
        </code>
        {secret && (
          <button
            type="button"
            onClick={() => setShown((s) => !s)}
            className="text-xs px-2 py-2 rounded-lg cursor-pointer"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {shown ? 'Hide' : 'Show'}
          </button>
        )}
        <button
          type="button"
          onClick={copy}
          aria-label={`Copy ${label}`}
          className="p-2 rounded-lg cursor-pointer"
          style={{ color: copied ? 'var(--color-success)' : 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
        >
          {copied ? <Check size={14} /> : <Copy size={14} />}
        </button>
      </div>
    </div>
  );
}

/** Desktop Settings → Phone access: pair the Ghost Android app with this PC. */
export function PhoneAccessSection() {
  const [state, setState] = useState<PhoneAccess | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    getPhoneAccess().then(setState).catch((e) => setError(String(e)));
  }, []);

  const apply = async (enabled: boolean, newKey = false) => {
    setBusy(true);
    setError('');
    try {
      setState(await setPhoneAccess(enabled, newKey));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const enabled = !!state?.enabled;

  return (
    <div
      className="rounded-xl p-5"
      style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}
    >
      <div className="flex items-center justify-between gap-4 mb-2">
        <h3 className="text-sm font-semibold flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
          <Smartphone size={15} /> Phone access
        </h3>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label="Phone access"
          disabled={busy || !state}
          onClick={() => apply(!enabled)}
          className="relative w-10 h-6 rounded-full transition-colors cursor-pointer disabled:opacity-60"
          style={{ background: enabled ? 'var(--color-accent)' : 'var(--color-bg-tertiary, var(--color-border))' }}
        >
          <span
            className="absolute top-0.5 w-5 h-5 rounded-full bg-white transition-all"
            style={{ left: enabled ? '18px' : '2px' }}
          />
        </button>
      </div>
      <p className="text-xs mb-4" style={{ color: 'var(--color-text-tertiary)' }}>
        Use Ghost from the Android app while your phone is on the same Wi-Fi as this PC.
        Turning this on restarts Ghost. If Windows asks about network access, allow it on
        private networks.
      </p>

      {busy && (
        <div className="flex items-center gap-2 text-xs mb-3" style={{ color: 'var(--color-text-secondary)' }}>
          <Loader2 size={13} className="animate-spin" /> Restarting Ghost…
        </div>
      )}
      {error && <p role="alert" className="text-xs mb-3" style={{ color: 'var(--color-error)' }}>{error}</p>}

      {enabled && state && (
        <>
          <p className="text-sm mb-3" style={{ color: 'var(--color-text-secondary)' }}>
            In the Ghost app on your phone, enter:
          </p>
          {state.url ? (
            <CopyField label="PC address" value={state.url} />
          ) : (
            <p className="text-xs mb-3" style={{ color: 'var(--color-warning, var(--color-error))' }}>
              Couldn't detect this PC's network address. Check that it is connected to Wi-Fi or
              Ethernet, then use its IP address with port {state.port}.
            </p>
          )}
          {state.api_key && <CopyField label="Access key" value={state.api_key} secret />}
          <button
            type="button"
            disabled={busy}
            onClick={() => apply(true, true)}
            className="flex items-center gap-1.5 text-xs mt-1 cursor-pointer disabled:opacity-60"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            <RefreshCw size={12} /> Make a new key (your phone will need the new one)
          </button>
        </>
      )}
    </div>
  );
}
