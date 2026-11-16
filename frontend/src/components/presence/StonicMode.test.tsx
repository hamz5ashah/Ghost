import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../lib/store', () => ({
  useAppStore: (pick: (s: { settings: object; updateSettings: () => void }) => unknown) =>
    pick({ settings: {}, updateSettings: () => {} }),
}));

import { FEMALE_POSTER, PresenceAvatar, readAvatarStyle, saveAvatarStyle } from './PresenceAvatar';
import { ALWAYS_ACTIVE, StonicModeSection } from './StonicModeSection';
import { buildSystemPrompt } from '../../lib/assistant-prompt';

describe('phase 6: female companion and Always-Active', () => {
  it('the female companion is the default avatar, the orb opt-in', () => {
    // No storage at all (or a blocked one) also means the companion.
    expect(readAvatarStyle()).toBe('light');
    const mem = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => void mem.set(k, v),
    });
    expect(readAvatarStyle()).toBe('light');
    saveAvatarStyle('orb');
    expect(readAvatarStyle()).toBe('orb');
    vi.unstubAllGlobals();
  });

  it('shows the companion still before the clips load', () => {
    const html = renderToStaticMarkup(<PresenceAvatar phase="idle" size={150} style="light" />);
    expect(html).toContain(FEMALE_POSTER);
    expect(html).not.toContain('butler-core');
    expect(renderToStaticMarkup(<PresenceAvatar phase="idle" size={150} style="orb" />)).toContain('butler-core');
  });

  it('renders the one switch, disabled until the server answers', () => {
    const html = renderToStaticMarkup(<StonicModeSection />);
    expect(html).toContain('Always-Active Stonic Mode');
    expect(html).toContain('disabled');
  });

  it('turns on the seven flags and never computer use', () => {
    expect([...ALWAYS_ACTIVE].sort()).toEqual(
      ['awareness', 'characters', 'desktop_presence', 'memory_v2', 'realtime_voice', 'ui_sounds', 'wake_word'],
    );
    expect(ALWAYS_ACTIVE).not.toContain('computer_use' as never);
  });

  it('the companion persona adds warmth, not a false identity', () => {
    const base = { responseStyle: 'balanced', aboutUser: '', customInstructions: '' } as const;
    const prompt = buildSystemPrompt({ ...base, persona: 'companion' } as never);
    expect(prompt).toContain('warm, close companion');
    expect(prompt).not.toMatch(/never (say|admit).*AI|Stonic|Usman/i);
    expect(buildSystemPrompt({ ...base, persona: 'professional' } as never)).not.toContain('companion');
  });
});
