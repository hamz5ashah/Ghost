import { Loader2, Settings, XCircle, Sparkles } from 'lucide-react';
import { useNavigate } from 'react-router';
import { useDesktopStatus } from '../../hooks/useDesktopStatus';

/**
 * Desktop only: a slim banner under the title bar while no model is running —
 * "set up a model", "starting…", or the startup error — linking to
 * Settings → Model. Hidden once the model is ready.
 */
export function DesktopStatusBanner() {
  const status = useDesktopStatus();
  const navigate = useNavigate();
  if (!status) return null;
  const ready = status.phase === 'ready' && !status.error;
  if (ready) return null;

  const tone = status.error ? 'var(--color-error)' : 'var(--color-accent)';
  let icon = <Sparkles size={14} className="shrink-0" style={{ color: tone }} />;
  let text = 'Set up an AI model to start chatting.';
  let action = 'Set up model';
  if (status.error) {
    icon = <XCircle size={14} className="shrink-0" style={{ color: tone }} />;
    text = `Ghost couldn't start the model: ${status.error.split('\n')[0]}`;
    action = 'Fix in Settings';
  } else if (!status.requires_source) {
    icon = <Loader2 size={14} className="shrink-0 animate-spin" style={{ color: tone }} />;
    text = status.detail || 'Starting Ghost…';
    action = 'Details';
  }

  return (
    <div
      // pr-14 keeps the button clear of the approvals bell in the top-right corner.
      className="flex items-center gap-3 pl-4 pr-14 py-2 text-sm shrink-0"
      style={{
        background: `color-mix(in srgb, ${tone} 8%, transparent)`,
        borderBottom: `1px solid color-mix(in srgb, ${tone} 15%, transparent)`,
        color: 'var(--color-text)',
      }}
      role="status"
    >
      {icon}
      <span className="min-w-0 truncate" title={status.error ?? text}>{text}</span>
      <button
        onClick={() => navigate('/settings?tab=model')}
        className="flex items-center gap-1.5 text-sm cursor-pointer ml-auto shrink-0"
        style={{ color: 'var(--color-accent)' }}
      >
        <Settings size={13} /> {action}
      </button>
    </div>
  );
}
