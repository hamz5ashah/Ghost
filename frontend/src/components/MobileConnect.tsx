import { useState } from 'react';
import { Laptop, Loader2, KeyRound, Wifi } from 'lucide-react';
import { useAppStore } from '../lib/store';
import { checkServerConnection, normalizeServerUrl } from '../lib/api';

const REASONS: Record<'unreachable' | 'bad-key' | 'not-ghost', string> = {
  unreachable:
    "Can't reach your PC. Check that Ghost is open on your PC, Phone access is turned on, and your phone is on the same Wi-Fi.",
  'bad-key': 'Your PC rejected that access key. Copy it again from Settings → Phone access on your PC.',
  'not-ghost': "Something answered at that address, but it isn't Ghost. Check the address.",
};

/**
 * First screen of the Android app: pair the phone with Ghost running on the
 * user's PC. The address and key come from Settings → Phone access in the
 * desktop app; they are saved in the normal settings so every API call uses them.
 */
export function MobileConnect({ onConnected }: { onConnected: () => void }) {
  const settings = useAppStore((s) => s.settings);
  const updateSettings = useAppStore((s) => s.updateSettings);
  const [address, setAddress] = useState(settings.apiUrl || '');
  const [apiKey, setApiKey] = useState(settings.apiKey || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const connect = async (e: React.FormEvent) => {
    e.preventDefault();
    const url = normalizeServerUrl(address);
    if (!url) {
      setError('Enter the address shown on your PC.');
      return;
    }
    setBusy(true);
    setError('');
    const result = await checkServerConnection(url, apiKey.trim());
    setBusy(false);
    if (!result.ok) {
      setError(REASONS[result.reason]);
      return;
    }
    updateSettings({ apiUrl: url, apiKey: apiKey.trim() });
    onConnected();
  };

  const inputStyle = {
    background: 'var(--color-bg-secondary)',
    color: 'var(--color-text)',
    border: '1px solid var(--color-border)',
  };

  return (
    <div
      className="min-h-dvh flex items-center justify-center px-4 py-8"
      style={{ background: 'var(--color-bg)' }}
    >
      <form
        noValidate
        onSubmit={connect}
        className="w-full max-w-sm rounded-2xl p-6"
        style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}
      >
        <img src="/pwa-192x192.png" alt="" className="w-14 h-14 rounded-2xl mb-4" />
        <h1 className="text-xl font-semibold mb-1" style={{ color: 'var(--color-text)' }}>
          Connect to your PC
        </h1>
        <p className="text-sm mb-5" style={{ color: 'var(--color-text-secondary)' }}>
          Ghost runs on your computer. On your PC, open Ghost → Settings → Phone access,
          turn it on, and enter what it shows here.
        </p>

        <label className="block text-xs font-medium mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
          <span className="inline-flex items-center gap-1.5"><Laptop size={13} /> PC address</span>
        </label>
        <input
          type="text"
          inputMode="url"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          placeholder="http://192.168.1.20:8000"
          className="w-full text-base px-3 py-2.5 rounded-lg outline-none mb-4"
          style={inputStyle}
        />

        <label className="block text-xs font-medium mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
          <span className="inline-flex items-center gap-1.5"><KeyRound size={13} /> Access key</span>
        </label>
        <input
          type="password"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          autoComplete="off"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="ghost_sk_…"
          className="w-full text-base px-3 py-2.5 rounded-lg outline-none mb-4"
          style={inputStyle}
        />

        {error && (
          <p role="alert" className="text-sm mb-4" style={{ color: 'var(--color-error)' }}>
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          className="w-full flex items-center justify-center gap-2 py-3 rounded-lg text-sm font-medium cursor-pointer disabled:opacity-60"
          style={{ background: 'var(--color-accent)', color: 'white' }}
        >
          {busy ? <Loader2 size={16} className="animate-spin" /> : <Wifi size={16} />}
          {busy ? 'Connecting…' : 'Connect'}
        </button>

        <p className="text-xs mt-4" style={{ color: 'var(--color-text-tertiary)' }}>
          Your phone and PC need to be on the same Wi-Fi network. Your PC must stay on
          with Ghost open while you use the app.
        </p>
      </form>
    </div>
  );
}
