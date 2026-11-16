import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../lib/store', () => ({
  useAppStore: (selector: (state: Record<string, unknown>) => unknown) => selector({
    settings: { conversationEngine: 'ghost', realtimeBrain: '', realtimeVoiceMode: 'tts' },
    updateSettings: vi.fn(),
  }),
}));

import { RealtimeVoiceSection } from './RealtimeVoiceSection';

describe('RealtimeVoiceSection', () => {
  it('renders nothing until the realtime_voice flag is known to be on', () => {
    // Flags resolve asynchronously and start all-off, so the first render
    // leaves Settings → Assistant exactly as it was before the port.
    expect(renderToStaticMarkup(<RealtimeVoiceSection />)).toBe('');
  });
});
