import { useCallback, useEffect, useState } from 'react';
import { Check, Cloud, ExternalLink, Eye, EyeOff, Loader2 } from 'lucide-react';
import { getCloudKeyStatus, isTauri, saveCloudKey } from '../lib/api';
import { CLOUD_KEY_STATUS_CHANGED, CLOUD_PROVIDERS, type CloudProvider } from '../lib/cloud-providers';
import { shortcutLabel } from '../lib/shortcuts';

function ProviderRow({
  provider,
  connected,
  onChanged,
}: {
  provider: CloudProvider;
  connected: boolean;
  onChanged: () => void;
}) {
  const [value, setValue] = useState('');
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');

  const save = async (next: string) => {
    setBusy(true);
    setError('');
    try {
      await saveCloudKey(provider.envKey, next.trim());
      setValue('');
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      window.dispatchEvent(new Event(CLOUD_KEY_STATUS_CHANGED));
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="py-3" style={{ borderBottom: '1px solid var(--color-border-subtle)' }}>
      <div className="flex items-center gap-2 mb-2">
        <Cloud size={14} style={{ color: connected ? 'var(--color-success)' : 'var(--color-text-tertiary)' }} />
        <span className="text-sm font-medium" style={{ color: 'var(--color-text)' }}>{provider.name}</span>
        <span
          className="text-[10px] px-1.5 py-0.5 rounded-full"
          style={connected
            ? { background: 'color-mix(in srgb, var(--color-success) 12%, transparent)', color: 'var(--color-success)' }
            : { background: 'var(--color-bg-secondary)', color: 'var(--color-text-tertiary)' }}
        >
          {connected ? 'Connected' : 'Not connected'}
        </span>
        <a
          href={provider.consoleUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="ml-auto flex items-center gap-1 text-xs"
          style={{ color: 'var(--color-accent)' }}
        >
          Get a key <ExternalLink size={11} />
        </a>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div
          className="flex-1 min-w-[220px] flex items-center rounded-lg"
          style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)' }}
        >
          <input
            type={visible ? 'text' : 'password'}
            value={value}
            onChange={(e) => { setValue(e.target.value); setError(''); }}
            onKeyDown={(e) => { if (e.key === 'Enter' && value.trim()) void save(value); }}
            placeholder={connected ? 'Saved in secure storage — paste a new key to replace' : provider.placeholder}
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
            className="flex-1 text-sm px-3 py-1.5 bg-transparent outline-none font-mono"
            style={{ color: 'var(--color-text)' }}
            aria-label={`${provider.name} API key`}
          />
          <button
            type="button"
            onClick={() => setVisible((v) => !v)}
            className="px-2 cursor-pointer"
            style={{ color: 'var(--color-text-tertiary)' }}
            aria-label={visible ? 'Hide key' : 'Show key'}
          >
            {visible ? <EyeOff size={13} /> : <Eye size={13} />}
          </button>
        </div>
        <button
          type="button"
          onClick={() => void save(value)}
          disabled={busy || !value.trim()}
          className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          style={{ background: 'var(--color-accent)', color: 'white' }}
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : saved ? <Check size={12} /> : null}
          {saved ? 'Saved' : 'Save'}
        </button>
        {connected && (
          <button
            type="button"
            onClick={() => void save('')}
            disabled={busy}
            className="text-xs px-3 py-1.5 rounded-lg cursor-pointer disabled:opacity-50"
            style={{ color: 'var(--color-error)', border: '1px solid var(--color-error)' }}
          >
            Remove
          </button>
        )}
      </div>
      {error && <p role="alert" className="text-xs mt-1.5" style={{ color: 'var(--color-error)' }}>{error}</p>}
      <p className="text-xs mt-1.5" style={{ color: 'var(--color-text-tertiary)' }}>
        Models: {provider.models.map((m) => m.id).join(', ')}
      </p>
    </div>
  );
}

/**
 * Settings → Cloud AI providers: connect Anthropic, DeepSeek, OpenAI… with
 * your own API key. Connected providers' models appear in the model picker.
 */
export function CloudProvidersSection() {
  const [status, setStatus] = useState<Record<string, boolean>>({});
  const [error, setError] = useState('');
  const desktop = isTauri();

  const refresh = useCallback(async () => {
    if (!desktop) return;
    try {
      setStatus(await getCloudKeyStatus());
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [desktop]);

  useEffect(() => {
    void refresh();
    window.addEventListener(CLOUD_KEY_STATUS_CHANGED, refresh);
    return () => window.removeEventListener(CLOUD_KEY_STATUS_CHANGED, refresh);
  }, [refresh]);

  return (
    <div
      className="rounded-xl p-5"
      style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}
    >
      <h3 className="text-sm font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
        <Cloud size={15} /> Your API keys
      </h3>
      <p className="text-xs mb-1" style={{ color: 'var(--color-text-tertiary)' }}>
        Connect a provider with your own API key to use its models. Once connected, pick the model
        from the model menu ({shortcutLabel('K')}) on the chat screen. Keys are stored in your computer's
        secure storage and sent only to that provider.
      </p>
      {!desktop && (
        <p className="text-xs mt-2" style={{ color: 'var(--color-text-secondary)' }}>
          Cloud keys are managed in the Ghost desktop app.
        </p>
      )}
      {error && <p role="alert" className="text-xs mt-2" style={{ color: 'var(--color-error)' }}>{error}</p>}
      {desktop && CLOUD_PROVIDERS.map((p) => (
        <ProviderRow key={p.envKey} provider={p} connected={!!status[p.envKey]} onChanged={() => void refresh()} />
      ))}
    </div>
  );
}
