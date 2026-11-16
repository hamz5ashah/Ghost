import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { onPresenceEvent, wakeFor, type PresenceSnapshot } from '../../lib/presence';
import { createWallpaper, type WallpaperInputs } from '../../lib/wallpaper-gl';
import { FEMALE_POSTER } from './PresenceAvatar';

/**
 * `?mode=wallpaper` — the living wallpaper behind the desktop icons. The
 * shell attaches this window to the desktop and tells it when it can rest
 * (desktop covered, Windows locked) and when the PC is on battery. The
 * companion stands to the right; the field's light gathers around her
 * (Stonic's focus, 0.7 / 0.42).
 */
export function LivingWallpaper() {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [ready, setReady] = useState(false);
  const inputs = useRef<WallpaperInputs>({ focusX: 0.7, focusY: 0.42, wake: 0.25, ai: 0, mic: 0, battery: false });

  useLayoutEffect(() => {
    document.documentElement.classList.add('wallpaper-window');
    document.title = 'Ghost Wallpaper';
  }, []);

  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const renderer = createWallpaper(el, () => inputs.current, () => setReady(true));
    if (!renderer) setReady(true); // no WebGL: the dark backdrop still shows
    let lastPhase = '';
    const subs = [
      onPresenceEvent<PresenceSnapshot>('presence:snapshot', (s) => {
        const i = inputs.current;
        i.wake = wakeFor(s.phase, s.voiceOpen);
        i.ai = s.phase === 'speak' ? 1 : 0;
        i.mic = s.phase === 'listen' ? 1 : 0;
        // A soft flare when a conversation starts.
        if (s.voiceOpen && lastPhase && lastPhase !== 'listen' && s.phase === 'listen') renderer?.flare(0.8);
        lastPhase = s.phase;
      }),
      onPresenceEvent<{ active: boolean }>('presence:wallpaper-active', (p) => renderer?.setActive(!!p.active)),
      onPresenceEvent<{ battery: boolean }>('presence:wallpaper-power', (p) => { inputs.current.battery = !!p.battery; }),
    ];
    return () => {
      renderer?.dispose();
      subs.forEach((s) => void s.then((off) => off()));
    };
  }, []);

  return (
    <div className={`wallpaper-root ${ready ? 'is-ready' : ''}`}>
      <canvas ref={canvas} className="wallpaper-field" />
      <img src={FEMALE_POSTER} alt="" aria-hidden="true" className="wallpaper-figure" />
      <div className="wallpaper-vignette" />
    </div>
  );
}
