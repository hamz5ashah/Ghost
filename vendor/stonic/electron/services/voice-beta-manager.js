'use strict';
// ═══════════════════════════════════════════════════════════════
//  VoiceBetaManager — Stonic Voice (Beta) ke engine ka maalik
//
//  Beta ek alag voice assistant hai (Voice Lab wali pipeline: sunna → dimagh →
//  jazbaati awaaz). Asal voice assistant aur agents se is ka koi taalluq nahi —
//  na un ka code chhuta hai, na un ki settings. User do mein se ek raasta
//  chunta hai:
//
//    Stonic Cloud   teeno hisse hamare gateway se, user ke plan ke andar. Koi
//                   key nahi; awaaz poori jazbaati. Shuru karne se pehle gateway
//                   se poochha jata hai (plan chalta hai? kaun se models?).
//    Custom API     user ki apni keys: dimagh Claude/Gemini/Groq, awaaz Gemini
//                   Live ("Standard", muft key par bhi) ya Gemini 3.8 TTS
//                   ("Expressive", key par requests ki tang hadd).
//
//  Is file ke do kaam:
//
//  1. Beta ki settings (voice_beta.json, DPAPI se encrypted — key-store.js):
//     raasta, chabiyan, dimagh ka provider/model, soch ki satah. Chabiyan
//     renderer ko kabhi wapas nahi jatin — sirf "lagi hai" aur aakhri 4 huroof.
//
//  2. Engine (stonic-voice/server.py, Python) ki zindagi:
//       - User Beta kholta hai → engine uthta hai (pehli dafa ~10 s: Pipecat
//         aur models load hote hain). App ke boot par KUCH nahi chalta.
//       - Settings engine ko yaad-dasht mein di jati hain (POST /beta/config),
//         disk par nahi.
//       - User Beta chhorta hai → baat-cheet foran band, chabiyan engine ki
//         yaad-dasht se saaf; engine thori der garam rehta hai (wapas aaye to
//         intezar na ho), phir band.
//       - App band = engine band. `shutdown()` poora process tree maarta hai;
//         engine khud bhi app ke marne par nikal jata hai (parent watch).
//
//  3. Screen ka kaam (dimagh Claude ho aur user ne Screen control band na kiya
//     ho): engine khud screen dekhta aur mouse / keyboard chalata hai
//     (stonic-voice/stonic_voice/screen.py). Engine stdout par batata hai ke
//     kaam shuru hua / qadam / khatam; yahan us par do cheezein hoti hain:
//       - Stonic ki apni window raaste se hat-ti hai (Beta ki screen poori app
//         ke upar khulti hai — warna dimagh ko screen par Stonic hi dikhta).
//       - Wohi glow + HUD jo asal computer agent ka hai (ComputerOverlay ka
//         ALAG instance — asal agent ka apna hai), aur us ka Esc kaam rokta hai.
//
//  4. Classic voice ka kaam (5 Nov 2026): Classic mein baat Gemini Live karta
//     hai, magar PC ka har kaam (`agent_task`) isi engine ke Claude Agent SDK
//     dimagh par jata hai (stonic-voice/stonic_voice/work.py) — wohi jo New
//     chalata hai. Yahan se `workTask` / `workStop` / `workClose` / `workStatus`;
//     engine ki "work" khabrein (qadam, report) renderer ko `voice-beta:work` par.
//     Raasta aur chabi wohi jo Stonic Voice ki Settings mein hai (Cloud ya apni
//     Claude / OpenRouter key) — har kaam ke saath engine ko jati hai, New ki
//     chalti baat-cheet ki settings ko chhuye baghair.
//
//  Engine sirf 127.0.0.1 par sunta hai aur har request par ek token maangta
//  hai jo har dafa naya banta hai. Renderer ko wohi token milta hai taake wo
//  WebRTC ka offer seedha engine ko de sake (awaaz ka raasta main process se
//  nahi guzarta).
// ═══════════════════════════════════════════════════════════════

const { spawn } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const keyStore = require('./key-store');
const voiceRuntime = require('./voice-runtime');
const { killProcessTree } = require('./computer-runtime');
const webSearch = require('./web-search');

const num = (name, dflt) => Number(process.env[name]) || dflt;

// Engine ko uthne ke liye zyada se zyada itna waqt. Garam machine par ~8 s;
// thandi disk par (boot ke foran baad, antivirus scan) kai guna — naapa (15 Oct
// 2026): dev PC par bhi bina cache 60 s. Naye PC par pehli dafa antivirus
// runtime ki ~12,000 files parhta hai; 120 s wahan kam tha.
const START_TIMEOUT_MS = num('STONIC_VOICE_START_TIMEOUT_MS', 5 * 60 * 1000);
// User Beta se nikle to engine itni der garam rahe.
const KEEP_WARM_MS = num('STONIC_VOICE_KEEP_WARM_MS', 2 * 60 * 1000);
// Classic ki baat-cheet band hui: engine (aur desk ka dimagh — pichle kaam ki yaad)
// itni der garam, taake jaldi wapas aayein to pehla kaam foran shuru ho.
const WORK_KEEP_WARM_MS = num('STONIC_VOICE_WORK_KEEP_WARM_MS', 10 * 60 * 1000);
// Classic ke kaam ke liye Cloud gateway ka "session" jawab (plan + model) itni der
// yaad: pehle HAR agent_task (chalte kaam mein hukm bhi) gateway tak jata tha (~1–3 s).
const WORK_CLOUD_TTL_MS = num('STONIC_VOICE_WORK_CLOUD_TTL_MS', 5 * 60 * 1000);
const REQUEST_TIMEOUT_MS = 20 * 1000;
const STDERR_TAIL_LINES = 40;
// Ghalti ke paigham mein stderr ki itni aakhri lines (poora Python traceback aa jaye).
const STDERR_REPORT_LINES = 20;
const STDERR_REPORT_CHARS = 4000;

const PROVIDERS = ['anthropic', 'gemini', 'groq', 'openrouter'];
// 14 Oct 2026 (Usman): Custom API par dimagh SIRF Claude — Anthropic ki apni key
// par, ya OpenRouter par (sirf Claude ke models). Dono Claude SDK chalate hain, kaam
// bhi karte hain. Groq Settings se hata (engine us ke liye ab bhi tayyar hai).
// 7 Nov 2026 (Usman): teesra dimagh Gemini — zyada tar customers muft Gemini key hi
// lagate hain. Wohi Claude Code harness (PC ka kaam, screen ke haath), model Gemini:
// engine ka pul (stonic_voice/gemini_relay.py) Claude ki zubaan Google ki zubaan mein
// badalta hai. Ek hi Gemini key par sab: awaaz + dimagh.
const AVAILABLE_PROVIDERS = ['anthropic', 'openrouter', 'gemini'];
const THINKING = ['low', 'medium', 'high'];
// Raasta: 'cloud' = Stonic Cloud, 'custom' = user ki apni keys.
const MODES = ['cloud', 'custom'];
// Custom API par awaaz: 'live' = Gemini Live (muft key par bhi, ada kam),
// 'tts' = Gemini 3.8 TTS (poori ada, key par requests ki tang hadd).
const VOICES = ['live', 'tts'];
const GATEWAY_TIMEOUT_MS = 15 * 1000;
// Har provider ka pehla model. User Settings mein badal sakta hai; list engine
// provider se khud laata hai (key ke saath), ye sirf shuruaat hai.
// Naapa hua (12 Oct 2026, poori baat-cheet ka median jawab): Claude Haiku 5.5
// 2.6 s, Gemini 3.5 Flash-Lite 2.3 s, Gemini 3.8 Flash 3.4 s.
const DEFAULT_MODELS = {
  anthropic: 'claude-haiku-5-5',
  gemini: 'gemini-3.5-flash-lite',
  groq: 'openai/gpt-oss-120b',
  // OpenRouter par wohi Claude Haiku (tez, sasta, aur PC par kaam bhi).
  openrouter: 'anthropic/claude-haiku-5.5',
};
// `openrouter` = OpenRouter dimagh ki key (Beta ki apni, warna app ki Settings wali).
const KEY_NAMES = ['gemini', 'geminiBackup', 'anthropic', 'groq', 'openrouter'];
// Screen control: 'off' = dimagh ko mouse / keyboard nahi milte; 'claude' =
// Claude pixel khud batata hai; 'uitars' = Claude cheez batata hai aur UI-TARS
// (OpenRouter key par) us ki jagah dhoondta hai.
const AIMS = ['off', 'claude', 'uitars'];
const MAX_KEY_CHARS = 400;
const MODEL_ID = /^[A-Za-z0-9._:/@-]{1,120}$/;
/** OpenRouter par sirf Claude ke models ("anthropic/…") — wohi Claude SDK chalata hai. */
const isOpenRouterClaude = (model) => String(model || '').toLowerCase().startsWith('anthropic/');

/** Ye model is provider par chal sakta hai? (Gemini par sirf Gemini ke naam — "gemini-…".) */
const allowedModel = (provider, model) => MODEL_ID.test(model)
  && (provider !== 'openrouter' || isOpenRouterClaude(model))
  && (provider !== 'gemini' || /^gemini-/i.test(model));
const CHARACTER_ID = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_PROMPT_CHARS = 6000;
// Agent (dimagh Claude ho to wo kaam bhi karta hai): renderer Stonic ke apne
// tools ka naqsha aur yaadash ka card bhejta hai. Yahan sirf haddein; har cheez
// ki asli jaanch engine karta hai (stonic_voice/config.py `_parse_agent`).
const MAX_AGENT_TOOLS = 40;
const MAX_AGENT_MEMORY_CHARS = 40000;
// Awareness ka block (is lamhe ka haal + diary) — engine bhi isi hadd par kat-ta hai (config.py).
const MAX_AWARENESS_CHARS = 8000;
// Khuli chat ki pichli baat (config.py MAX_HISTORY_* — wahan asli hadd).
const MAX_HISTORY_TURNS = 40;
const MAX_HISTORY_TURN_CHARS = 1500;
// Chat ki "notebook" (Claude Agent SDK ka session) — UUID hi. Engine isi naam se
// resume karta hai (agent.py); file STONIC_VOICE_AGENT_HOME/projects/*/<id>.jsonl.
const SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const TOOL_NAME = /^[a-z][a-z0-9_]{1,48}$/;
// Classic ka kaam (engine work.py): desk / kaam ke naam aur hukm ki lambai.
const WORK_ID = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_WORK_TEXT_CHARS = 8000;
const WORK_EVENT_KINDS = new Set(['started', 'step', 'step_failed', 'job', 'done']);
// Kaam kahan se aaya: Gemini ka diya / peeche ka command khatam / aakhir mein joda hukm (work.py).
const WORK_ORIGINS = new Set(['task', 'notice', 'followup']);
const WORK_INTENTS = new Set(['task', 'update', 'resume']);

// Chat box se aayi wo files jin ki disk par koi jagah nahi (paste ki gayi
// tasveer): dimagh unhein khol sake, is liye yahan rakhi jati hain. Har bhejne
// ka apna folder; purane folder agli dafa khud mit jate hain.
const ATTACHMENTS_DIR = 'voice-beta-attachments';
const MAX_ATTACHMENTS = 5;
const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024; // wohi hadd jo file chunne wale ki hai
const ATTACHMENT_KEEP_MS = 14 * 24 * 60 * 60 * 1000;
const EXT_BY_MIME = { 'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'image/gif': '.gif', 'image/bmp': '.bmp' };

/** Sirf file ka naam (koi folder nahi), Windows mein chalne wala; khali ho to "attachment". */
function safeFileName(name, mimeType) {
  let base = path.posix.basename(String(name || '').replace(/\\/g, '/'))
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
    .replace(/[. ]+$/, '')
    .trim();
  if (!base || /^\.+$/.test(base)) base = 'attachment';
  if (/^(con|prn|aux|nul|com\d|lpt\d)(\.|$)/i.test(base)) base = `_${base}`;
  if (!path.extname(base) && EXT_BY_MIME[mimeType]) base += EXT_BY_MIME[mimeType];
  if (base.length > 120) {
    const ext = path.extname(base).slice(0, 16);
    base = base.slice(0, 120 - ext.length) + ext;
  }
  return base;
}

const clean = (value, limit) => String(value ?? '').trim().slice(0, limit);

// Purane builds engine ki .pyc yahan banate the (user ke data folder mein).
const PYCACHE_DIR = 'voice-beta-pycache';
// Installer ki .pyc par chalte hue us purane cache ka is install wala hissa
// itni der baad mitaya jata hai (engine ke boot ke disk ke kaam se door).
const LEGACY_CACHE_SWEEP_DELAY_MS = 60 * 1000;

/**
 * Engine ko kaise chalana hai: Python ke arguments aur mahaul (environment).
 *
 * Do raaste, `runtime.bytecode` se (voice-runtime.js — installer ka nishan):
 *   installer ki .pyc  `-B`: Python kuch nahi likhta, sab .pyc installer ki
 *                      (build par bani, scripts/python-bytecode.py). Koi
 *                      PYTHONPYCACHEPREFIX nahi — wo hota to Python installer
 *                      ki .pyc ko dekhta hi nahi. `-B` flag hai, env nahi, is
 *                      liye agent ke chalaye user ke apne Python par asar nahi.
 *   nishan nahi        pehle jaisa: .pyc user ke data folder mein (dev, Mac,
 *                      purana build) — install folder mein kabhi kuch nahi likha jata.
 */
function engineLaunch({ runtime, userDataDir, baseEnv, token, parentPid, logPath, spawnedAt = Date.now() }) {
  const shipped = Boolean(runtime && runtime.bytecode);
  const env = {
    ...baseEnv,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    PYTHONUNBUFFERED: '1',
    STONIC_VOICE_TOKEN: token,
    STONIC_VOICE_PARENT_PID: String(parentPid),
    STONIC_VOICE_LOG: logPath,
    // Engine apne log mein likhta hai ke app ke chalane ke kitni der baad tayyar / garam hua.
    STONIC_VOICE_SPAWNED_AT: String(spawnedAt),
    // Agent ke dimagh (Claude Agent SDK) ka apna ghar: user ke apne Claude
    // Code (~/.claude) se bilkul alag — na us ka login, na us ki settings.
    STONIC_VOICE_AGENT_HOME: path.join(userDataDir, 'voice-beta-agent'),
  };
  if (shipped) {
    // User ke mahaul ka prefix / optimize installer ki .pyc ko be-kaar kar deta (Python doosri jagah / doosri .pyc dhoondta).
    delete env.PYTHONPYCACHEPREFIX;
    delete env.PYTHONOPTIMIZE;
  } else {
    // .pyc install folder mein nahi (wahan likhna update ke package ko
    // bigaarta hai aur kuch machines par ijazat hi nahi hoti) — user ke
    // data folder mein. Is ke baghair har dafa engine thanda uthta.
    env.PYTHONPYCACHEPREFIX = path.join(userDataDir, PYCACHE_DIR);
  }
  // Dev ka shell kabhi kabhi ye chhor jata hai; in ke saath Python ghalat jagah dekhta hai.
  delete env.PYTHONHOME;
  delete env.PYTHONPATH;
  // User ki machine par us ke apne Claude Code ki settings (koi aur server,
  // koi aur key) Stonic ke dimagh tak na pohnchen. Engine khud bhi yehi karta hai.
  for (const name of Object.keys(env)) {
    if (/^(ANTHROPIC_|CLAUDE_|CLAUDECODE)/i.test(name)) delete env[name];
  }
  return { args: shipped ? ['-B', runtime.server] : [runtime.server], env, shipped };
}

/**
 * "Start" dabane se config tak ka hisaab (engine ise sirf log karta hai). `startedAt`
 * renderer ka bheja hua waqt (ms) — ajeeb ho (purani app, ghalat ghari) to chhor diya jata hai.
 */
function startTiming({ startedAt, engineWaitMs, gatewayMs, now = Date.now() }) {
  const timing = { engine_wait_ms: Math.max(0, Math.round(engineWaitMs)) };
  if (Number.isFinite(gatewayMs)) timing.gateway_ms = Math.max(0, Math.round(gatewayMs));
  const since = now - Number(startedAt);
  if (Number.isFinite(since) && since >= 0 && since < 10 * 60 * 1000) timing.since_click_ms = Math.round(since);
  return timing;
}

/**
 * Purane cache (PYTHONPYCACHEPREFIX) mein is install ka hissa — Python har source
 * folder ka poora raasta (drive ke baghair) prefix ke neeche dohrata hai.
 * Sirf `sourceRoot` (installer ka resources folder) ka hissa; dev ya kisi aur
 * install ka cache nahi. Raasta prefix se bahar jaye to null.
 */
function legacyBytecodeDir(userDataDir, sourceRoot) {
  if (!userDataDir || !sourceRoot) return null;
  const prefix = path.join(userDataDir, PYCACHE_DIR);
  const stripped = path.resolve(sourceRoot).replace(/^[A-Za-z]:/, '').replace(/^[\\/]+/, '');
  if (!stripped) return null;
  const target = path.join(prefix, stripped);
  const rel = path.relative(prefix, target);
  if (!rel || rel.startsWith('..') || path.isAbsolute(rel)) return null;
  return { prefix, target };
}

/** Chabi mein space ya nayi line nahi hoti; copy-paste ke saath aa jaye to hata do. */
const cleanKey = (value) => String(value ?? '').replace(/\s+/g, '').slice(0, MAX_KEY_CHARS);

const hint = (key) => (key ? `…${key.slice(-4)}` : '');

class VoiceBetaManager {
  /**
   * @param {object} opts
   * @param {string} opts.userDataDir       settings aur log yahin
   * @param {() => (string|null)} [opts.getAppGeminiKey]  app ki apni Gemini key (Lifetime user ne
   *        Settings → AI Engine mein lagayi ho) — Beta mein alag key na ho to wohi chalti hai
   * @param {() => (string|null)} [opts.getAppOpenRouterKey]  app ki apni OpenRouter key (Settings → AI Engine) —
   *        Beta mein alag key na ho to screen ka nishana (UI-TARS) usi se chalta hai
   * @param {() => boolean} [opts.isAllowed]  is user ke paas app ka access hai? (main process ki apni yaad se)
   * @param {() => {signedIn: boolean, active: boolean}} [opts.getCloudState]  Stonic Cloud ka haal, main process
   *        ki apni yaad se (koi network nahi): login hai? plan chal raha hai?
   * @param {() => Promise<{ok: true, gateway: string, token: string} | {ok: false, code: string}>} [opts.getCloudAccess]
   *        gateway ka pata aur Stonic ka token (device key, warna taaza login token)
   * @param {typeof fetch} [opts.fetchImpl]  test ke liye
   * @param {() => {ok: boolean, python: string|null, server: string|null, missing: string[]}} [opts.describeRuntime]
   */
  constructor({ userDataDir, getAppGeminiKey, getAppOpenRouterKey, isAllowed, describeRuntime, getCloudState, getCloudAccess, fetchImpl, availableProviders } = {}) {
    this._available = availableProviders || AVAILABLE_PROVIDERS;
    this.userDataDir = userDataDir;
    this.configPath = path.join(userDataDir, 'voice_beta.json');
    this.logPath = path.join(userDataDir, 'logs', 'voice-beta.log');
    this._getAppGeminiKey = getAppGeminiKey || (() => null);
    this._getAppOpenRouterKey = getAppOpenRouterKey || (() => null);
    this.isAllowed = isAllowed || (() => true);
    this._describeRuntime = describeRuntime || voiceRuntime.describeRuntime;
    this._getCloudState = getCloudState || (() => ({ signedIn: false, active: false }));
    this._getCloudAccess = getCloudAccess || (async () => ({ ok: false, code: 'cloud_signin' }));
    this._fetch = fetchImpl || ((...args) => fetch(...args));
    this.mainWindow = null;
    this.child = null;
    this.state = 'stopped';         // stopped | starting | ready | error
    this.error = null;              // { code, message } — sirf state 'error' par
    this.port = null;
    this.token = null;
    this._starting = null;          // chalte hue start ka promise (do dafa na uthe)
    this._idleTimer = null;
    this._stderrTail = [];
    this._shuttingDown = false;
    this.overlay = null;            // screen ke kaam ka glow + HUD (ComputerOverlay ka apna instance)
    this._screen = null;            // chalta hua screen ka kaam: { id, maxSteps } — warna null
    this._screenRuns = 0;
    this._computerBusy = false;     // asal computer agent is waqt mouse chala raha hai
    this._legacySweep = null;       // purane .pyc cache ki safai (ek hi dafa) — timer / chalta kaam
    this._session = null;           // chalti baat-cheet ka raasta { mode, provider } — web search usi par
    this._workTasks = new Map();    // Classic ke chalte kaam: task id -> desk (engine band ho to renderer ko "nakaam")
    this._classicLive = false;      // Classic (Gemini Live) ki baat-cheet chal rahi hai — engine garam rahe
    this._workCloud = null;         // { at, cloud } — kaam ke liye Cloud gateway ka taaza jawab (WORK_CLOUD_TTL_MS)
  }

  setWindow(win) { this.mainWindow = win; }

  /** Screen ke kaam ka HUD. Us ka Esc (aur koi bhi "stop") `interruptWork()` tak aata hai. */
  setOverlay(overlay) { this.overlay = overlay || null; }

  /**
   * Nayi yaadash (file-memory/service.js). Engine Claude Agent SDK ki auto
   * memory sirf ISI service ke us waqt ke account folder par chalata hai —
   * renderer ka bheja raasta isi se milaya jata hai (`_memoryDir`).
   */
  setFileMemory(service) { this.fileMemory = service || null; }

  /** User ki skills (services/skills/library.js) — engine ko har session / kaam par taaza. */
  setSkills(library) { this.skills = library || null; }

  /**
   * Renderer ka bheja folder tab hi engine tak jata hai jab wo bilkul wohi ho
   * jo FileMemoryService abhi chala rahi hai (sign out / account badla / koi
   * aur raasta = khali). Wapsi CLI ki shakl mein: aakhir mein separator.
   */
  _memoryDir(requested) {
    const asked = clean(requested, 1024);
    if (!asked || !this.fileMemory || typeof this.fileMemory.folder !== 'function') return '';
    let actual = '';
    try { actual = String(this.fileMemory.folder() || ''); } catch { return ''; }
    if (!actual) return '';
    const norm = (p) => path.resolve(p).replace(/[\\/]+$/, '');
    const same = process.platform === 'win32'
      ? norm(asked).toLowerCase() === norm(actual).toLowerCase()
      : norm(asked) === norm(actual);
    return same ? `${norm(actual)}${path.sep}` : '';
  }

  // ── Settings ──────────────────────────────────────────────────────────────

  _read() {
    const stored = keyStore.readObject(this.configPath) || {};
    const keys = {};
    for (const name of KEY_NAMES) keys[name] = cleanKey(stored.keys?.[name]);
    const models = {};
    for (const provider of PROVIDERS) {
      const model = clean(stored.models?.[provider], 120);
      // Pehle (sab models khule the) OpenRouter par GPT waghera save hua ho to wapas Claude Haiku.
      models[provider] = allowedModel(provider, model) ? model : DEFAULT_MODELS[provider];
    }
    return {
      keys,
      provider: PROVIDERS.includes(stored.provider) ? stored.provider : null,
      models,
      thinking: THINKING.includes(stored.thinking) ? stored.thinking : 'low',
      // null = user ne abhi chuna nahi (neeche _mode tay karta hai).
      mode: MODES.includes(stored.mode) ? stored.mode : null,
      voice: VOICES.includes(stored.voice) ? stored.voice : 'live',
      // null = user ne abhi chuna nahi (neeche _aim tay karta hai).
      screen: AIMS.includes(stored.screen) ? stored.screen : null,
      captions: stored.captions !== false,
      // "Stay quiet" (baat kisi aur se ho to chup): default BAND — sirf user ke chalu karne par.
      stayQuiet: stored.stayQuiet === true,
      characterId: CHARACTER_ID.test(String(stored.characterId || '')) ? stored.characterId : null,
    };
  }

  // 6 Nov 2026: Stonic Voice ki alag Settings khatam — sab Settings → AI Engine
  // mein. Wahan Gemini aur OpenRouter ki EK EK key hai (app ki store), is liye
  // ab app wali key PEHLE: jo key screen par dikhti hai wahi chalti hai. Beta ki
  // purani apni key sirf tab jab app mein key hi na ho (warna purana setup
  // toot jata); AI Engine se key save hote hi wo bhi mita di jati hai.

  /** Wo Gemini key jo waqai chalegi: app (AI Engine) wali, warna Beta ki purani. */
  _geminiKey(config) {
    let appKey = '';
    try { appKey = cleanKey(this._getAppGeminiKey()); } catch { appKey = ''; }
    if (appKey) return { key: appKey, source: 'stonic' };
    return config.keys.gemini ? { key: config.keys.gemini, source: 'beta' } : { key: '', source: null };
  }

  /** Wo OpenRouter key jo waqai chalegi: app (AI Engine) wali, warna Beta ki purani. */
  _openRouterKey(config) {
    let appKey = '';
    try { appKey = cleanKey(this._getAppOpenRouterKey()); } catch { appKey = ''; }
    if (appKey) return { key: appKey, source: 'stonic' };
    return config.keys.openrouter ? { key: config.keys.openrouter, source: 'beta' } : { key: '', source: null };
  }

  /**
   * Screen par nishana: hamesha Claude ka apna. 14 Oct 2026 se Settings mein
   * Screen control ka chunao nahi (Off / UI-TARS hata) — pehle ka save hua
   * `screen` ab nazar-andaz hota hai. Haath phir bhi sirf us dimagh ko milte
   * hain jo kaam karta hai (neeche _enginePayload).
   */
  _aim() {
    return 'claude';
  }

  /**
   * Dimagh ka provider: user ka chuna hua (agar abhi chalta ho); na chuna ho to
   * Claude ki key ho to Claude, warna Gemini (muft key — zyada tar users isi par).
   * Sirf Claude chalta ho to hamesha Claude.
   */
  _provider(config) {
    if (config.provider && this._available.includes(config.provider)) return config.provider;
    if (this._available.includes('anthropic') && (config.keys.anthropic || this._available.length === 1)) return 'anthropic';
    return this._available.includes('gemini') ? 'gemini' : this._available[0];
  }

  _brainKey(config, provider) {
    if (provider === 'gemini') return this._geminiKey(config).key;
    if (provider === 'openrouter') return this._openRouterKey(config).key;
    return config.keys[provider] || '';
  }

  /**
   * Ye dimagh kaam bhi karta hai (agent, screen ke haath)? Claude — apni key par,
   * ya OpenRouter par Claude ka model (engine: config.openrouter_claude) — ya Gemini
   * (wohi Claude Code harness, engine ke pul se; engine: config.uses_agent).
   */
  _acts(config, provider) {
    if (provider === 'anthropic' || provider === 'gemini') return true;
    return provider === 'openrouter' && isOpenRouterClaude(config.models.openrouter);
  }

  /** Stonic Cloud ka haal (login? plan?). Poochhne mein ghalti ho to "nahi". */
  _cloudState() {
    try {
      const state = this._getCloudState() || {};
      return { signedIn: Boolean(state.signedIn), active: Boolean(state.signedIn && state.active) };
    } catch {
      return { signedIn: false, active: false };
    }
  }

  /**
   * Wo raasta jo waqai chalega: user ka chuna hua; na chuna ho to plan chalta
   * ho to Stonic Cloud, warna apni keys.
   */
  _mode(config) {
    if (config.mode) return config.mode;
    return this._cloudState().active ? 'cloud' : 'custom';
  }

  _missing(config) {
    // Stonic Cloud par koi key nahi lagti (login aur plan start() par poochhe jate hain).
    if (this._mode(config) === 'cloud') return [];
    const provider = this._provider(config);
    const missing = [];
    if (!this._geminiKey(config).key) missing.push('gemini');
    if (!this._brainKey(config, provider) && !missing.includes(provider)) missing.push(provider);
    return missing;
  }

  /** Renderer ke liye settings — is mein koi chabi nahi. */
  getConfig() {
    const config = this._read();
    const gemini = this._geminiKey(config);
    const missing = this._missing(config);
    const mode = this._mode(config);
    const cloud = this._cloudState();
    const openrouter = this._openRouterKey(config);
    return {
      ok: true,
      mode,
      modeChosen: config.mode !== null,
      voice: config.voice,
      // Screen control: sirf tab kaam aata hai jab dimagh Claude ho (wohi kaam karta hai).
      screen: { aim: this._aim(), chosen: false },
      cloud,
      keys: {
        gemini: { set: Boolean(gemini.key), hint: hint(gemini.key), source: gemini.source },
        geminiBackup: { set: Boolean(config.keys.geminiBackup), hint: hint(config.keys.geminiBackup) },
        anthropic: { set: Boolean(config.keys.anthropic), hint: hint(config.keys.anthropic) },
        groq: { set: Boolean(config.keys.groq), hint: hint(config.keys.groq) },
        openrouter: { set: Boolean(openrouter.key), hint: hint(openrouter.key), source: openrouter.source },
      },
      provider: this._provider(config),
      // Jo dimagh abhi chalte hain — baqi Settings mein "Coming soon".
      availableProviders: [...this._available],
      models: config.models,
      thinking: config.thinking,
      captions: config.captions,
      stayQuiet: config.stayQuiet,
      characterId: config.characterId,
      missing,
      // Cloud par "tayyar" = login hai. Plan na ho to screen khud batati hai
      // (cloud.active) aur gateway start() par pakka jawab deta hai.
      ready: mode === 'cloud' ? cloud.signedIn : missing.length === 0,
    };
  }

  /**
   * Settings ka kuch hissa badlo. Jo field na bheji jaye wo waisi rehti hai;
   * chabi ke liye khali string = "hata do".
   */
  saveConfig(patch = {}) {
    const config = this._read();
    if (patch.keys && typeof patch.keys === 'object') {
      for (const name of KEY_NAMES) {
        if (typeof patch.keys[name] === 'string') config.keys[name] = cleanKey(patch.keys[name]);
      }
    }
    if (patch.provider !== undefined) {
      if (!this._available.includes(patch.provider)) return { ok: false, code: 'bad_provider' };
      config.provider = patch.provider;
    }
    if (patch.models && typeof patch.models === 'object') {
      for (const provider of PROVIDERS) {
        if (patch.models[provider] === undefined) continue;
        const model = clean(patch.models[provider], 120);
        if (!allowedModel(provider, model)) return { ok: false, code: 'bad_model' };
        config.models[provider] = model;
      }
    }
    if (patch.thinking !== undefined) {
      if (!THINKING.includes(patch.thinking)) return { ok: false, code: 'bad_thinking' };
      config.thinking = patch.thinking;
    }
    if (patch.mode !== undefined) {
      if (!MODES.includes(patch.mode)) return { ok: false, code: 'bad_mode' };
      config.mode = patch.mode;
    }
    if (patch.voice !== undefined) {
      if (!VOICES.includes(patch.voice)) return { ok: false, code: 'bad_voice' };
      config.voice = patch.voice;
    }
    if (patch.screen !== undefined) {
      if (!AIMS.includes(patch.screen)) return { ok: false, code: 'bad_screen' };
      config.screen = patch.screen;
    }
    if (patch.captions !== undefined) config.captions = Boolean(patch.captions);
    if (patch.stayQuiet !== undefined) {
      if (typeof patch.stayQuiet !== 'boolean') return { ok: false, code: 'bad_stay_quiet' };
      config.stayQuiet = patch.stayQuiet;
    }
    if (patch.characterId !== undefined) {
      const id = String(patch.characterId || '');
      config.characterId = CHARACTER_ID.test(id) ? id : null;
    }
    try {
      keyStore.writeObject(this.configPath, { version: 1, updatedAt: Date.now(), ...config });
    } catch (err) {
      console.error('[VoiceBeta] settings likhi nahi ja sakin:', err.message);
      return { ok: false, code: 'save_failed' };
    }
    return this.getConfig();
  }

  _characterPayload(character) {
    return {
      id: CHARACTER_ID.test(String(character?.id || '')) ? String(character.id).toLowerCase() : 'custom',
      name: clean(character?.name, 60),
      gender: character?.gender === 'female' ? 'female' : 'male',
      prompt: clean(character?.prompt, MAX_PROMPT_CHARS),
    };
  }

  /** Renderer ka bheja hua agent wala hissa, saaf kiya hua. Kuch na ho to null. */
  _agentPayload(agent) {
    if (!agent || typeof agent !== 'object') return null;
    const tools = (Array.isArray(agent.tools) ? agent.tools : [])
      .filter((tool) => tool && typeof tool === 'object' && TOOL_NAME.test(String(tool.name || '')) && clean(tool.description, 2000))
      .slice(0, MAX_AGENT_TOOLS)
      .map((tool) => ({
        name: String(tool.name),
        description: clean(tool.description, 2000),
        parameters: tool.parameters && typeof tool.parameters === 'object' ? tool.parameters : {},
      }));
    const history = (Array.isArray(agent.history) ? agent.history : [])
      .filter((turn) => turn && (turn.role === 'user' || turn.role === 'assistant') && clean(turn.text, MAX_HISTORY_TURN_CHARS))
      .slice(-MAX_HISTORY_TURNS)
      .map((turn) => ({ role: turn.role, text: clean(turn.text, MAX_HISTORY_TURN_CHARS) }));
    const awareness = clean(agent.awareness, MAX_AWARENESS_CHARS);
    // Nayi yaadash: SDK ki auto memory ka folder — sirf jab FileMemoryService
    // ka asal folder ho. Na mile to field hi nahi (engine yaadash band rakhta hai).
    const memoryDir = this._memoryDir(agent.memory_dir);
    return {
      tools,
      memory: clean(agent.memory, MAX_AGENT_MEMORY_CHARS),
      ...(memoryDir ? { memory_dir: memoryDir } : null),
      user_name: clean(agent.user_name, 60),
      // Awareness band ho to field hi nahi jati (prompt pehle jaisa).
      ...(awareness ? { awareness } : null),
      // Baat-cheet Awareness ne kholi: salaam nahi (engine config.py `greet`).
      ...(agent.greet === false ? { greet: false } : null),
      // Nayi chat par field hi nahi jati (config pehle jaisa).
      ...(history.length > 0 ? { history } : null),
      ...(SESSION_ID.test(String(agent.session || '').toLowerCase()) ? { session: String(agent.session).toLowerCase() } : null),
    };
  }

  /**
   * Engine ke liye poora config. Sirf wohi cheezein jo is session ko chahiyen:
   * Custom API par user ki chabiyan, Stonic Cloud par gateway ka pata + token
   * (`cloud` — _openCloudSession ka jawab) aur koi provider key nahi.
   */
  _enginePayload(config, character, cloud = null, agent = null) {
    const extra = this._agentPayload(agent);
    const skills = this._skillsPayload();
    if (cloud) {
      // Cloud par haath sirf tab jab gateway dimagh Claude de (wohi kaam karta
      // hai). Nishana Claude ka apna: UI-TARS user ki OpenRouter key maangta hai.
      const cloudAim = cloud.brain_provider === 'anthropic' ? this._aim() : 'off';
      return {
        mode: 'cloud',
        cloud,
        brain: { thinking: config.thinking },
        character: this._characterPayload(character),
        screen: { aim: cloudAim },
        stay_quiet: config.stayQuiet,
        ...(extra ? { agent: extra } : null),
        ...(skills ? { skills } : null),
      };
    }
    const provider = this._provider(config);
    const gemini = this._geminiKey(config).key;
    // Screen ke haath sirf us dimagh ko jo kaam karta hai (Claude, OpenRouter par bhi).
    const aim = this._acts(config, provider) ? this._aim() : 'off';
    return {
      mode: 'custom',
      voice: config.voice,
      keys: {
        gemini,
        // 14 Oct 2026: Settings mein sirf EK Gemini key — purani save hui backup key ab nahi chalti.
        gemini_backup: '',
        anthropic: provider === 'anthropic' ? config.keys.anthropic : '',
        groq: provider === 'groq' ? config.keys.groq : '',
        openrouter: provider === 'openrouter' ? this._openRouterKey(config).key : '',
      },
      brain: { provider, model: config.models[provider], thinking: config.thinking },
      character: this._characterPayload(character),
      screen: { aim },
      // User ki "Stay quiet" setting — engine (config.py `uses_floor`) sirf `true` par chalu karta hai.
      stay_quiet: config.stayQuiet,
      ...(extra ? { agent: extra } : null),
      ...(skills ? { skills } : null),
    };
  }

  /**
   * User ki skills (Skills card) — dono voices ka Claude dimagh (New ki baat-cheet
   * aur Classic ka kaam) inhein Claude Code ke plugin raaste se parhta hai. Raasta
   * yahin main process mein banta hai (services/skills/library.js), renderer se
   * kabhi nahi. Kuch na ho / ghalti ho to field hi nahi (engine pehle jaisa).
   */
  _skillsPayload() {
    if (!this.skills || typeof this.skills.forEngine !== 'function') return null;
    try {
      return this.skills.forEngine() || null;
    } catch (err) {
      console.warn('[VoiceBeta] skills tayyar na ho sakin:', err?.message || err);
      return null;
    }
  }

  // ── Stonic Cloud ──────────────────────────────────────────────────────────

  /**
   * Gateway se poochho: is user ka plan chalta hai, aur dimagh ka kaun sa
   * model? Baat shuru hone se PEHLE — taake "plan nahi" ya "hadd bhar gayi"
   * ka saaf paigham dikhe, beech guftagu mein khamoshi nahi.
   *
   * @returns {Promise<{ok: true, cloud: object} | {ok: false, code: string, message?: string}>}
   */
  async _openCloudSession(config) {
    let access;
    try {
      access = await this._getCloudAccess();
    } catch (err) {
      return { ok: false, code: 'cloud_unavailable', message: err?.message || String(err) };
    }
    if (!access?.ok || !access.gateway || !access.token) {
      return { ok: false, code: access?.code || 'cloud_signin' };
    }

    const url = String(access.gateway).replace(/^ws(s?):/, 'http$1:').replace(/\/+$/, '');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), GATEWAY_TIMEOUT_MS);
    let response;
    let body = {};
    try {
      response = await this._fetch(`${url}/v1/voice-beta/session`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: `Bearer ${access.token}` },
        // Body lazmi: Google ka edge bina body ke POST par 411 deta hai.
        body: '{}',
        signal: controller.signal,
      });
      body = await response.json().catch(() => ({}));
    } catch (err) {
      return { ok: false, code: 'cloud_unavailable', message: err?.message || String(err) };
    } finally {
      clearTimeout(timer);
    }

    if (response.status !== 200) {
      const error = body?.error && typeof body.error === 'object' ? body.error : {};
      // 404 = gateway par ye raasta abhi laga hi nahi (purana revision).
      const code =
        response.status === 401 || response.status === 403 ? 'cloud_signin' :
        response.status === 402 ? 'cloud_no_plan' :
        response.status === 429 ? 'cloud_limit' :
        response.status === 404 ? 'cloud_not_live' :
        'cloud_unavailable';
      return { ok: false, code, message: code === 'cloud_limit' && error.resets_at ? `resets_at=${error.resets_at}` : '' };
    }

    const model = clean(body?.brain?.model, 120);
    if (!MODEL_ID.test(model)) return { ok: false, code: 'cloud_unavailable', message: 'gateway ne model nahi bataya' };
    return {
      ok: true,
      cloud: {
        url,
        token: access.token,
        brain_model: model,
        // 'anthropic' = gateway Claude deta hai (dimagh kaam bhi karta hai);
        // purana gateway ye batata hi nahi = 'openai' (sirf baat, jaisa pehle).
        brain_provider: body?.brain?.provider === 'anthropic' ? 'anthropic' : 'openai',
        reasoning: clean(body?.brain?.reasoning?.[config.thinking], 20),
        purpose: clean(body?.brain?.purpose, 40) || 'voice_beta_brain',
      },
    };
  }

  // ── Engine ────────────────────────────────────────────────────────────────

  getStatus() {
    return { ok: true, state: this.state, error: this.error, running: Boolean(this.child) };
  }

  /**
   * Installer ki .pyc par engine chal raha hai: purane builds ka banaya cache
   * (user ke data folder mein, ~50 MB) ab kabhi nahi parha jata. Sirf is install
   * ka hissa mitao — dev ya kisi aur install ka nahi — ek dafa, thori der baad,
   * peeche. Na mit sake to bhi kuch nahi tootta (agli dafa phir koshish).
   */
  _scheduleLegacyCacheSweep(runtime) {
    if (this._legacySweep || !runtime || !runtime.root) return;
    const dirs = legacyBytecodeDir(this.userDataDir, path.dirname(runtime.root));
    if (!dirs) return;
    this._legacySweep = setTimeout(() => {
      this._legacySweep = this._sweepLegacyCache(dirs).catch((err) => {
        console.warn('[VoiceBeta] purana .pyc cache saaf na ho saka:', err?.message || err);
      });
    }, LEGACY_CACHE_SWEEP_DELAY_MS);
    if (typeof this._legacySweep.unref === 'function') this._legacySweep.unref();
  }

  async _sweepLegacyCache({ prefix, target }) {
    if (!fs.existsSync(target)) return false;
    await fs.promises.rm(target, { recursive: true, force: true, maxRetries: 2 });
    // Upar ke khali folder bhi (prefix samet) — jo kisi aur ke kaam ke hon wo khali nahi, is liye bach jate hain.
    let dir = path.dirname(target);
    while (dir.length >= prefix.length && path.relative(prefix, dir).split(path.sep)[0] !== '..') {
      try {
        if (fs.readdirSync(dir).length > 0) break;
        fs.rmdirSync(dir);
      } catch {
        break;
      }
      if (dir === prefix) break;
      dir = path.dirname(dir);
    }
    console.log('[VoiceBeta] purana .pyc cache saaf (engine ab installer ki .pyc par):', target);
    return true;
  }

  _setState(state, error = null) {
    this.state = state;
    this.error = error;
    this._broadcast({ state, error });
  }

  _broadcast(payload) {
    try {
      const win = this.mainWindow;
      if (win && !win.isDestroyed()) win.webContents.send('voice-beta:state', payload);
    } catch { /* window ja chuki */ }
  }

  /** Engine ko uthao (agar utha na ho) aur us ke tayyar hone tak ruko. */
  _ensureEngine() {
    if (this.child && this.state === 'ready') return Promise.resolve();
    if (this._starting) return this._starting;
    this._starting = this._spawn().finally(() => { this._starting = null; });
    return this._starting;
  }

  _spawn() {
    return new Promise((resolve, reject) => {
      const runtime = this._describeRuntime();
      if (!runtime.ok) {
        console.warn('[VoiceBeta] runtime nahi mila:', runtime.missing.join(' | '));
        const error = { code: 'runtime_missing', message: runtime.missing.join(' ') };
        this._setState('error', error);
        reject(error);
        return;
      }

      try { fs.mkdirSync(path.dirname(this.logPath), { recursive: true }); } catch { /* log ke baghair bhi chalta hai */ }
      const token = crypto.randomBytes(32).toString('hex');
      const launch = engineLaunch({
        runtime,
        userDataDir: this.userDataDir,
        baseEnv: process.env,
        token,
        parentPid: process.pid,
        logPath: this.logPath,
      });
      const { env } = launch;

      this._stderrTail = [];
      this.token = token;
      this.port = null;
      this._setState('starting');
      console.log(`[VoiceBeta] engine uth raha hai (${launch.shipped ? 'installer ki .pyc' : '.pyc data folder mein'}):`, runtime.python);

      let settled = false;
      let timer = null;
      let child = null;

      const fail = (code, message) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        const error = { code, message };
        if (this.child === child) {
          this.child = null;
          if (child && child.pid) killProcessTree(child.pid);
          this._setState('error', error);
        }
        reject(error);
      };

      try {
        child = spawn(runtime.python, launch.args, {
          // Install folder ke bahar: engine us folder ko "pakar" kar na baithe
          // jise update ke waqt naam badalna hota hai.
          cwd: this.userDataDir,
          env,
          windowsHide: true,
          // stdin 'ignore': engine stdin nahi parhta (Windows par pipe wali
          // stdin + DLL load ka purana jam yahan ho hi nahi sakta).
          stdio: ['ignore', 'pipe', 'pipe'],
        });
      } catch (err) {
        this.child = null;
        fail('engine_failed', `Engine chal na saka: ${err.message}`);
        return;
      }
      this.child = child;

      timer = setTimeout(() => {
        fail('engine_timeout', `Engine ${Math.round(START_TIMEOUT_MS / 1000)} s mein tayyar nahi hua. ${this._tail()}`);
      }, START_TIMEOUT_MS);

      readline.createInterface({ input: child.stdout }).on('line', (line) => {
        let message;
        try { message = JSON.parse(line); } catch { return; }
        if (message.event === 'ready' && Number.isInteger(message.port)) {
          if (settled || this.child !== child) return;
          settled = true;
          clearTimeout(timer);
          this.port = message.port;
          console.log(`[VoiceBeta] engine tayyar — port ${message.port}, pipecat ${message.pipecat}`);
          this._setState('ready');
          if (launch.shipped) this._scheduleLegacyCacheSweep(runtime);
          resolve();
        } else if (message.event === 'fatal') {
          fail('engine_failed', String(message.message || message.code || 'engine fatal'));
        } else if (message.event === 'screen') {
          if (this.child === child) this._onScreen(message);
        } else if (message.event === 'work') {
          if (this.child === child) this._onWork(message);
        }
      });
      readline.createInterface({ input: child.stderr }).on('line', (line) => {
        if (!line.trim()) return;
        this._stderrTail.push(line);
        if (this._stderrTail.length > STDERR_TAIL_LINES) this._stderrTail.shift();
      });

      child.on('error', (err) => fail('engine_failed', `Engine chal na saka: ${err.message}`));
      child.on('exit', (code, signal) => {
        const wasCurrent = this.child === child;
        if (wasCurrent) {
          this.child = null;
          this.port = null;
          this.token = null;
          this._endScreen('stopped');
          this._failOpenWork('The engine stopped before the work finished.');
        }
        if (!settled) {
          fail('engine_failed', `Engine uthte hi band ho gaya (code ${code ?? signal}). ${this._tail()}`);
          return;
        }
        if (!wasCurrent || this._shuttingDown) return;
        // Hum ne khud band kiya ho to state pehle hi 'stopped' hai.
        if (this.state === 'ready') {
          console.warn(`[VoiceBeta] engine achanak band (code ${code ?? signal}). ${this._tail()}`);
          this._setState('error', { code: 'engine_exited', message: `Engine band ho gaya (code ${code ?? signal}). ${this._tail()}`.trim() });
        }
      });
    });
  }

  // ── Screen ka kaam (engine ke haath: mouse / keyboard) ────────────────────

  /**
   * Engine ki khabar: screen ka kaam shuru hua / ek qadam / khatam.
   * `task` user ka apna jumla hai — sirf us ki apni screen ke HUD par jata hai, log mein nahi.
   */
  _onScreen(message) {
    const phase = String(message.phase || '');
    if (phase === 'start') {
      this._endScreen('stopped');
      this._screenRuns += 1;
      const maxSteps = Number.isInteger(message.max_steps) && message.max_steps > 0 ? message.max_steps : undefined;
      this._screen = { id: `beta-${this._screenRuns}`, maxSteps };
      // Stonic ki window apni jagah rehti hai: dimagh khud taskbar / Alt+Tab se
      // doosri app par jata hai (config.py ki screen hidayat).
      this._overlayEvent({
        event: 'task_queued', id: this._screen.id, source: 'voice', max_steps: maxSteps,
        task: clean(message.task, 200) || 'Working on your screen',
      });
      this._overlayEvent({ event: 'task_started', id: this._screen.id, max_steps: maxSteps });
    } else if (phase === 'step' && this._screen) {
      this._overlayEvent({
        event: 'step', id: this._screen.id, step: Number(message.step) || 0, max_steps: this._screen.maxSteps,
        thought: clean(message.label, 120), action: '',
      });
    } else if (phase === 'end') {
      this._endScreen(['done', 'stopped', 'interrupted'].includes(message.status) ? message.status : 'done', Number(message.steps) || undefined);
    }
  }

  /**
   * Asal computer agent (chat / phone / asal voice ka diya hua kaam) ki khabar.
   * Mouse ek hai: jab wo chale to Beta ke haath screen nahi chherte, aur Beta
   * beech mein ho to ruk jata hai (us ka dimagh agle qadam par wajah sunta hai).
   * main.js is manager ko wohi events deta hai jo us agent ka overlay leta hai —
   * computer-manager.js ko haath nahi lagta.
   */
  onComputerEvent(ev) {
    const busy =
      ev?.event === 'task_queued' || ev?.event === 'task_starting' || ev?.event === 'task_started' || ev?.event === 'step' || ev?.event === 'stopping' ? true :
      ev?.event === 'task_done' || ev?.event === 'process_exit' ? false :
      null;
    if (busy === null || busy === this._computerBusy) return;
    this._computerBusy = busy;
    void this._pushMouse();
    if (busy && this._screen) void this.interruptWork();
  }

  async _pushMouse() {
    if (!this.child || this.state !== 'ready') return;
    try { await this._request('POST', '/beta/mouse', { busy: this._computerBusy }); } catch { /* engine ja chuka: agli baat-cheet par dobara jata hai */ }
  }

  _endScreen(status, steps) {
    const screen = this._screen;
    if (!screen) return;
    this._screen = null;
    this._overlayEvent({ event: 'task_done', id: screen.id, status, steps, summary: '' });
  }

  _overlayEvent(event) {
    try { this.overlay?.event(event); } catch (err) { console.warn('[VoiceBeta] overlay:', err?.message || err); }
  }

  /** Chalta kaam roko (HUD ka Esc). Baat-cheet chalti rehti hai. */
  async interruptWork() {
    if (!this.child || this.state !== 'ready') return { ok: true, stopped: false };
    try {
      const result = await this._request('POST', '/beta/interrupt');
      return { ok: true, stopped: Boolean(result.data?.stopped) };
    } catch {
      return { ok: true, stopped: false };
    }
  }

  // ── Classic voice ka kaam (engine work.py — wohi Claude Agent SDK dimagh) ──

  /**
   * Classic ka kaam kis raaste par chalega, ya kyun nahi chal sakta. Kaam Claude
   * Code harness karta hai: Stonic Cloud, apni Anthropic key, OpenRouter par Claude
   * ka model, ya muft Gemini key (engine ka pul — gemini_relay.py).
   */
  _workRoute(config) {
    const mode = this._mode(config);
    if (mode === 'cloud') return { ok: true, mode };
    const provider = this._provider(config);
    if (!this._acts(config, provider)) return { ok: false, code: 'brain_not_claude' };
    if (!this._brainKey(config, provider)) return { ok: false, code: `${provider}_key_missing` };
    return { ok: true, mode, provider };
  }

  /**
   * Kaam ke liye Cloud gateway ka jawab (plan chalta hai? dimagh ka model?) — taaza
   * ho to yaad se (WORK_CLOUD_TTL_MS). Sirf kamyab jawab yaad rehta hai: "plan nahi"
   * / "hadd bhar gayi" har dafa gateway se taaza.
   */
  async _workCloudSession(config) {
    const cached = this._workCloud;
    if (cached && Date.now() - cached.at < WORK_CLOUD_TTL_MS) return { ok: true, cloud: cached.cloud };
    const opened = await this._openCloudSession(config);
    this._workCloud = opened.ok ? { at: Date.now(), cloud: opened.cloud } : null;
    return opened;
  }

  /**
   * Classic (Gemini Live) ki baat-cheet judi: engine abhi se garam — pehla PC ka kaam
   * ~1 s mein shuru (pehle thanda engine ~10 s + gateway ~3 s leta tha, aur user is
   * khamoshi mein "karo" keh kar wohi kaam dobara bhejwa deta tha). Kaam ka raasta hi
   * na ho (Claude ki chabi / Cloud nahi) to kuch nahi uthta. Kabhi throw nahi karta.
   */
  async workWarm() {
    this._classicLive = true;
    const config = this._read();
    const route = this._workRoute(config);
    if (!route.ok) return { ok: true, warmed: false, code: route.code };
    this._cancelIdleStop();
    try {
      await this._ensureEngine();
      if (route.mode === 'cloud') {
        const opened = await this._workCloudSession(config);
        if (!opened.ok) return { ok: true, warmed: true, cloud: false, code: opened.code };
      }
      return { ok: true, warmed: true };
    } catch (err) {
      return { ok: false, code: err?.code || 'engine_failed' };
    }
  }

  /**
   * Classic ki baat-cheet band: thori der (WORK_KEEP_WARM_MS) baad engine bhi band —
   * magar New voice chal rahi ho ya koi kaam chal raha ho to nahi.
   */
  workIdle() {
    this._classicLive = false;
    if (this.child && !this._session) this._scheduleIdleStop(WORK_KEEP_WARM_MS);
    return { ok: true };
  }

  /**
   * Gemini Live (Classic) ka `agent_task`: kaam engine ke dimagh ko. Foran lautta
   * hai ("shuru hua" / "chalte kaam mein gaya" / "dohraya — kuch naya nahi"); qadam
   * aur report `voice-beta:work` par aate hain. Kabhi throw nahi karta.
   *
   * @returns {Promise<{ok: true, task: string, joined: boolean, duplicate?: boolean} | {ok: false, code: string, message?: string}>}
   */
  async workTask({ desk, task, text, userName, intent } = {}) {
    const deskId = String(desk || '');
    const body = clean(text, MAX_WORK_TEXT_CHARS);
    if (!WORK_ID.test(deskId) || !body) return { ok: false, code: 'bad_request' };
    const taskId = WORK_ID.test(String(task || '')) ? String(task) : `task-${crypto.randomBytes(5).toString('hex')}`;
    const config = this._read();
    const route = this._workRoute(config);
    if (!route.ok) return route;
    this._cancelIdleStop();
    try {
      await this._ensureEngine();
      let cloud = null;
      if (route.mode === 'cloud') {
        const opened = await this._workCloudSession(config);
        if (!opened.ok) return { ok: false, code: opened.code, message: opened.message || '' };
        // Purana gateway jo dimagh Claude nahi deta: wahan kaam nahi hota (sirf baat).
        if (opened.cloud.brain_provider !== 'anthropic') return { ok: false, code: 'cloud_no_agent' };
        cloud = opened.cloud;
      }
      // Wohi config jo New ko jata hai — magar kirdar / yaadash ke baghair, aur
      // Gemini ki chabi bhi nahi (kaam ko us ki zaroorat nahi) — siwaye jab dimagh
      // khud Gemini ho: tab wohi chabi kaam karti hai.
      const payload = this._enginePayload(config, null, cloud, null);
      if (payload.keys) {
        const brainIsGemini = payload.brain?.provider === 'gemini';
        payload.keys = { ...payload.keys, gemini: brainIsGemini ? payload.keys.gemini : '', gemini_backup: '' };
      }
      payload.agent = { user_name: clean(userName, 60) };
      // Gemini ka kaun sa tool: naya kaam / chalte kaam mein tabdeeli / roka hua kaam wapas.
      const kind = WORK_INTENTS.has(intent) ? intent : 'task';
      const res = await this._request('POST', '/work/task', { desk: deskId, task: taskId, text: body, config: payload, intent: kind });
      if (res.status !== 200 || !res.data?.ok) {
        return { ok: false, code: res.data?.code || 'work_rejected', message: res.data?.message || '' };
      }
      const started = String(res.data.task || taskId);
      this._workTasks.set(started, deskId);
      // Naya (ya garam) engine: use pata ho ke mouse abhi kis ke paas hai.
      await this._pushMouse();
      return { ok: true, task: started, joined: Boolean(res.data.joined), ...(res.data.duplicate ? { duplicate: true } : null) };
    } catch (err) {
      return { ok: false, code: err?.code || 'engine_failed', message: err?.message || String(err) };
    }
  }

  /** Classic ka chalta kaam roko (dimagh zinda rehta hai). */
  async workStop({ desk } = {}) {
    const deskId = String(desk || '');
    if (!WORK_ID.test(deskId)) return { ok: false, code: 'bad_request' };
    if (!this.child || this.state !== 'ready') return { ok: true, stopped: false };
    try {
      const res = await this._request('POST', '/work/stop', { desk: deskId });
      return { ok: true, stopped: Boolean(res.data?.stopped) };
    } catch {
      return { ok: true, stopped: false };
    }
  }

  /** Classic ki baat-cheet khatam aur koi kaam nahi chal raha: dimagh (Claude Code ka process) band. */
  async workClose({ desk } = {}) {
    const deskId = String(desk || '');
    if (!WORK_ID.test(deskId)) return { ok: false, code: 'bad_request' };
    if (!this.child || this.state !== 'ready') return { ok: true, closed: false };
    try {
      const res = await this._request('POST', '/work/close', { desk: deskId });
      return { ok: true, closed: Boolean(res.data?.closed) };
    } catch {
      return { ok: true, closed: false };
    }
  }

  /** Classic ka kaam kahan tak pohncha (Gemini ka `agent_task_status`). */
  async workStatus({ desk } = {}) {
    const deskId = String(desk || '');
    if (!WORK_ID.test(deskId)) return { ok: false, code: 'bad_request' };
    if (!this.child || this.state !== 'ready') return { ok: true, open: false, busy: false, task: '', steps: 0, jobs: [] };
    try {
      const res = await this._request('POST', '/work/status', { desk: deskId });
      const data = res.data || {};
      return {
        ok: true,
        open: Boolean(data.open),
        busy: Boolean(data.busy),
        task: String(data.task || ''),
        steps: Number(data.steps) || 0,
        jobs: Array.isArray(data.jobs) ? data.jobs.map((job) => clean(job, 160)).filter(Boolean) : [],
        ...(data.starting ? { starting: true } : null),
        ...(data.stopping ? { stopping: true } : null),
      };
    } catch {
      return { ok: true, open: false, busy: false, task: '', steps: 0, jobs: [] };
    }
  }

  /** Engine ki "work" khabar (stdout) — saaf kar ke renderer ko. */
  _onWork(message) {
    const kind = String(message.kind || '');
    if (!WORK_EVENT_KINDS.has(kind)) return;
    const desk = String(message.desk || '');
    const task = String(message.task || '');
    if (!WORK_ID.test(desk) || !WORK_ID.test(task)) return;
    const event = { desk, task, kind };
    if (kind === 'started') {
      this._workTasks.set(task, desk);
      event.origin = WORK_ORIGINS.has(message.origin) ? message.origin : 'task';
      // "followup": kaam ke aakhir mein joda hukm — Gemini ko batana hai ke kis baat ka jawab hai.
      if (event.origin === 'followup') event.text = clean(message.text, 300);
    } else if (kind === 'step') {
      event.step = Number(message.step) || 0;
      event.label = clean(message.label, 160);
    } else if (kind === 'step_failed') {
      event.step = Number(message.step) || 0;
      event.output = clean(message.output, 400);
    } else if (kind === 'job') {
      event.status = clean(message.status, 20);
      event.label = clean(message.label, 160);
      event.running = Number(message.running) || 0;
    } else if (kind === 'done') {
      this._workTasks.delete(task);
      event.origin = WORK_ORIGINS.has(message.origin) ? message.origin : 'task';
      // Cloud ne chabi / plan par roka: yaad wala gateway jawab purana — agla kaam taaza poochhe.
      if (message.status === 'failed' && /auth|billing|401|402|403|sign.?in|plan/i.test(`${message.error_kind || ''} ${message.error || ''}`)) {
        this._workCloud = null;
      }
      event.status = ['completed', 'failed', 'stopped'].includes(message.status) ? message.status : 'failed';
      event.report = clean(message.report, 6000);
      event.steps = Number(message.steps) || 0;
      event.jobs = Array.isArray(message.jobs) ? message.jobs.map((job) => clean(job, 160)).filter(Boolean) : [];
      if (message.error) event.error = clean(message.error, 600);
      if (message.error_kind) event.errorKind = clean(message.error_kind, 40);
      // Apni keys par kaam kis dimagh par tha — renderer ka card (ApiIssueModal) sahi
      // provider ka naam aur hal dikhaye. Cloud par nahi (wahan andar ka naam raaz hai).
      if (message.error) {
        try {
          const config = this._read();
          if (this._mode(config) === 'custom') event.provider = this._provider(config);
        } catch { /* config na parhi ja saki: card provider ke baghair */ }
      }
    }
    this._sendWork(event);
  }

  _sendWork(event) {
    try {
      const win = this.mainWindow;
      if (win && !win.isDestroyed()) win.webContents.send('voice-beta:work', event);
    } catch { /* window ja chuki */ }
  }

  /** Engine band ho gaya: chalte kaam renderer mein "atke" na rahein. */
  _failOpenWork(reason) {
    for (const [task, desk] of this._workTasks) {
      this._sendWork({ desk, task, kind: 'done', origin: 'task', status: 'failed', report: '', steps: 0, jobs: [], error: reason });
    }
    this._workTasks.clear();
  }

  /**
   * Engine ki stderr ki aakhri lines — app ke Logs panel tak jati hain. Asal
   * ghalti (Python ka "ImportError: DLL load failed …") hamesha AAKHIR mein
   * hoti hai, is liye kaatna ho to shuru se katta hai, aakhir se nahi.
   */
  _tail() {
    const lines = this._stderrTail.slice(-STDERR_REPORT_LINES).join('\n');
    return lines ? `Aakhri lines:\n${lines.slice(-STDERR_REPORT_CHARS)}` : '';
  }

  async _request(method, route, body) {
    if (!this.child || !this.port || !this.token) throw { code: 'engine_stopped', message: 'Engine chal nahi raha.' };
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(`http://127.0.0.1:${this.port}${route}`, {
        method,
        headers: { 'content-type': 'application/json', 'x-stonic-voice-token': this.token },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      const data = await response.json().catch(() => ({}));
      return { status: response.status, data };
    } catch (err) {
      throw { code: 'engine_unreachable', message: err?.message || String(err) };
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Beta khula: engine uthao, settings do. Jawab mein wo pata jis par renderer
   * WebRTC ka offer bhejta hai.
   */
  async start({ character, agent, startedAt } = {}) {
    this._cancelIdleStop();
    const config = this._read();
    const missing = this._missing(config);
    try {
      const waitFrom = Date.now();
      await this._ensureEngine();
      const engineWaitMs = Date.now() - waitFrom;
      // Kirdar ke baghair (sirf engine garam karna ho) config nahi bheja jata —
      // aur gateway se bhi nahi poochha jata.
      const starting = Boolean(clean(character?.prompt, MAX_PROMPT_CHARS));
      let cloud = null;
      let gatewayMs = null;
      if (starting && this._mode(config) === 'cloud') {
        const gatewayFrom = Date.now();
        const opened = await this._openCloudSession(config);
        gatewayMs = Date.now() - gatewayFrom;
        if (!opened.ok) return { ok: false, code: opened.code, message: opened.message || '', missing };
        cloud = opened.cloud;
      }
      const payload = this._enginePayload(config, character, cloud, agent);
      // "Start" ka hisaab engine ke log mein (voice-beta.log) — kis qadam ne kitna waqt liya.
      if (starting) payload.timing = startTiming({ startedAt, engineWaitMs, gatewayMs });
      // Engine ka faisla: dimagh kaam bhi karega (Claude) ya sirf baat. App ki
      // chat isi se jaanti hai ke ek "baari" kab poori hui.
      let agentOn = false;
      if (starting) {
        const pushed = await this._request('POST', '/beta/config', payload);
        if (pushed.status !== 200) {
          return { ok: false, code: pushed.data?.code || 'config_rejected', message: pushed.data?.message || '' };
        }
        agentOn = pushed.data?.config?.agent?.on === true;
        // Web search (aur aage koi bhi tool jo key maange) isi raaste par chale
        // jis par ye baat-cheet shuru hui — beech mein Settings badlein to bhi.
        this._session = { mode: cloud ? 'cloud' : 'custom', provider: cloud ? null : this._provider(config) };
        // Naya (ya garam) engine: use pata ho ke mouse abhi kis ke paas hai.
        await this._pushMouse();
      }
      return { ok: true, url: `http://127.0.0.1:${this.port}`, token: this.token, missing, agent: agentOn };
    } catch (err) {
      return { ok: false, code: err?.code || 'engine_failed', message: err?.message || String(err), missing };
    }
  }

  /** Provider se models ki list — yehi chabi ki jaanch bhi hai. `key` na ho to mehfooz wali. */
  async checkProvider({ provider, key } = {}) {
    if (!PROVIDERS.includes(provider)) return { ok: false, code: 'bad_provider' };
    const config = this._read();
    const draft = cleanKey(key);
    const useKey = draft || this._brainKey(config, provider);
    if (!useKey) return { ok: false, code: `${provider}_key_missing` };
    this._cancelIdleStop();
    try {
      await this._ensureEngine();
      const result = await this._request('POST', '/beta/models', { provider, key: useKey });
      return result.data && typeof result.data === 'object' ? result.data : { ok: false, code: 'engine_failed' };
    } catch (err) {
      return { ok: false, code: err?.code || 'engine_failed', message: err?.message || String(err) };
    }
  }

  /**
   * Dimagh ka `web_search` tool (renderer lib/beta/agent.ts se). Raasta wohi
   * jis par baat-cheet chal rahi hai: Stonic Cloud = gateway; Custom API =
   * user ki Gemini key, phir Claude ki apni search (dimagh wali key par).
   * Tafseel web-search.js mein. Kabhi throw nahi karta.
   *
   * @returns {Promise<{ok: boolean, text: string, via?: string}>}  `text` seedha dimagh ko jata hai.
   */
  async webSearch({ query } = {}) {
    const q = webSearch.cleanQuery(query);
    if (!q) return { ok: false, text: 'query parameter is required and must be a non-empty string.' };
    const config = this._read();
    const mode = this._session?.mode || this._mode(config);
    let result;
    try {
      if (mode === 'cloud') {
        const access = await this._getCloudAccess().catch(() => null);
        if (!access?.ok || !access.gateway || !access.token) {
          result = { ok: false, code: access?.code === 'cloud_unavailable' ? 'search_network' : 'cloud_signin' };
        } else {
          const url = String(access.gateway).replace(/^ws(s?):/, 'http$1:').replace(/\/+$/, '');
          result = await webSearch.searchCloud({ url, token: access.token, query: q, fetchImpl: this._fetch });
        }
      } else {
        const provider = this._session?.provider || this._provider(config);
        result = await webSearch.searchWithOwnKeys({
          query: q,
          fetchImpl: this._fetch,
          keys: {
            gemini: this._geminiKey(config).key,
            // Claude ki search sirf us key par jis se dimagh chalta hai (user ka apna bill).
            anthropic: provider === 'anthropic' ? config.keys.anthropic : '',
            openrouter: provider === 'openrouter' ? this._openRouterKey(config).key : '',
          },
          log: (line) => console.warn(`[VoiceBeta] web search: ${line}`),
        });
      }
    } catch (err) {
      result = { ok: false, code: 'search_network', reason: err?.message || String(err) };
    }
    if (result.ok) {
      console.log(`[VoiceBeta] web search (${mode}) → ${result.via}`);
      return { ok: true, via: result.via, text: webSearch.formatResult(result) || 'No results found.' };
    }
    console.warn(`[VoiceBeta] web search (${mode}) nakaam: ${result.code}${result.reason ? ` — ${result.reason}` : ''}`);
    return { ok: false, code: result.code, text: webSearch.failureText(result) };
  }

  /** Chalti baat-cheet band (engine chalta rehta hai). */
  async hangup() {
    this._endScreen('stopped');
    if (!this.child || this.state !== 'ready') return { ok: true };
    try { await this._request('POST', '/beta/hangup'); } catch { /* engine ja chuka */ }
    return { ok: true };
  }

  /**
   * User Beta se nikla: baat-cheet band, chabiyan engine ki yaad-dasht se
   * saaf, aur thori der baad engine bhi band.
   */
  async leave() {
    this._endScreen('stopped');
    this._session = null;
    if (!this.child) return { ok: true };
    if (this.state === 'ready') {
      try { await this._request('POST', '/beta/forget'); } catch { /* engine ja chuka */ }
    }
    this._scheduleIdleStop();
    return { ok: true };
  }

  /**
   * Thori der baad engine band — magar Classic ka koi kaam chal raha ho to us ke
   * khatam hone tak nahi, aur Classic ki baat-cheet chal rahi ho to bilkul nahi
   * (us ka desk — pichle kaam ki yaad — aur garam engine usi ke liye hain).
   */
  _scheduleIdleStop(ms = KEEP_WARM_MS) {
    this._cancelIdleStop();
    this._idleTimer = setTimeout(() => {
      this._idleTimer = null;
      if (this._classicLive) return;
      if (this._workTasks.size > 0) this._scheduleIdleStop(ms);
      else this.stop();
    }, ms);
    if (typeof this._idleTimer.unref === 'function') this._idleTimer.unref();
  }

  _cancelIdleStop() {
    if (this._idleTimer) clearTimeout(this._idleTimer);
    this._idleTimer = null;
  }

  /**
   * User ne chat mitayi: us ki Beta notebook (SDK session ki file, aur us ke
   * sub-agents ka folder) bhi PC se mit jaye. Engine chal raha ho ya nahi —
   * sirf files. Ghalat / anjaan id par kuch nahi hota.
   */
  forgetSession({ id } = {}) {
    const sessionId = String(id || '').toLowerCase();
    if (!SESSION_ID.test(sessionId)) return { ok: false, code: 'bad_session' };
    const projects = path.join(this.userDataDir, 'voice-beta-agent', 'projects');
    let removed = 0;
    let folders = [];
    try { folders = fs.readdirSync(projects, { withFileTypes: true }).filter((entry) => entry.isDirectory()); } catch { /* koi notebook nahi */ }
    for (const folder of folders) {
      for (const name of [`${sessionId}.jsonl`, sessionId]) {
        const target = path.join(projects, folder.name, name);
        try {
          if (!fs.existsSync(target)) continue;
          fs.rmSync(target, { recursive: true, force: true });
          removed += 1;
        } catch (err) {
          console.warn('[VoiceBeta] notebook mit na saki:', err.message);
        }
      }
    }
    return { ok: true, removed };
  }

  /**
   * Chat box ki wo files jo disk par nahi (paste ki gayi tasveer): PC par rakh
   * kar raasta lautao. Dimagh (Claude Agent SDK) inhein khud kholta hai — jaise
   * Claude Code mein file ka raasta de do. Chuni gayi files ka apna raasta
   * pehle se hota hai, wo yahan nahi aatin.
   */
  stageFiles({ files } = {}) {
    if (!Array.isArray(files) || files.length === 0 || files.length > MAX_ATTACHMENTS) return { ok: false, code: 'bad_files' };
    const decoded = [];
    for (const file of files) {
      const raw = String((file && file.data) || '');
      const buffer = Buffer.from(raw.startsWith('data:') ? raw.slice(raw.indexOf(',') + 1) : raw, 'base64');
      if (!buffer.length || buffer.length > MAX_ATTACHMENT_BYTES) return { ok: false, code: 'bad_files' };
      decoded.push({ name: safeFileName(file.name, String(file.mimeType || '')), buffer });
    }
    const root = path.join(this.userDataDir, ATTACHMENTS_DIR);
    this._sweepAttachments(root);
    const dir = path.join(root, `${Date.now()}-${crypto.randomBytes(4).toString('hex')}`);
    try {
      fs.mkdirSync(dir, { recursive: true });
      const used = new Set();
      const paths = decoded.map(({ name, buffer }) => {
        // Do files ka ek hi naam ("image.png" do dafa paste): doosri "image (2).png".
        const ext = path.extname(name);
        let unique = name;
        for (let n = 2; used.has(unique.toLowerCase()); n += 1) unique = `${name.slice(0, name.length - ext.length)} (${n})${ext}`;
        used.add(unique.toLowerCase());
        const target = path.join(dir, unique);
        fs.writeFileSync(target, buffer);
        return target;
      });
      return { ok: true, paths };
    } catch (err) {
      console.warn('[VoiceBeta] file rakhi na ja saki:', err.message);
      return { ok: false, code: 'stage_failed', message: err.message };
    }
  }

  /** Purani rakhi hui files (ATTACHMENT_KEEP_MS se purani) — jo mit na sake wo agli dafa. */
  _sweepAttachments(root) {
    let folders = [];
    try { folders = fs.readdirSync(root, { withFileTypes: true }).filter((entry) => entry.isDirectory()); } catch { return; }
    const cutoff = Date.now() - ATTACHMENT_KEEP_MS;
    for (const folder of folders) {
      const target = path.join(root, folder.name);
      try {
        if (fs.statSync(target).mtimeMs < cutoff) fs.rmSync(target, { recursive: true, force: true });
      } catch { /* band file: agli dafa */ }
    }
  }

  /** Engine band. */
  stop() {
    this._cancelIdleStop();
    this._endScreen('stopped');
    this._failOpenWork('The engine was stopped before the work finished.');
    this._session = null;
    this._workCloud = null;
    const child = this.child;
    this.child = null;
    this.port = null;
    this.token = null;
    if (child && child.pid) killProcessTree(child.pid);
    if (this.state !== 'stopped') this._setState('stopped');
    return { ok: true };
  }

  /** App band ho rahi hai: poora tree, abhi (sync). */
  shutdown() {
    this._shuttingDown = true;
    this._cancelIdleStop();
    const child = this.child;
    this.child = null;
    if (child && child.pid) killProcessTree(child.pid, { sync: true });
    this.state = 'stopped';
  }
}

module.exports = {
  VoiceBetaManager, PROVIDERS, AVAILABLE_PROVIDERS, THINKING, MODES, VOICES, AIMS, DEFAULT_MODELS, engineLaunch, legacyBytecodeDir, startTiming,
};
