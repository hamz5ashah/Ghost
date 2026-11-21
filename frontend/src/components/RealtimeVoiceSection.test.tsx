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
  it('offers to turn the Stonic voice on while the flag is off', () => {
    // Flags start all-off, so the first render is the opt-in card: the
    // realtime engine settings appear only after the user turns it on.
    const html = renderToStaticMarkup(<RealtimeVoiceSection />);
    expect(html).toContain('Turn on Stonic voice');
    expect(html).toContain('Gemini API key');
    expect(html).not.toContain('Conversation engine');
  });
});
