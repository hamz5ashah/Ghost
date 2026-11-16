import { useEffect, useRef, useState } from 'react';
import type { PresencePhase } from '../../lib/awareness';
import { stonicVideoUrl, tempoFor } from '../../lib/presence';

export type AvatarStyle = 'orb' | 'light';

const MASK = 'linear-gradient(to bottom, transparent 0%, #000 7%, #000 88%, transparent 100%)';
const AVATAR_KEY = 'ghost-presence-avatar';
/** Stonic's still of the companion; shown while the clips load, and if they never do. */
export const FEMALE_POSTER = '/stonic/images/female-light.jpg';
const CLIPS = ['light-female-1.mp4', 'light-female-1r.mp4'] as const;
const FADE_MS = 450;

/** The female companion ("light") is the default; the orb is opt-in. */
export function readAvatarStyle(): AvatarStyle {
  try { return localStorage.getItem(AVATAR_KEY) === 'orb' ? 'orb' : 'light'; } catch { return 'light'; }
}

export function saveAvatarStyle(style: AvatarStyle): void {
  try { localStorage.setItem(AVATAR_KEY, style); } catch { /* per-viewer convenience only */ }
}

/**
 * The character on the desktop. Default: Stonic's female companion — the
 * forward clip and its reversed twin played back to back (a seamless
 * ping-pong, cross-faded at each turn), masked at top and bottom, their tempo
 * following the phase (Stonic's per-phase table). The still is the poster, and
 * stays up if the clips are not shipped. "Orb": Ghost's own animated core.
 */
export function PresenceAvatar({ phase, size, style }: { phase: PresencePhase; size: number; style: AvatarStyle }) {
  const [srcs, setSrcs] = useState<(string | null)[]>([null, null]);
  const [front, setFront] = useState(0);
  const [failed, setFailed] = useState(false);
  const a = useRef<HTMLVideoElement>(null);
  const b = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (style !== 'light') return;
    let live = true;
    void Promise.all(CLIPS.map((c) => stonicVideoUrl(c))).then((u) => { if (live) setSrcs(u); });
    return () => { live = false; };
  }, [style]);

  useEffect(() => {
    for (const v of [a.current, b.current]) if (v) v.playbackRate = tempoFor(phase);
  }, [phase, srcs]);

  const pingPong = !!(srcs[0] && srcs[1]);
  const onEnded = (i: number) => {
    if (!pingPong) return;
    const next = (i + 1) % 2;
    const el = next === 0 ? a.current : b.current;
    if (el) { el.currentTime = 0; void el.play().catch(() => {}); }
    setFront(next);
  };

  if (style === 'light') {
    const box = {
      width: size, height: size, objectFit: 'cover' as const,
      WebkitMaskImage: MASK, maskImage: MASK,
    };
    const dim = phase === 'sleep' ? 0.55 : 1;
    return (
      <div style={{ width: size, height: size, position: 'relative', opacity: dim, transition: 'opacity .8s' }} aria-hidden="true">
        <img src={FEMALE_POSTER} alt="" style={{ ...box, position: 'absolute', inset: 0 }} />
        {!failed && srcs[0] && (
          [a, b].map((ref, i) => (srcs[i] ? (
            <video
              key={i}
              ref={ref}
              src={srcs[i]!}
              poster={FEMALE_POSTER}
              autoPlay={i === 0}
              loop={!pingPong}
              muted
              playsInline
              onEnded={() => onEnded(i)}
              onError={() => setFailed(true)}
              style={{
                ...box, position: 'absolute', inset: 0,
                opacity: front === i ? 1 : 0, transition: `opacity ${FADE_MS}ms ease`,
              }}
            />
          ) : null))
        )}
      </div>
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
