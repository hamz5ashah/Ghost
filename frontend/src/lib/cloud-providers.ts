/**
 * Cloud AI providers the user can connect with their own API key.
 *
 * Keys are entered in Settings → Cloud AI providers (stored in the OS's
 * secure storage by the desktop app). The model picker only lists models of
 * providers that are connected. `envKey` is the variable the Ghost server
 * reads; model ids must match what `ghost.engine.cloud` routes.
 */
export interface CloudProvider {
  name: string;
  envKey: string;
  /** Example of what a key looks like, shown in the empty input. */
  placeholder: string;
  /** Where to create a key. */
  consoleUrl: string;
  models: Array<{ id: string; desc: string }>;
}

export const CLOUD_PROVIDERS: CloudProvider[] = [
  {
    name: 'Anthropic (Claude)',
    envKey: 'ANTHROPIC_API_KEY',
    placeholder: 'sk-ant-…',
    consoleUrl: 'https://console.anthropic.com/settings/keys',
    models: [
      { id: 'claude-sonnet-4-6', desc: 'Claude Sonnet 4.6 — balanced' },
      { id: 'claude-opus-4-6', desc: 'Claude Opus 4.6 — most capable' },
      { id: 'claude-haiku-4-5', desc: 'Claude Haiku 4.5 — fastest' },
    ],
  },
  {
    name: 'DeepSeek',
    envKey: 'DEEPSEEK_API_KEY',
    placeholder: 'sk-…',
    consoleUrl: 'https://platform.deepseek.com/api_keys',
    models: [
      { id: 'deepseek-v4-flash', desc: 'DeepSeek V4 Flash — fast, low cost' },
      { id: 'deepseek-v4-pro', desc: 'DeepSeek V4 Pro — strongest' },
    ],
  },
  {
    name: 'OpenAI',
    envKey: 'OPENAI_API_KEY',
    placeholder: 'sk-…',
    consoleUrl: 'https://platform.openai.com/api-keys',
    models: [
      { id: 'gpt-4o', desc: 'GPT-4o — fast, multimodal' },
      { id: 'gpt-4o-mini', desc: 'GPT-4o Mini — cheap, fast' },
      { id: 'o3-mini', desc: 'o3-mini — reasoning' },
    ],
  },
  {
    name: 'Google (Gemini)',
    envKey: 'GEMINI_API_KEY',
    placeholder: 'AI…',
    consoleUrl: 'https://aistudio.google.com/apikey',
    models: [
      { id: 'gemini-2.5-pro', desc: 'Gemini 2.5 Pro — flagship' },
      { id: 'gemini-2.5-flash', desc: 'Gemini 2.5 Flash — fast' },
      { id: 'gemini-3-pro', desc: 'Gemini 3 Pro — latest' },
    ],
  },
  {
    name: 'MiniMax',
    envKey: 'MINIMAX_API_KEY',
    placeholder: 'MiniMax API key',
    consoleUrl: 'https://www.minimax.io/platform',
    models: [
      { id: 'MiniMax-M3', desc: 'MiniMax M3' },
      { id: 'MiniMax-M2.7', desc: 'MiniMax M2.7' },
    ],
  },
  {
    name: 'OpenRouter',
    envKey: 'OPENROUTER_API_KEY',
    placeholder: 'sk-or-…',
    consoleUrl: 'https://openrouter.ai/keys',
    models: [
      { id: 'openrouter/auto', desc: 'Auto — best model for the task' },
      { id: 'openrouter/anthropic/claude-sonnet-4', desc: 'Claude Sonnet 4 via OpenRouter' },
      { id: 'openrouter/deepseek/deepseek-r1', desc: 'DeepSeek R1 via OpenRouter' },
    ],
  },
  {
    name: 'Atlas Cloud',
    envKey: 'ATLASCLOUD_API_KEY',
    placeholder: 'Atlas Cloud API key',
    consoleUrl: 'https://www.atlascloud.ai',
    models: [
      { id: 'atlascloud/openai/gpt-4.1-mini', desc: 'GPT-4.1 Mini via Atlas Cloud' },
      { id: 'atlascloud/deepseek-ai/deepseek-v3.2', desc: 'DeepSeek V3.2 via Atlas Cloud' },
      { id: 'atlascloud/Qwen/Qwen3-235B-A22B-Instruct-2507', desc: 'Qwen3 235B via Atlas Cloud' },
    ],
  },
];

/** Fired after a key is saved/removed so every view can refresh its status. */
export const CLOUD_KEY_STATUS_CHANGED = 'ghost-cloud-key-status-changed';
