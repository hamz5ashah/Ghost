import { useState } from 'react';
import { RotateCw, Settings, Sparkles } from 'lucide-react';
import { useNavigate } from 'react-router';
import { refreshDesktopStatus, useDesktopStatus } from '../../hooks/useDesktopStatus';
import { startBackend } from '../../lib/api';

/**
 * A one-line, user-facing summary of a startup error. Raw tracebacks and
 * multi-line logs never reach the main screen; they stay in Settings → AI Model.
 */
export function friendlyStartupError(error: string): string {
  const first = error.split('\n').map((l) => l.trim()).find(Boolean) ?? '';
  if (!first || /traceback|^file "|exception|error:|\bat line\b/i.test(first) || first.length > 160) {
    return 'Something went wrong while starting the AI engine.';
  }
  return first;
}

/**
 * Desktop only: minimal status under the title bar while no model is running.
 * Connecting shows a small pulse; an error shows a compact card with Retry;
 * all technical detail (engine, model, server, logs) lives in Settings.
 */
export function DesktopStatusBanner() {
  const status = useDesktopStatus();
  const navigate = useNavigate();
  const [retrying, setRetrying] = useState(false);
  if (!status) return null;
  if (status.phase === 'ready' && !status.error) return null;

  const openSettings = () => navigate('/settings?tab=model');

  if (status.error) {
    const retry = async () => {
      setRetrying(true);
      try {
        await startBackend();
      } catch {
        /* the next status poll shows any new error */
      } finally {
        setRetrying(false);
        refreshDesktopStatus();
      }
    };
    return (
      <div
        role="alert"
        className="mx-4 mt-3 flex items-center gap-3 rounded-xl px-4 py-3 text-sm shrink-0"
        style={{
          background: 'color-mix(in srgb, var(--color-error) 8%, transparent)',
          border: '1px solid color-mix(in srgb, var(--color-error) 20%, transparent)',
          color: 'var(--color-text)',
        }}
      >
        <span className="min-w-0 flex-1">
          <span className="font-medium">Ghost couldn't start.</span>{' '}
          <span style={{ color: 'var(--color-text-secondary)' }}>{friendlyStartupError(status.error)}</span>
        </span>
        <button
          onClick={openSettings}
          className="text-xs cursor-pointer shrink-0"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          Details
        </button>
        <button
          onClick={() => void retry()}
          disabled={retrying}
          className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium cursor-pointer shrink-0"
          style={{ background: 'var(--color-accent)', color: 'white', opacity: retrying ? 0.6 : 1 }}
        >
          <RotateCw size={13} className={retrying ? 'animate-spin' : ''} /> Retry
        </button>
      </div>
    );
  }

  if (status.requires_source) {
    return (
      <div className="flex items-center gap-3 pl-4 pr-14 py-2 text-sm shrink-0" role="status">
        <Sparkles size={14} className="shrink-0" style={{ color: 'var(--color-accent)' }} />
        <span className="min-w-0 truncate">Set up an AI model to start chatting.</span>
        <button
          onClick={openSettings}
          className="flex items-center gap-1.5 text-sm cursor-pointer ml-auto shrink-0"
          style={{ color: 'var(--color-accent)' }}
        >
          <Settings size={13} /> Set up model
        </button>
      </div>
    );
  }

  // Connecting in the background: just a quiet pulse, no technical steps.
  return (
    <div
      className="flex items-center justify-center gap-2 py-1.5 text-xs shrink-0"
      style={{ color: 'var(--color-text-tertiary)' }}
      role="status"
      aria-label="Ghost is connecting"
    >
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60" style={{ background: 'var(--color-accent)' }} />
        <span className="relative inline-flex h-2 w-2 rounded-full" style={{ background: 'var(--color-accent)' }} />
      </span>
      Connecting…
    </div>
  );
}
