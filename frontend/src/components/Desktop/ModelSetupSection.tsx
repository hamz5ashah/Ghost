import { useCallback, useEffect, useState } from 'react';
import { Brain, CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { useNavigate } from 'react-router';
import {
  getCloudKeyStatus,
  getInferenceSource,
  resetInferenceSource,
} from '../../lib/api';
import { CLOUD_KEY_STATUS_CHANGED, CLOUD_PROVIDERS } from '../../lib/cloud-providers';
import { persistAndStartInferenceSource } from '../InferenceSourceSetup';
import { refreshDesktopStatus, useDesktopStatus } from '../../hooks/useDesktopStatus';
import { friendlyStartupError } from './DesktopStatusBanner';

const inputStyle = {
  background: 'var(--color-bg-secondary)',
  color: 'var(--color-text)',
  border: '1px solid var(--color-border)',
};

function Row({ label, description, children }: { label: string; description?: string; children: React.ReactNode }) {
  return (
    <div
      className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 py-3"
      style={{ borderBottom: '1px solid var(--color-border-subtle)' }}
    >
      <div className="min-w-0">
        <div className="text-sm" style={{ color: 'var(--color-text)' }}>{label}</div>
        {description && (
          <div className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{description}</div>
        )}
      </div>
      <div>{children}</div>
    </div>
  );
}

/**
 * Desktop Settings → Model. Choosing and starting the AI model lives here
 * (not on a launch screen): the app opens straight to chat and a banner
 * points here until a model is running.
 */
export function ModelSetupSection() {
  const status = useDesktopStatus();
  // 'cloud' = Ghost's built-in cloud engine with a provider from Settings →
  // Cloud providers (DeepSeek, Anthropic, …): no local model, no server URL.
  const [kind, setKind] = useState<'ollama' | 'custom' | 'cloud'>('ollama');
  const [cloudModel, setCloudModel] = useState('deepseek-v4-flash');
  const [connected, setConnected] = useState<Record<string, boolean>>({});
  const navigate = useNavigate();
  const [host, setHost] = useState('http://localhost:1234/v1');
  const [model, setModel] = useState('');
  const [engine, setEngine] = useState('lmstudio');
  const [apiKey, setApiKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    getInferenceSource().then((s) => {
      if (s.kind === 'custom' && s.engine === 'cloud') {
        setKind('cloud');
        if (s.model) setCloudModel(s.model);
        return;
      }
      setKind(s.kind);
      if (s.host) setHost(s.host);
      if (s.model) setModel(s.model);
      if (s.engine) setEngine(s.engine);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    const refresh = () => { getCloudKeyStatus().then(setConnected).catch(() => {}); };
    refresh();
    window.addEventListener(CLOUD_KEY_STATUS_CHANGED, refresh);
    return () => window.removeEventListener(CLOUD_KEY_STATUS_CHANGED, refresh);
  }, []);

  const connectedProviders = CLOUD_PROVIDERS.filter((p) => connected[p.envKey]);
  const cloudModels = connectedProviders.flatMap((p) => p.models.map((m) => ({ ...m, provider: p.name })));

  const start = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      if (kind === 'custom' && (!host.trim() || !model.trim())) {
        throw new Error('Enter the server URL and the model name.');
      }
      if (kind === 'cloud' && !cloudModels.some((m) => m.id === cloudModel)) {
        throw new Error('Connect a provider in Cloud providers first, then pick one of its models.');
      }
      // Stop whatever is running and forget the old choice, then save the new
      // one and start it. Progress and errors show in the status row above.
      await resetInferenceSource().catch(() => {});
      await persistAndStartInferenceSource(
        kind === 'custom'
          ? { kind: 'custom', host: host.trim(), model: model.trim(), engine, apiKey: apiKey || undefined }
          : kind === 'cloud'
            ? { kind: 'custom', engine: 'cloud', model: cloudModel }
            : { kind: 'ollama' },
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      refreshDesktopStatus();
    }
  }, [kind, host, model, engine, apiKey, cloudModel, cloudModels]);

  const ready = status?.phase === 'ready' && !status.error;
  const notSetUp = !status || status.requires_source;
  const starting = !!status && !status.requires_source && !ready && !status.error;

  return (
    <div
      className="rounded-xl p-5"
      style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}
    >
      <h3 className="text-sm font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--color-text)' }}>
        <Brain size={15} /> Model
      </h3>
      <p className="text-xs mb-3" style={{ color: 'var(--color-text-tertiary)' }}>
        Choose where Ghost runs its AI model. Nothing is downloaded or started until you press Start.
      </p>

      {/* Status */}
      <div
        className="flex items-start gap-2 text-sm px-3 py-2.5 rounded-lg mb-2"
        style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text-secondary)' }}
        role="status"
      >
        {ready && <CheckCircle2 size={16} className="shrink-0 mt-0.5" style={{ color: 'var(--color-success)' }} />}
        {starting && <Loader2 size={16} className="shrink-0 mt-0.5 animate-spin" style={{ color: 'var(--color-accent)' }} />}
        {status?.error && <XCircle size={16} className="shrink-0 mt-0.5" style={{ color: 'var(--color-error)' }} />}
        <span style={{ wordBreak: 'break-word', overflowWrap: 'anywhere' }}>
          {ready && 'Running — you can chat now.'}
          {notSetUp && !status?.error && 'Not set up yet. Choose a source below and press Start.'}
          {starting && (status?.detail || 'Starting…')}
          {status?.error && friendlyStartupError(status.error)}
        </span>
      </div>
      {status?.error && (
        <details className="text-xs mb-2">
          <summary className="cursor-pointer" style={{ color: 'var(--color-text-tertiary)' }}>Technical details</summary>
          <pre className="mt-1 p-2 rounded-lg overflow-auto max-h-60 whitespace-pre-wrap" style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text-secondary)' }}>{status.error}</pre>
        </details>
      )}
      {starting && (
        <p className="text-xs mb-2" style={{ color: 'var(--color-text-tertiary)' }}>
          The first start can take several minutes (Ghost installs its components and downloads a model).
          You can keep using the app meanwhile.
        </p>
      )}

      <Row label="Source" description="Where the model runs">
        <select
          value={kind}
          onChange={(e) => { setKind(e.target.value as 'ollama' | 'custom' | 'cloud'); setError(''); }}
          className="text-sm px-3 py-1.5 rounded-lg outline-none w-64"
          style={inputStyle}
        >
          <option value="ollama">On this PC (Phantom, included)</option>
          <option value="cloud">Cloud: DeepSeek, Claude, GPT…</option>
          <option value="custom">My own OpenAI-compatible server</option>
        </select>
      </Row>
      {kind === 'ollama' && (
        <p className="text-xs py-2" style={{ color: 'var(--color-text-tertiary)' }}>
          Ghost picks a model that fits your PC's memory and downloads it (a few GB) the first time.
        </p>
      )}
      {kind === 'cloud' && (
        cloudModels.length === 0 ? (
          <div className="text-xs py-3 flex flex-wrap items-center gap-2" style={{ color: 'var(--color-text-tertiary)' }}>
            No cloud provider connected yet. Add your DeepSeek (or other) API key first.
            <button
              type="button"
              onClick={() => navigate('/settings?tab=cloud')}
              className="text-xs px-2.5 py-1 rounded-lg cursor-pointer"
              style={{ color: 'var(--color-accent)', border: '1px solid var(--color-accent)' }}
            >
              Open Cloud providers
            </button>
          </div>
        ) : (
          <Row label="Model" description="Runs on the provider's servers with your API key">
            <select
              value={cloudModels.some((m) => m.id === cloudModel) ? cloudModel : ''}
              onChange={(e) => setCloudModel(e.target.value)}
              className="text-sm px-3 py-1.5 rounded-lg outline-none w-64"
              style={inputStyle}
            >
              <option value="" disabled>Choose a model…</option>
              {connectedProviders.map((p) => (
                <optgroup key={p.envKey} label={p.name}>
                  {p.models.map((m) => <option key={m.id} value={m.id}>{m.desc}</option>)}
                </optgroup>
              ))}
            </select>
          </Row>
        )
      )}
      {kind === 'custom' && (
        <>
          <Row label="Server URL" description="e.g. LM Studio: http://localhost:1234/v1">
            <input type="text" value={host} onChange={(e) => setHost(e.target.value)} placeholder="http://localhost:1234/v1"
              className="text-sm px-3 py-1.5 rounded-lg outline-none w-64" style={inputStyle} />
          </Row>
          <Row label="Model" description="Model name on that server">
            <input type="text" value={model} onChange={(e) => setModel(e.target.value)} placeholder="qwen2.5-7b-instruct"
              className="text-sm px-3 py-1.5 rounded-lg outline-none w-64" style={inputStyle} />
          </Row>
          <Row label="Server type">
            <select value={engine} onChange={(e) => setEngine(e.target.value)}
              className="text-sm px-3 py-1.5 rounded-lg outline-none w-64" style={inputStyle}>
              <option value="lmstudio">LM Studio</option>
              <option value="vllm">vLLM</option>
              <option value="sglang">SGLang</option>
              <option value="llamacpp">llama.cpp</option>
              <option value="mlx">MLX</option>
            </select>
          </Row>
          <Row label="API key (optional)" description="Only if your server requires one">
            <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="leave blank if none"
              className="text-sm px-3 py-1.5 rounded-lg outline-none w-64" style={inputStyle} />
          </Row>
        </>
      )}

      {error && <p role="alert" className="text-xs mt-3" style={{ color: 'var(--color-error)' }}>{error}</p>}

      <div className="flex justify-end mt-4">
        <button
          type="button"
          onClick={() => void start()}
          disabled={busy}
          className="flex items-center gap-2 text-sm px-4 py-2 rounded-lg cursor-pointer disabled:opacity-60"
          style={{ background: 'var(--color-accent)', color: 'white' }}
        >
          {busy && <Loader2 size={14} className="animate-spin" />}
          {notSetUp ? 'Start' : 'Save and restart'}
        </button>
      </div>
    </div>
  );
}
