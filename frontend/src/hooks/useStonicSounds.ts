// Wires Stonic's UI sounds to interface events (Phase 1 of
// /STONIC_PORT_PLAN.md).
//
// Sounds are attached by delegation at the document root rather than threaded
// through every component: that keeps the port to one call site, and means the
// whole feature is genuinely inert when `ui_sounds` is off — no listeners are
// registered at all, so there is nothing to regress for users who never turn
// it on.
//
// The cue decision is a pure function (`cueForTarget`) so it can be tested
// without a DOM, which this suite does not have.

import { useEffect } from 'react';

import { getStonicFlags } from '../lib/stonic-flags';
import { play, stopAllLoops, type SoundName } from '../lib/stonic-sounds';

/** Elements that should click/hover audibly, matching Stonic's own behaviour. */
export const INTERACTIVE = 'button, [role="button"], a[href], summary, [data-sound]';

/** What the cue decision needs to know about an element. */
export interface SoundTarget {
  /** `data-sound` attribute, if any. */
  sound?: string | null;
  disabled?: boolean;
  ariaDisabled?: boolean;
}

/**
 * Which cue an element should make, or `null` for silence.
 *
 * An element can name its own cue with `data-sound="success"`. On hover, a
 * named element stays silent: its cue is meant for the click, and firing it on
 * hover would make passing the pointer over a form sound like using it.
 */
export function cueForTarget(
  target: SoundTarget | null,
  fallback: 'click' | 'hover',
): SoundName | null {
  if (!target) return null;
  if (target.disabled || target.ariaDisabled) return null;
  const named = target.sound;
  // `data-sound` with no value (or React's `data-sound={true}`) only opts an
  // element in; it does not name a clip, so the defaults apply.
  const namesACue = Boolean(named) && named !== 'true';
  if (fallback === 'hover') return namesACue ? null : 'hoverButton';
  return namesACue ? (named as SoundName) : 'click';
}

function readTarget(event: Event): SoundTarget | null {
  const node = event.target;
  if (!(node instanceof Element)) return null;
  const el = node.closest<HTMLElement>(INTERACTIVE);
  if (!el) return null;
  return {
    sound: el.dataset.sound ?? null,
    disabled: el.hasAttribute('disabled'),
    ariaDisabled: el.getAttribute('aria-disabled') === 'true',
  };
}

/**
 * Attach the delegated listeners. Exported for direct use and returns a detach
 * function; `useStonicSounds` is the React entry point.
 */
export function attachSoundListeners(doc: Document): () => void {
  const handler = (fallback: 'click' | 'hover') => (event: Event) => {
    const cue = cueForTarget(readTarget(event), fallback);
    if (cue) play(cue);
  };
  const onClick = handler('click');
  // `pointerover` fires once per element entered, unlike mousemove.
  const onOver = handler('hover');
  doc.addEventListener('click', onClick, { capture: true, passive: true });
  doc.addEventListener('pointerover', onOver, { capture: true, passive: true });
  return () => {
    doc.removeEventListener('click', onClick, { capture: true });
    doc.removeEventListener('pointerover', onOver, { capture: true });
  };
}

export function useStonicSounds(): void {
  useEffect(() => {
    let cancelled = false;
    let detach: (() => void) | undefined;

    void getStonicFlags().then((flags) => {
      if (cancelled || !flags.ui_sounds) return;
      if (typeof document === 'undefined') return;
      detach = attachSoundListeners(document);
    });

    return () => {
      cancelled = true;
      detach?.();
      stopAllLoops();
    };
  }, []);
}
