import { useEffect, useState } from 'react';
import {
  fetchModels,
  fetchRecommendedModel,
  getSetupStatus,
  isTauri,
  type SetupStatus,
} from '../lib/api';
import { isEmbedOnlyModel } from '../lib/model-capabilities';
import { useAppStore } from '../lib/store';

// One shared poller for the desktop backend's status (setup/boot progress),
// used by the top banner and Settings → Model. Fast while starting, slow once
// running.
let current: SetupStatus | null = null;
let wasReady = false;
let timer: ReturnType<typeof setTimeout> | null = null;
const listeners = new Set<(s: SetupStatus | null) => void>();

const isReady = (s: SetupStatus | null) => !!s && s.phase === 'ready' && !s.error;

/** When the backend first becomes ready: load models and pick a default. */
async function onBackendReady(): Promise<void> {
  try {
    const [models, rec] = await Promise.all([
      fetchModels().catch(() => []),
      fetchRecommendedModel().catch(() => ({ model: '', reason: '' })),
    ]);
    const store = useAppStore.getState();
    store.setModels(models);
    store.setModelsLoading(false);
    const chatModels = models.filter((m) => !isEmbedOnlyModel(m.id));
    const selectedStillExists = chatModels.some((m) => m.id === store.selectedModel);
    if (!selectedStillExists) {
      const recommended = rec.model && chatModels.some((m) => m.id === rec.model)
        ? rec.model
        : chatModels[0]?.id || '';
      if (recommended) store.setSelectedModel(recommended);
    }
  } catch {
    // Non-fatal: the model picker refreshes on its own later.
  }
}

async function tick(): Promise<void> {
  const s = await getSetupStatus();
  current = s;
  const ready = isReady(s);
  if (ready && !wasReady) void onBackendReady();
  wasReady = ready;
  listeners.forEach((fn) => fn(s));
  if (listeners.size > 0) timer = setTimeout(tick, ready ? 10_000 : 1_500);
  else timer = null;
}

/** Live desktop backend status, or null outside the desktop app. */
export function useDesktopStatus(): SetupStatus | null {
  const [status, setStatus] = useState<SetupStatus | null>(current);
  useEffect(() => {
    if (!isTauri()) return;
    listeners.add(setStatus);
    if (!timer) void tick();
    return () => {
      listeners.delete(setStatus);
    };
  }, []);
  return status;
}

/** Ask for an immediate refresh (e.g. right after pressing Start). */
export function refreshDesktopStatus(): void {
  if (!isTauri()) return;
  if (timer) clearTimeout(timer);
  void tick();
}
