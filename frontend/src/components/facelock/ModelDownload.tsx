import { useCallback, useEffect, useState } from 'react';
import { Check, Download, Loader2 } from 'lucide-react';
import { fetchModelStatus, startModelDownload, type Bundle, type ModelStatus } from '../../lib/facelock/api';

const mb = (n: number) => `${Math.round(n / 1e6)} MB`;

/** One-time, on-demand download of a feature's models. Calls onReady once ready. */
export function ModelDownload({ bundle, onReady }: { bundle: Bundle; onReady: (ready: boolean) => void }) {
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    try {
      const s = await fetchModelStatus(bundle);
      setStatus(s);
      onReady(s.state === 'ready');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [bundle, onReady]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (status?.state !== 'downloading') return;
    const t = setInterval(() => void refresh(), 1000);
    return () => clearInterval(t);
  }, [status?.state, refresh]);

  if (!status) return error ? <p className="text-xs" style={{ color: 'var(--color-error)' }}>{error}</p> : null;
  if (status.state === 'ready') {
    return <span className="text-xs inline-flex items-center gap-1" style={{ color: 'var(--color-success)' }}><Check size={13} /> Models installed</span>;
  }
  const downloading = status.state === 'downloading';
  return (
    <div className="flex flex-col gap-1.5">
      <button
        type="button"
        disabled={downloading}
        onClick={() => { setError(''); void startModelDownload(bundle).then(() => refresh(), (e) => setError(String(e?.message ?? e))); }}
        className="self-start inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm cursor-pointer"
        style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}
      >
        {downloading ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
        {downloading
          ? `Downloading… ${mb(status.downloaded)} of ${mb(status.total)}`
          : `Download models (${mb(status.total)}, one time)`}
      </button>
      {status.state === 'error' && <p className="text-xs" style={{ color: 'var(--color-error)' }}>{status.error}</p>}
      {error && <p className="text-xs" style={{ color: 'var(--color-error)' }}>{error}</p>}
    </div>
  );
}
