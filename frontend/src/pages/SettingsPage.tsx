import { useState, useEffect, useCallback, useRef } from 'react';
import {
  Palette,
  Globe,
  Cpu,
  Database,
  Info,
  Check,
  Sun,
  Moon,
  Monitor,
  Download,
  Upload,
  Trash2,
  Mic,
  Key,
  Search,
  Brain,
  RefreshCw,
  Cloud,
  Smartphone,
  Wrench,
  Activity,
  Sparkles,
} from 'lucide-react';
import { useSearchParams } from 'react-router';
import { useAppStore, type ThemeMode } from '../lib/store';
import { AboutSection } from '../components/AboutSection';
import { AssistantSection } from '../components/AssistantSection';
import {
  checkHealth,
  fetchSpeechHealth,
  fetchTtsHealth,
  getMemoryStats,
  getCloudKeyStatus,
  saveCloudKey,
  fetchToolCredentialStatus,
  saveToolCredentials,
  deleteToolCredential,
  isTauri,
  isMobileApp,
  type MemoryStats,
} from '../lib/api';
import { isAutoUpdateDisabled, setAutoUpdateDisabled } from '../components/Desktop/UpdateChecker';
import { PhoneAccessSection } from '../components/Desktop/PhoneAccessSection';
import { ModelSetupSection } from '../components/Desktop/ModelSetupSection';
import { CloudProvidersSection } from '../components/CloudProvidersSection';
import { SystemPanel } from '../components/Chat/SystemPanel';

const CLOUD_KEY_STATUS_CHANGED = 'ghost-cloud-key-status-changed';

function OllamaModelList() {
  const [models, setModels] = useState<Array<{ name: string; size: number }>>([]);
  useEffect(() => {
    fetch('http://localhost:11434/api/tags')
      .then(r => r.json())
      .then(data => setModels((data.models || []).map((m: any) => ({ name: m.name, size: m.size }))))
      .catch(() => setModels([]));
  }, []);
  if (models.length === 0) return <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>No models loaded</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {models.map(m => (
        <span key={m.name} className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px]"
          style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text)' }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--color-success)', display: 'inline-block' }} />
          {m.name} ({(m.size / 1e9).toFixed(1)} GB)
        </span>
      ))}
    </div>
  );
}

function ApiKeyInput({
  keyName,
  placeholder,
  toolName,
}: {
  keyName: string;
  placeholder: string;
  toolName?: string;
}) {
  const [value, setValue] = useState('');
  const [saved, setSaved] = useState(false);
  const [hasKey, setHasKey] = useState(false);
  const [error, setError] = useState('');
  const desktopKeyStorage = isTauri();
  const serverToolStorage = !desktopKeyStorage && !!toolName;
  const canManage = desktopKeyStorage || serverToolStorage;

  const refresh = useCallback(async () => {
    if (!canManage) {
      setHasKey(false);
      return;
    }
    try {
      const status = desktopKeyStorage
        ? await getCloudKeyStatus()
        : await fetchToolCredentialStatus(toolName!);
      setHasKey(!!status[keyName]);
    } catch {
      setHasKey(false);
    }
  }, [canManage, desktopKeyStorage, keyName, toolName]);

  useEffect(() => {
    void refresh();
    window.addEventListener(CLOUD_KEY_STATUS_CHANGED, refresh);
    return () => window.removeEventListener(CLOUD_KEY_STATUS_CHANGED, refresh);
  }, [refresh]);

  const save = async (v: string) => {
    const next = v.trim();
    if (!next) return;
    setError('');
    try {
      if (desktopKeyStorage) {
        await saveCloudKey(keyName, next);
      } else if (toolName) {
        await saveToolCredentials(toolName, { [keyName]: next });
      } else {
        return;
      }
      setValue('');
      setHasKey(true);
      setSaved(true);
      window.dispatchEvent(new Event(CLOUD_KEY_STATUS_CHANGED));
      setTimeout(() => setSaved(false), 2000);
    } catch (e: any) {
      setError(e?.message || 'Failed to save API key');
    }
  };

  const remove = async () => {
    setError('');
    try {
      if (desktopKeyStorage) {
        await saveCloudKey(keyName, '');
      } else if (toolName) {
        await deleteToolCredential(toolName, keyName);
      } else {
        return;
      }
      setValue('');
      setHasKey(false);
      setSaved(true);
      window.dispatchEvent(new Event(CLOUD_KEY_STATUS_CHANGED));
      setTimeout(() => setSaved(false), 2000);
    } catch (e: any) {
      setError(e?.message || 'Failed to remove API key');
    }
  };

  return (
    <div className="flex items-center gap-2">
      <input
        type="password"
        value={value}
        onChange={e => setValue(e.target.value)}
        onBlur={() => { if (value.trim()) void save(value); }}
        placeholder={hasKey ? (desktopKeyStorage ? 'Saved in secure storage' : 'Saved by local server') : placeholder}
        disabled={!canManage}
        className="w-48 px-2 py-1 rounded text-xs"
        style={{ background: 'var(--color-bg)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }} />
      {hasKey && (
        <button
          onClick={() => void remove()}
          className="px-2 py-1 rounded text-[10px] cursor-pointer"
          style={{ color: 'var(--color-error)', border: '1px solid var(--color-error)' }}
        >
          Remove
        </button>
      )}
      {saved && <span className="text-[10px]" style={{ color: 'var(--color-success)' }}>Saved</span>}
      {error && <span className="text-[10px]" style={{ color: 'var(--color-error)' }}>{error}</span>}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div
      className="rounded-xl p-5"
      style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}
    >
      <h3 className="text-sm font-semibold mb-4" style={{ color: 'var(--color-text)' }}>
        {title}
      </h3>
      {children}
    </div>
  );
}

function SettingRow({ label, description, children }: { label: string; description?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-3" style={{ borderBottom: '1px solid var(--color-border-subtle)' }}>
      <div className="min-w-0">
        <div className="text-sm" style={{ color: 'var(--color-text)' }}>{label}</div>
        {description && (
          <div className="text-xs mt-0.5 break-words" style={{ color: 'var(--color-text-tertiary)' }}>{description}</div>
        )}
      </div>
      <div>{children}</div>
    </div>
  );
}

type MemoryStatus =
  | { kind: 'loading' }
  | { kind: 'ready'; stats: MemoryStats }
  | { kind: 'error'; message: string };

export function MemoryStatusRow({
  status,
  onRetry,
}: {
  status: MemoryStatus;
  onRetry: () => void;
}) {
  const stats = status.kind === 'ready' ? status.stats : null;
  const description = status.kind === 'error'
    ? status.message
    : status.kind === 'loading'
      ? 'Checking memory backend...'
      : stats?.backend === 'none'
        ? 'Memory is not configured'
        : `${stats?.backend} backend — ${stats?.entries} entries`;
  const label = status.kind === 'loading'
    ? 'Checking...'
    : stats?.backend === 'none'
      ? 'Not configured'
      : stats
        ? `${stats.entries} entries`
        : 'Unavailable';

  return (
    <SettingRow label="Memory status" description={description}>
      <div className="flex items-center gap-2">
        <Brain size={14} style={{ color: stats && stats.backend !== 'none' ? 'var(--color-accent)' : 'var(--color-text-tertiary)' }} />
        <span className="text-xs whitespace-nowrap" style={{ color: 'var(--color-text-secondary)' }}>
          {label}
        </span>
        {status.kind === 'error' && (
          <button
            type="button"
            onClick={onRetry}
            className="text-xs underline cursor-pointer whitespace-nowrap"
            style={{ color: 'var(--color-accent)' }}
          >
            Retry
          </button>
        )}
      </div>
    </SettingRow>
  );
}

const themeOptions: { value: ThemeMode; label: string; icon: typeof Sun }[] = [
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'system', label: 'System', icon: Monitor },
];

type SettingsTab = 'general' | 'assistant' | 'model' | 'cloud' | 'phone' | 'tools' | 'usage' | 'data' | 'about';

const SETTINGS_TABS: Array<{
  key: SettingsTab;
  label: string;
  description: string;
  icon: React.ComponentType<{ size?: number; style?: React.CSSProperties }>;
}> = [
  { key: 'general', label: 'General', description: 'Theme and text size.', icon: Palette },
  { key: 'assistant', label: 'Assistant', description: 'How Ghost answers you: style, what it knows about you and your instructions.', icon: Sparkles },
  { key: 'model', label: 'AI Model', description: 'Where Ghost runs its model, local models and defaults.', icon: Brain },
  { key: 'cloud', label: 'Cloud providers', description: 'Use Anthropic, DeepSeek, OpenAI and more with your own API key.', icon: Cloud },
  { key: 'phone', label: 'Phone & connection', description: 'Use Ghost from your phone, and the server this app talks to.', icon: Smartphone },
  { key: 'tools', label: 'Tools & memory', description: 'Web search, memory and voice.', icon: Wrench },
  { key: 'usage', label: 'Usage & cost', description: 'Requests, tokens, energy and what the same work would cost in the cloud.', icon: Activity },
  { key: 'data', label: 'Data', description: 'Export or clear your chats and settings.', icon: Database },
  { key: 'about', label: 'About', description: 'Version, updates and license.', icon: Info },
];

export function SettingsPage() {
  // The open tab lives in the URL (?tab=cloud) so other screens can link to it.
  const [searchParams, setSearchParams] = useSearchParams();
  const visibleTabs = SETTINGS_TABS;
  const requestedTab = searchParams.get('tab') as SettingsTab | null;
  const activeTab = visibleTabs.find((t) => t.key === requestedTab) ?? visibleTabs[0];
  const tab = activeTab.key;
  const selectTab = (key: SettingsTab) => setSearchParams({ tab: key }, { replace: true });

  const settings = useAppStore((s) => s.settings);
  const updateSettings = useAppStore((s) => s.updateSettings);
  const conversations = useAppStore((s) => s.conversations);
  const serverInfo = useAppStore((s) => s.serverInfo);
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [speechBackendAvailable, setSpeechBackendAvailable] = useState<boolean | null>(null);
  const [ttsBackend, setTtsBackend] = useState<{ available: boolean; backend?: string; voice_id?: string } | null>(null);
  const [saved, setSaved] = useState(false);

  const [autoUpdateEnabled, setAutoUpdateEnabled] = useState(() => !isAutoUpdateDisabled());
  const [updateCheckState, setUpdateCheckState] = useState<'idle' | 'checking' | 'available' | 'latest'>('idle');

  const handleAutoUpdateToggle = useCallback((enabled: boolean) => {
    setAutoUpdateEnabled(enabled);
    setAutoUpdateDisabled(!enabled);
  }, []);

  const handleCheckNow = useCallback(async () => {
    if (!isTauri()) return;
    setUpdateCheckState('checking');
    try {
      const { check } = await import('@tauri-apps/plugin-updater');
      const update = await check();
      setUpdateCheckState(update ? 'available' : 'latest');
      setTimeout(() => setUpdateCheckState('idle'), 4000);
    } catch {
      setUpdateCheckState('idle');
    }
  }, []);

  const [memoryStatus, setMemoryStatus] = useState<MemoryStatus>({ kind: 'loading' });
  const memoryRequestId = useRef(0);
  const [memoryEnabled, setMemoryEnabled] = useState(() => {
    try { return localStorage.getItem('ghost-memory-enabled') !== 'false'; } catch { return true; }
  });
  const [memoryBackend, setMemoryBackend] = useState(() => {
    try { return localStorage.getItem('ghost-memory-backend') || 'sqlite'; } catch { return 'sqlite'; }
  });
  const [memoryTopK, setMemoryTopK] = useState(() => {
    try { return parseInt(localStorage.getItem('ghost-memory-top-k') || '5'); } catch { return 5; }
  });
  const [memoryMinScore, setMemoryMinScore] = useState(() => {
    try { return parseFloat(localStorage.getItem('ghost-memory-min-score') || '0.1'); } catch { return 0.1; }
  });
  const [memoryMaxTokens, setMemoryMaxTokens] = useState(() => {
    try { return parseInt(localStorage.getItem('ghost-memory-max-tokens') || '2048'); } catch { return 2048; }
  });


  const refreshMemoryStatus = useCallback(async () => {
    const requestId = ++memoryRequestId.current;
    setMemoryStatus({ kind: 'loading' });
    try {
      const stats = await getMemoryStats();
      if (requestId === memoryRequestId.current) {
        setMemoryStatus({ kind: 'ready', stats });
      }
    } catch (error) {
      if (requestId === memoryRequestId.current) {
        setMemoryStatus({
          kind: 'error',
          message: error instanceof Error ? error.message : String(error),
        });
      }
    }
  }, []);

  useEffect(() => {
    void refreshMemoryStatus();
    const onFocus = () => { void refreshMemoryStatus(); };
    window.addEventListener('focus', onFocus);
    return () => {
      window.removeEventListener('focus', onFocus);
      ++memoryRequestId.current;
    };
  }, [refreshMemoryStatus, settings.apiUrl]);

  useEffect(() => {
    checkHealth().then(setHealthy);
    fetchSpeechHealth()
      .then((h) => setSpeechBackendAvailable(h.available))
      .catch(() => setSpeechBackendAvailable(false));
    fetchTtsHealth()
      .then((h) => setTtsBackend(h))
      .catch(() => setTtsBackend({ available: false }));
  }, []);

  const showSaved = () => {
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  };

  const handleExport = () => {
    const data = localStorage.getItem('ghost-conversations') || '{}';
    const blob = new Blob([data], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `ghost-export-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleImport = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';
    input.onchange = (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (ev) => {
        try {
          const data = JSON.parse(ev.target?.result as string);
          if (data.version === 1) {
            localStorage.setItem('ghost-conversations', JSON.stringify(data));
            useAppStore.getState().loadConversations();
            showSaved();
          }
        } catch {}
      };
      reader.readAsText(file);
    };
    input.click();
  };

  const [confirmClear, setConfirmClear] = useState(false);
  const handleClear = () => {
    if (!confirmClear) {
      setConfirmClear(true);
      setTimeout(() => setConfirmClear(false), 3000);
      return;
    }
    localStorage.removeItem('ghost-conversations');
    useAppStore.getState().loadConversations();
    setConfirmClear(false);
    showSaved();
  };

  return (
    <div className="flex-1 flex min-h-0 flex-col md:flex-row">
      {/* Settings navigation */}
      <nav
        className="shrink-0 md:w-56 md:h-full md:overflow-y-auto px-3 pt-4 md:pt-8 md:pb-6 flex md:flex-col gap-1 overflow-x-auto"
        style={{ borderRight: '1px solid var(--color-border)' }}
        aria-label="Settings sections"
      >
        <div className="hidden md:block px-3 mb-4">
          <h1 className="text-lg font-semibold" style={{ color: 'var(--color-text)' }}>Settings</h1>
        </div>
        {visibleTabs.map((t) => {
          const Icon = t.icon;
          const active = tab === t.key;
          return (
            <button
              key={t.key}
              onClick={() => selectTab(t.key)}
              aria-current={active ? 'page' : undefined}
              className="flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm whitespace-nowrap cursor-pointer transition-colors text-left"
              style={{
                background: active ? 'var(--color-accent-subtle)' : 'transparent',
                color: active ? 'var(--color-text)' : 'var(--color-text-secondary)',
                fontWeight: active ? 500 : 400,
              }}
            >
              <Icon size={15} style={{ color: active ? 'var(--color-accent)' : 'var(--color-text-tertiary)' }} />
              {t.label}
            </button>
          );
        })}
      </nav>

      {/* Active section */}
      <div className="flex-1 min-w-0 overflow-y-auto px-4 md:px-8 py-6 md:py-8">
        <div className="max-w-3xl mx-auto">
          <header className="mb-6 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-xl font-semibold" style={{ color: 'var(--color-text)' }}>{activeTab.label}</h2>
              <p className="text-sm mt-1" style={{ color: 'var(--color-text-secondary)' }}>{activeTab.description}</p>
            </div>
            {saved && (
              <span className="flex items-center gap-1 text-xs px-2 py-1 rounded-full shrink-0" style={{
                background: 'var(--color-accent-subtle)',
                color: 'var(--color-success)',
              }}>
                <Check size={12} /> Saved
              </span>
            )}
          </header>

          {tab === 'general' && (
            <div className="flex flex-col gap-4">
              <Section title="Appearance">
                <SettingRow label="Theme" description="Choose how Ghost looks">
                  <div className="flex gap-1 p-0.5 rounded-lg" style={{ background: 'var(--color-bg-secondary)' }}>
                    {themeOptions.map((opt) => {
                      const isActive = settings.theme === opt.value;
                      return (
                        <button
                          key={opt.value}
                          onClick={() => { updateSettings({ theme: opt.value }); showSaved(); }}
                          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors cursor-pointer"
                          style={{
                            background: isActive ? 'var(--color-surface)' : 'transparent',
                            color: isActive ? 'var(--color-text)' : 'var(--color-text-tertiary)',
                            boxShadow: isActive ? 'var(--shadow-sm)' : 'none',
                          }}
                        >
                          <opt.icon size={14} />
                          {opt.label}
                        </button>
                      );
                    })}
                  </div>
                </SettingRow>
                <SettingRow label="Font size">
                  <select
                    value={settings.fontSize}
                    onChange={(e) => { updateSettings({ fontSize: e.target.value as any }); showSaved(); }}
                    className="text-sm px-3 py-1.5 rounded-lg outline-none cursor-pointer"
                    style={{
                      background: 'var(--color-bg-secondary)',
                      color: 'var(--color-text)',
                      border: '1px solid var(--color-border)',
                    }}
                  >
                    <option value="small">Small</option>
                    <option value="default">Default</option>
                    <option value="large">Large</option>
                  </select>
                </SettingRow>
              </Section>
            </div>
          )}

          {tab === 'assistant' && (
            <div className="flex flex-col gap-4">
              <AssistantSection />
            </div>
          )}

          {tab === 'model' && (
            <div className="flex flex-col gap-4">
              {isTauri() && <ModelSetupSection />}

              <Section title="Local models">
                <SettingRow label="Local models (Ollama)" description="Models available for local inference">
                  <OllamaModelList />
                </SettingRow>
                <div className="text-xs mt-2 px-1" style={{ color: 'var(--color-text-tertiary)' }}>
                  Run <code className="px-1 py-0.5 rounded text-[11px]" style={{ background: 'var(--color-bg-tertiary)' }}>ollama pull &lt;model-name&gt;</code> in your terminal to add more models
                </div>
              </Section>

              <Section title="Model Defaults">
                <SettingRow label="Temperature" description={`${settings.temperature}`}>
                  <input
                    type="range"
                    min="0"
                    max="2"
                    step="0.1"
                    value={settings.temperature}
                    onChange={(e) => { updateSettings({ temperature: parseFloat(e.target.value) }); showSaved(); }}
                    className="w-32 cursor-pointer accent-[var(--color-accent)]"
                  />
                </SettingRow>
                <SettingRow label="Max tokens" description={`${settings.maxTokens}`}>
                  <input
                    type="range"
                    min="256"
                    max="32768"
                    step="256"
                    value={settings.maxTokens}
                    onChange={(e) => { updateSettings({ maxTokens: parseInt(e.target.value) }); showSaved(); }}
                    className="w-32 cursor-pointer accent-[var(--color-accent)]"
                  />
                </SettingRow>
              </Section>
            </div>
          )}

          {tab === 'cloud' && (
            <div className="flex flex-col gap-4">
              <CloudProvidersSection />
            </div>
          )}

          {tab === 'phone' && (
            <div className="flex flex-col gap-4">
              {isTauri() && <PhoneAccessSection />}

              <Section title="Server connection (advanced)">
                <SettingRow label="Server status" description={serverInfo ? `${serverInfo.engine} / ${serverInfo.model}` : 'Not connected'}>
                  <div className="flex items-center gap-2">
                    <span
                      className="w-2 h-2 rounded-full"
                      style={{ background: healthy === true ? 'var(--color-success)' : healthy === false ? 'var(--color-error)' : 'var(--color-text-tertiary)' }}
                    />
                    <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                      {healthy === true ? 'Connected' : healthy === false ? 'Disconnected' : 'Checking...'}
                    </span>
                  </div>
                </SettingRow>
                <SettingRow label="API URL" description="Set if backend runs on a different port or host">
                  <input
                    type="text"
                    value={settings.apiUrl}
                    onChange={(e) => { updateSettings({ apiUrl: e.target.value }); showSaved(); }}
                    placeholder="http://localhost:8000"
                    className="text-sm px-3 py-1.5 rounded-lg outline-none w-56"
                    style={{
                      background: 'var(--color-bg-secondary)',
                      color: 'var(--color-text)',
                      border: '1px solid var(--color-border)',
                    }}
                  />
                </SettingRow>
                <SettingRow label="API key" description="Required only if the server was started with an API key">
                  <input
                    type="password"
                    value={settings.apiKey}
                    onChange={(e) => { updateSettings({ apiKey: e.target.value }); showSaved(); }}
                    placeholder="GHOST_API_KEY"
                    autoComplete="off"
                    className="text-sm px-3 py-1.5 rounded-lg outline-none w-56"
                    style={{
                      background: 'var(--color-bg-secondary)',
                      color: 'var(--color-text)',
                      border: '1px solid var(--color-border)',
                    }}
                  />
                </SettingRow>
              </Section>
            </div>
          )}

          {tab === 'tools' && (
            <div className="flex flex-col gap-4">
              <Section title="Tools">
                <SettingRow label="Web Search" description="Tavily key for web search tool">
                  <ApiKeyInput keyName="TAVILY_API_KEY" placeholder="tvly-..." toolName="web_search" />
                </SettingRow>
              </Section>

              <Section title="Memory">
                <MemoryStatusRow status={memoryStatus} onRetry={() => { void refreshMemoryStatus(); }} />
                <SettingRow label="Use memory context" description="Automatically inject relevant memories into conversations">
                  <button
                    onClick={() => {
                      const next = !memoryEnabled;
                      setMemoryEnabled(next);
                      try { localStorage.setItem('ghost-memory-enabled', String(next)); } catch {}
                      showSaved();
                    }}
                    className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
                    style={{
                      background: memoryEnabled ? 'var(--color-accent)' : 'var(--color-bg-tertiary)',
                    }}
                  >
                    <span
                      className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full transition-transform bg-white"
                      style={{
                        transform: memoryEnabled ? 'translateX(20px)' : 'translateX(0)',
                        boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                      }}
                    />
                  </button>
                </SettingRow>
                <SettingRow label="Memory backend" description="Which retrieval engine to use">
                  <select
                    value={memoryBackend}
                    onChange={(e) => {
                      setMemoryBackend(e.target.value);
                      try { localStorage.setItem('ghost-memory-backend', e.target.value); } catch {}
                      showSaved();
                    }}
                    className="text-sm px-3 py-1.5 rounded-lg outline-none cursor-pointer"
                    style={{
                      background: 'var(--color-bg-secondary)',
                      color: 'var(--color-text)',
                      border: '1px solid var(--color-border)',
                    }}
                  >
                    <option value="sqlite">sqlite</option>
                    <option value="faiss">faiss</option>
                    <option value="bm25">bm25</option>
                    <option value="colbert">colbert</option>
                    <option value="hybrid">hybrid</option>
                  </select>
                </SettingRow>
                <SettingRow label="Results to inject" description={`${memoryTopK}`}>
                  <input
                    type="range"
                    min="1"
                    max="20"
                    step="1"
                    value={memoryTopK}
                    onChange={(e) => {
                      const v = parseInt(e.target.value);
                      setMemoryTopK(v);
                      try { localStorage.setItem('ghost-memory-top-k', String(v)); } catch {}
                      showSaved();
                    }}
                    className="w-32 cursor-pointer accent-[var(--color-accent)]"
                  />
                </SettingRow>
                <SettingRow label="Min relevance score" description={`${memoryMinScore}`}>
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    value={memoryMinScore}
                    onChange={(e) => {
                      const v = parseFloat(e.target.value);
                      setMemoryMinScore(v);
                      try { localStorage.setItem('ghost-memory-min-score', String(v)); } catch {}
                      showSaved();
                    }}
                    className="w-32 cursor-pointer accent-[var(--color-accent)]"
                  />
                </SettingRow>
                <SettingRow label="Max context tokens" description={`${memoryMaxTokens}`}>
                  <input
                    type="range"
                    min="256"
                    max="8192"
                    step="256"
                    value={memoryMaxTokens}
                    onChange={(e) => {
                      const v = parseInt(e.target.value);
                      setMemoryMaxTokens(v);
                      try { localStorage.setItem('ghost-memory-max-tokens', String(v)); } catch {}
                      showSaved();
                    }}
                    className="w-32 cursor-pointer accent-[var(--color-accent)]"
                  />
                </SettingRow>
              </Section>

              <Section title="Speech">
                <SettingRow label="Speech-to-Text" description="Enable microphone input for voice dictation">
                  <button
                    onClick={() => { updateSettings({ speechEnabled: !settings.speechEnabled }); showSaved(); }}
                    className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
                    style={{
                      background: settings.speechEnabled ? 'var(--color-accent)' : 'var(--color-bg-tertiary)',
                    }}
                  >
                    <span
                      className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full transition-transform bg-white"
                      style={{
                        transform: settings.speechEnabled ? 'translateX(20px)' : 'translateX(0)',
                        boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                      }}
                    />
                  </button>
                </SettingRow>
                <SettingRow label="Text-to-Speech" description="Show a read-aloud button on assistant replies">
                  <button
                    onClick={() => { updateSettings({ voiceOutputEnabled: !settings.voiceOutputEnabled }); showSaved(); }}
                    className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
                    style={{
                      background: settings.voiceOutputEnabled ? 'var(--color-accent)' : 'var(--color-bg-tertiary)',
                    }}
                  >
                    <span
                      className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full transition-transform bg-white"
                      style={{
                        transform: settings.voiceOutputEnabled ? 'translateX(20px)' : 'translateX(0)',
                        boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                      }}
                    />
                  </button>
                </SettingRow>
                <SettingRow label="Speak replies automatically" description="Read every assistant reply aloud as soon as it finishes">
                  <button
                    onClick={() => { updateSettings({ voiceAutoplay: !settings.voiceAutoplay }); showSaved(); }}
                    disabled={!settings.voiceOutputEnabled}
                    className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
                    style={{
                      background: settings.voiceAutoplay && settings.voiceOutputEnabled ? 'var(--color-accent)' : 'var(--color-bg-tertiary)',
                      opacity: settings.voiceOutputEnabled ? 1 : 0.4,
                      cursor: settings.voiceOutputEnabled ? 'pointer' : 'default',
                    }}
                  >
                    <span
                      className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full transition-transform bg-white"
                      style={{
                        transform: settings.voiceAutoplay && settings.voiceOutputEnabled ? 'translateX(20px)' : 'translateX(0)',
                        boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                      }}
                    />
                  </button>
                </SettingRow>
                <SettingRow label="Voice" description="Backend and voice used for spoken replies">
                  <div className="flex items-center gap-2">
                    <span
                      className="w-2 h-2 rounded-full"
                      style={{
                        background: ttsBackend?.available ? 'var(--color-success)' : 'var(--color-text-tertiary)',
                      }}
                    />
                    <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                      {ttsBackend === null ? 'Checking...'
                        : ttsBackend.available ? `${ttsBackend.backend}${ttsBackend.voice_id ? ` / ${ttsBackend.voice_id}` : ''}`
                        : 'Not configured'}
                    </span>
                  </div>
                </SettingRow>
                <SettingRow label="Backend status" description="Requires Whisper, Deepgram, or another speech backend">
                  <div className="flex items-center gap-2">
                    <span
                      className="w-2 h-2 rounded-full"
                      style={{
                        background: speechBackendAvailable === true ? 'var(--color-success)'
                          : speechBackendAvailable === false ? 'var(--color-text-tertiary)'
                          : 'var(--color-text-tertiary)',
                      }}
                    />
                    <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                      {speechBackendAvailable === null ? 'Checking...'
                        : speechBackendAvailable ? 'Available'
                        : 'Not configured'}
                    </span>
                  </div>
                </SettingRow>
                {!speechBackendAvailable && speechBackendAvailable !== null && (
                  <div className="text-xs mt-2 px-1" style={{ color: 'var(--color-text-tertiary)' }}>
                    Set up a speech backend to use voice input.
                    See the <a href="https://hamz5ashah.github.io/Ghost/user-guide/tools/" target="_blank" rel="noopener noreferrer" style={{ color: 'var(--color-accent)' }}>documentation</a> for details.
                  </div>
                )}
              </Section>
            </div>
          )}

          {tab === 'usage' && (
            <div className="flex flex-col gap-4">
              <Section title="Since Ghost started">
                <SystemPanel embedded />
              </Section>
            </div>
          )}

          {tab === 'data' && (
            <div className="flex flex-col gap-4">
              <Section title="Data">
                <SettingRow label="Conversations" description={`${conversations.length} stored locally`}>
                  <div className="flex gap-2">
                    <button
                      onClick={handleExport}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer"
                      style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
                      onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--color-bg-tertiary)')}
                      onMouseLeave={(e) => (e.currentTarget.style.background = 'var(--color-bg-secondary)')}
                    >
                      <Download size={12} /> Export
                    </button>
                    <button
                      onClick={handleImport}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer"
                      style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
                      onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--color-bg-tertiary)')}
                      onMouseLeave={(e) => (e.currentTarget.style.background = 'var(--color-bg-secondary)')}
                    >
                      <Upload size={12} /> Import
                    </button>
                  </div>
                </SettingRow>
                <SettingRow label="Clear all data" description="Permanently delete all conversations">
                  <button
                    onClick={handleClear}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer"
                    style={{
                      color: confirmClear ? 'white' : 'var(--color-error)',
                      background: confirmClear ? 'var(--color-error)' : 'transparent',
                      border: '1px solid var(--color-error)',
                    }}
                    onMouseEnter={(e) => { if (!confirmClear) e.currentTarget.style.background = 'rgba(220,38,38,0.1)'; }}
                    onMouseLeave={(e) => { if (!confirmClear) e.currentTarget.style.background = 'transparent'; }}
                  >
                    <Trash2 size={12} /> {confirmClear ? 'Click again to confirm' : 'Clear'}
                  </button>
                </SettingRow>
              </Section>
            </div>
          )}

          {tab === 'about' && (
            <div className="flex flex-col gap-4">
              <AboutSection />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
