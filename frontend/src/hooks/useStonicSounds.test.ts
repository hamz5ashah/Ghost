import { describe, expect, it } from 'vitest';

import { SOUNDS } from '../lib/stonic-sounds';
import { INTERACTIVE, cueForTarget } from './useStonicSounds';

describe('INTERACTIVE selector', () => {
  it('covers the controls a user can activate', () => {
    for (const part of ['button', '[role="button"]', 'a[href]', 'summary', '[data-sound]']) {
      expect(INTERACTIVE).toContain(part);
    }
  });
});

describe('cueForTarget', () => {
  it('returns null for a non-interactive target', () => {
    expect(cueForTarget(null, 'click')).toBeNull();
    expect(cueForTarget(null, 'hover')).toBeNull();
  });

  it('plays the generic click cue on an ordinary control', () => {
    expect(cueForTarget({}, 'click')).toBe('click');
  });

  it('plays the hover cue on an ordinary control', () => {
    expect(cueForTarget({}, 'hover')).toBe('hoverButton');
  });

  it('stays silent on a disabled control', () => {
    expect(cueForTarget({ disabled: true }, 'click')).toBeNull();
    expect(cueForTarget({ disabled: true }, 'hover')).toBeNull();
  });

  it('stays silent on an aria-disabled control', () => {
    expect(cueForTarget({ ariaDisabled: true }, 'click')).toBeNull();
    expect(cueForTarget({ ariaDisabled: true }, 'hover')).toBeNull();
  });

  it('honours a named data-sound cue on click', () => {
    expect(cueForTarget({ sound: 'success' }, 'click')).toBe('success');
    expect(cueForTarget({ sound: 'dataScan' }, 'click')).toBe('dataScan');
  });

  it('does not hover-sound an element that names its own cue', () => {
    // Otherwise sweeping the pointer across a form would sound like using it.
    expect(cueForTarget({ sound: 'success' }, 'hover')).toBeNull();
  });

  it('treats a bare data-sound attribute as opt-in, not a cue name', () => {
    // <div data-sound> parses as sound === "", and React renders
    // data-sound={true} as "true". Neither names a clip, so such an element
    // opts into the default cues rather than going silent — that is the point
    // of putting [data-sound] in the INTERACTIVE selector.
    expect(cueForTarget({ sound: '' }, 'click')).toBe('click');
    expect(cueForTarget({ sound: '' }, 'hover')).toBe('hoverButton');
    expect(cueForTarget({ sound: 'true' }, 'click')).toBe('click');
    expect(cueForTarget({ sound: 'true' }, 'hover')).toBe('hoverButton');
  });

  it('passes through a null data-sound', () => {
    expect(cueForTarget({ sound: null }, 'click')).toBe('click');
    expect(cueForTarget({ sound: null }, 'hover')).toBe('hoverButton');
  });

  it('only ever names a real clip', () => {
    const names = new Set(Object.keys(SOUNDS));
    for (const fallback of ['click', 'hover'] as const) {
      const cue = cueForTarget({}, fallback);
      expect(cue).not.toBeNull();
      expect(names.has(cue as string)).toBe(true);
    }
  });

  it('disabled beats a named cue', () => {
    expect(cueForTarget({ sound: 'success', disabled: true }, 'click')).toBeNull();
  });
});
