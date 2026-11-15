import { useEffect, useRef, useState } from 'react';
import type { PresencePhase } from '../../lib/awareness';
import { stonicVideoUrl, tempoFor } from '../../lib/presence';

export type AvatarStyle = 'orb' | 'light';

const MASK = 'linear-gradient(to bottom, transparent 0%, #000 7%, #000 88%, transparent 100%)';
const AVATAR_KEY = 'ghost-presence-avatar';

export function readAvatarStyle(): AvatarStyle {
  try { return localStorage.getItem(AVATAR_KEY) === 'light' ? 'light' : 'orb'; } catch { return 'orb'; }
}

export function saveAvatarStyle(style: AvatarStyle): void {
  try { localStorage.setItem(AVATAR_KEY, style); } catch { /* per-viewer convenience only */ }
}

/**
 * The character on the desktop. Default: Ghost's own orb. "Light": Stonic's
 * recovered light-figure clip, masked at top and bottom, its tempo following
 * the phase (Stonic's per-phase table). Falls back to the orb if the clip is
 * not shipped or fails to play.
 */
export function PresenceAvatar({ phase, size, style }: { phase: PresencePhase; size: number; style: AvatarStyle }) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (style !== 'light') return;
    let live = true;
    void stonicVideoUrl('light-female-1.mp4').then((u) => { if (live) setSrc(u); });
    return () => { live = false; };
  }, [style]);

  useEffect(() => {
    if (video.current) video.current.playbackRate = tempoFor(phase);
  }, [phase, src]);

  if (style === 'light' && src && !failed) {
    return (
      <video
        ref={video}
        src={src}
        autoPlay
        loop
        muted
        playsInline
        onError={() => setFailed(true)}
        aria-hidden="true"
        style={{
          width: size, height: size, objectFit: 'cover',
          WebkitMaskImage: MASK, maskImage: MASK,
          opacity: phase === 'sleep' ? 0.55 : 1, transition: 'opacity .8s',
        }}
      />
    );
  }

  const scale = (size * 0.78) / 132;
  return (
    <div style={{ width: size, height: size, display: 'grid', placeItems: 'center' }} aria-hidden="true">
      <div
        className={`butler-core presence-orb presence-${phase} ${phase === 'think' ? 'voice-thinking' : ''}`}
        style={{ transform: `scale(${scale})` }}
      >
        <span className="butler-ring butler-ring-outer" />
        <span className="butler-ring butler-ring-mid" />
        <span className="butler-ring butler-ring-inner" />
        <span className="butler-glow" />
      </div>
    </div>
  );
}
