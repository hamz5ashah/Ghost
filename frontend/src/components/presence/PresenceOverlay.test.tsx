import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../lib/store', () => ({
  useAppStore: (pick: (s: { settings: object }) => unknown) => pick({ settings: {} }),
}));

import { PresenceOverlay } from './PresenceOverlay';
import { AwarenessSection } from './AwarenessSection';

describe('desktop presence UI', () => {
  it('renders the mini notch first', () => {
    const html = renderToStaticMarkup(<PresenceOverlay initialSize="m" />);
    expect(html).toContain('presence-notch');
    expect(html).toContain('Ghost');
    expect(html).not.toContain('presence-panel');
  });

  it('settings render nothing while both flags are off', () => {
    expect(renderToStaticMarkup(<AwarenessSection />)).toBe('');
  });
});
