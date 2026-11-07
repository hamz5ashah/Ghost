import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

describe('desktop cloud provider settings', () => {
  it('offers Atlas Cloud and DeepSeek key storage in Settings → Cloud providers', async () => {
    vi.stubGlobal('localStorage', { getItem: () => null });
    vi.stubGlobal('window', { __TAURI_INTERNALS__: {} });
    const { CloudProvidersSection } = await import('../components/CloudProvidersSection');
    const html = renderToStaticMarkup(<CloudProvidersSection />);

    for (const provider of ['Atlas Cloud', 'DeepSeek', 'Anthropic (Claude)']) {
      expect(html).toContain(provider);
    }
    const atlasKeyInput = html.match(/<input[^>]*placeholder="Atlas Cloud API key"[^>]*>/)?.[0];
    expect(atlasKeyInput).toBeDefined();
    expect(atlasKeyInput).not.toContain('disabled');
    vi.unstubAllGlobals();
  });

  it('opens Settings on the tab named in the URL', async () => {
    vi.stubGlobal('localStorage', { getItem: () => null });
    vi.stubGlobal('window', { __TAURI_INTERNALS__: {} });
    const { MemoryRouter } = await import('react-router');
    const { SettingsPage } = await import('./SettingsPage');
    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={['/settings?tab=cloud']}>
        <SettingsPage />
      </MemoryRouter>,
    );
    expect(html).toContain('Use Anthropic, DeepSeek, OpenAI and more with your own API key.');
    expect(html).toContain('placeholder="Atlas Cloud API key"');
    vi.unstubAllGlobals();
  });
});
