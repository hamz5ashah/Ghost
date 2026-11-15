import { useEffect } from 'react';
import { useAppStore } from '../lib/store';
import { getStonicFlags } from '../lib/stonic-flags';
import { fetchWakeEvents, readWakeEvents } from '../lib/realtime-voice';

/**
 * Open Voice Mode when the wake word is heard.
 *
 * Inert unless the `realtime_voice` and `wake_word` flags are on and the
 * realtime engine is selected; the listener itself is started from
 * Settings → Assistant. Polls slowly while the listener is not running.
 */
export function useWakeWord(): void {
  const engine = useAppStore((s) => s.settings.conversationEngine);
  const setVoiceModeOpen = useAppStore((s) => s.setVoiceModeOpen);

  useEffect(() => {
    if (engine !== 'realtime') return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let after = -1; // -1 = skip whatever happened before the app opened

    const tick = async () => {
      let delay = 5000;
      try {
        const batch = await fetchWakeEvents(Math.max(after, 0));
        if (after < 0) {
          after = batch.last_seq || 0;
        } else {
          const { last, woke } = readWakeEvents(batch, after);
          after = last;
          if (woke && !useAppStore.getState().voiceModeOpen) setVoiceModeOpen(true);
        }
        if (batch.running) delay = 800;
      } catch { /* server busy or route refused: try again later */ }
      if (live) timer = setTimeout(() => void tick(), delay);
    };

    void getStonicFlags().then((flags) => {
      if (live && flags.realtime_voice && flags.wake_word) void tick();
    });
    return () => { live = false; clearTimeout(timer); };
  }, [engine, setVoiceModeOpen]);
}
