import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Check, ExternalLink, Mic, MoreHorizontal, BellOff, LogOut } from 'lucide-react';
import { THOUGHT_TTL_MS, type PresencePhase } from '../../lib/awareness';
import {
  CAPTION, NOTCH, PANEL_WIDTH, STAGE, hitRect, onPresenceEvent, presenceApi,
  type PresenceInfo, type PresencePrefs, type PresenceSize, type PresenceSnapshot,
} from '../../lib/presence';
import { PresenceAvatar, readAvatarStyle, saveAvatarStyle, type AvatarStyle } from './PresenceAvatar';

const COLLAPSE_MS = 600;
const SIZES: Array<[PresenceSize, string]> = [['s', 'Small'], ['m', 'Medium'], ['l', 'Large']];

const DOT: Record<PresencePhase, string> = {
  idle: '#5b8cff', listen: '#38d39f', think: '#f5b942', speak: '#7aa7ff', sleep: '#6b7280', wake: '#f5b942',
};

const EMPTY: PresenceSnapshot = { phase: 'idle', caption: '', thought: '', thoughtAt: 0, mood: '', voiceOpen: false };

/**
 * `?mode=presence` — the notch that stays at the top of the screen while
 * Ghost is minimised (Stonic's "Live on desktop"). A 236×38 pill that opens
 * into a panel on hover; the rest of the window lets clicks through.
 */
export function PresenceOverlay({ initialSize }: { initialSize: PresenceSize }) {
  const [snap, setSnap] = useState<PresenceSnapshot>(EMPTY);
  const [hover, setHover] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [menu, setMenu] = useState(false);
  const [visible, setVisible] = useState(true);
  const [size, setSize] = useState<PresenceSize>(initialSize);
  const [info, setInfo] = useState<PresenceInfo | null>(null);
  const [avatar, setAvatar] = useState<AvatarStyle>(readAvatarStyle);
  const [now, setNow] = useState(Date.now());
  const collapse = useRef<number | null>(null);

  useLayoutEffect(() => {
    document.documentElement.classList.add('presence-window');
  }, []);

  useEffect(() => {
    const subs = [
      onPresenceEvent<PresenceSnapshot>('presence:snapshot', setSnap),
      onPresenceEvent<{ hover: boolean }>('presence:hover', (p) => setHover(!!p.hover)),
      onPresenceEvent<{ visible: boolean }>('presence:visible', (p) => setVisible(!!p.visible)),
      onPresenceEvent<PresencePrefs>('presence:prefs', (p) => setSize(p.size)),
    ];
    void presenceApi.state().then(setInfo);
    const tick = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => {
      window.clearInterval(tick);
      subs.forEach((s) => void s.then((off) => off()));
    };
  }, []);

  // Hover opens the panel; leaving closes it after a beat (not while the menu is open).
  useEffect(() => {
    if (collapse.current) { window.clearTimeout(collapse.current); collapse.current = null; }
    if (hover) { setExpanded(true); return; }
    if (menu) return;
    collapse.current = window.setTimeout(() => setExpanded(false), COLLAPSE_MS);
  }, [hover, menu]);

  // Keep the clickable area in step with what is drawn.
  useEffect(() => {
    void presenceApi.hit(visible ? hitRect(expanded, size, menu) : null);
  }, [expanded, size, menu, visible]);

  const setPrefs = useCallback(async (patch: Partial<PresencePrefs>) => {
    const next = await presenceApi.setPrefs(patch);
    if (next) { setInfo(next); setSize(next.prefs.size); }
  }, []);

  const stage = STAGE[size];
  const thought = snap.thought && now - snap.thoughtAt < THOUGHT_TTL_MS ? snap.thought : '';
  const caption = snap.caption || (snap.phase === 'listen' ? 'Listening…' : snap.phase === 'sleep' ? 'Resting' : 'Ghost');

  return (
    <div className={`presence-root ${visible ? 'is-visible' : ''}`}>
      {!expanded ? (
        <div className="presence-notch" style={{ width: NOTCH.width, height: NOTCH.height }}>
          <span className="presence-dot" style={{ background: DOT[snap.phase] }} />
          <span className="presence-caption" dir="auto">{caption}</span>
          <button className="presence-btn" title="Talk" onClick={() => void presenceApi.command('toggle')}>
            <Mic size={14} />
          </button>
        </div>
      ) : (
        <div className="presence-panel" style={{ width: PANEL_WIDTH }}>
          <div className="presence-stage" style={{ height: stage }}>
            <PresenceAvatar phase={snap.phase} size={stage} style={avatar} />
          </div>
          <div className="presence-caption presence-caption-wide" dir="auto" style={{ height: CAPTION }}>{caption}</div>
          <div className="presence-bar">
            <button className={`presence-btn ${snap.voiceOpen ? 'is-on' : ''}`} title={snap.voiceOpen ? 'End conversation' : 'Talk'}
              onClick={() => void presenceApi.command('toggle')}>
              <Mic size={15} />
            </button>
            <button className="presence-btn" title="Quiet for an hour" onClick={() => void presenceApi.command('mute')}>
              <BellOff size={15} />
            </button>
            <button className="presence-btn" title="Open Ghost" onClick={() => void presenceApi.command('open')}>
              <ExternalLink size={15} />
            </button>
            <button className={`presence-btn ${menu ? 'is-on' : ''}`} title="More" onClick={() => setMenu((m) => !m)}>
              <MoreHorizontal size={15} />
            </button>
          </div>
          {menu && (
            <div className="presence-menu" role="menu">
              <div className="presence-menu-label">Size</div>
              {SIZES.map(([s, label]) => (
                <button key={s} role="menuitemradio" aria-checked={size === s} onClick={() => void setPrefs({ size: s })}>
                  {label} {size === s && <Check size={13} />}
                </button>
              ))}
              <div className="presence-menu-label">Character</div>
              {(['orb', 'light'] as AvatarStyle[]).map((s) => (
                <button key={s} role="menuitemradio" aria-checked={avatar === s}
                  onClick={() => { saveAvatarStyle(s); setAvatar(s); }}>
                  {s === 'orb' ? 'Ghost orb' : 'Light figure'} {avatar === s && <Check size={13} />}
                </button>
              ))}
              {info?.wallpaper_supported && (
                <button role="menuitemcheckbox" aria-checked={info.prefs.wallpaper}
                  onClick={() => void setPrefs({ wallpaper: !info.prefs.wallpaper })}>
                  Living wallpaper {info.prefs.wallpaper && <Check size={13} />}
                </button>
              )}
              <button onClick={() => void presenceApi.command('leave')}>
                <LogOut size={13} /> Leave the desktop
              </button>
            </div>
          )}
        </div>
      )}
      {thought && !menu && (
        <div className="presence-thought" dir="auto" style={{ maxWidth: PANEL_WIDTH - 40 }}>{thought}</div>
      )}
    </div>
  );
}
