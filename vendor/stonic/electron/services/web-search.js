'use strict';
// ═══════════════════════════════════════════════════════════════
//  Web search — Stonic Voice (New / Beta) ka `web_search` tool
//
//  Beta ka dimagh "web_search" maange to renderer (lib/beta/agent.ts) yahan
//  IPC se aata hai. Search main process mein is liye hai ke Beta ki chabiyan
//  sirf yahin rehti hain (voice-beta-manager.js) — renderer ko kabhi poori
//  chabi nahi milti. Raasta wohi jo user ne Beta Settings mein chuna:
//
//    Stonic Cloud   gateway ka /v1/search (Vertex par Google Search, user ke
//                   plan ke andar). Koi key nahi.
//    Custom API     1. user ki Gemini key — Google Search grounding.
//                   2. Gemini se na ho sake to Claude ki apni web search, usi
//                      key par jis par dimagh chalta hai (Anthropic, ya
//                      OpenRouter par Claude).
//
//  Gemini ke models ki tarteeb — Google ki shartein (23 Oct 2026, naap ke saath):
//    - 2.5 models "naye users" ke liye band: 404 "no longer available to new
//      users" (Safwan wali shikayat). Purane users ko ab bhi milte hain.
//    - Free key par Google Search SIRF 2.5 par hai; har 3.x model par free tier
//      mein "Not available" (paid key: 5,000 searches / mahina muft, phir
//      $14 / 1000). Yani naya free-key user Gemini se search kar hi nahi
//      sakta — us ke liye Claude wala raasta.
//    - 3.5 Flash-Lite pehle: sab se tez (1.7–2.3 s naapa), Google ke mutabiq
//      kam az kam July 2027 tak. 2.5 Flash purane free users ke liye (wahan
//      search muft hai). 3.8 Flash aakhri sahara.
//    - Har model ki "soch" ki setting alag: 3.5 Flash-Lite `thinkingBudget: 0`
//      par 400 deta hai (sirf thinkingLevel leta hai), 3.8 Flash `minimal`
//      nahi leta. Isi liye har model ke saath us ki apni setting.
// ═══════════════════════════════════════════════════════════════

const crypto = require('crypto');

const GEMINI_BASE = 'https://generativelanguage.googleapis.com/v1beta';
const GEMINI_SEARCH_MODELS = [
  { model: 'gemini-3.5-flash-lite', thinkingConfig: { thinkingLevel: 'minimal' } },
  { model: 'gemini-2.5-flash', thinkingConfig: { thinkingBudget: 0 } },
  { model: 'gemini-3.8-flash', thinkingConfig: { thinkingLevel: 'low' } },
];

// Claude ka search sirf ek lookup hai: sab se sasta Claude kaafi hai (naapa:
// ~6 s, ~$0.012 fi search — $0.01 Anthropic ki search fee + tokens). Haiku 5.5
// par `web_search_20250305` (naya _20260209 sirf Opus / Sonnet ke liye; Haiku
// par us ke jawab behtar nahi the).
const ANTHROPIC_URL = 'https://api.anthropic.com/v1/messages';
const ANTHROPIC_SEARCH_MODEL = 'claude-haiku-5-5';
const OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions';
const OPENROUTER_SEARCH_MODEL = 'anthropic/claude-haiku-5.5';

// Engine tool ka 90 s intezar karta hai (settings.toml tool_timeout_secs) —
// poora silsila (Gemini + Claude) us se kaafi neeche rehna chahiye.
const GEMINI_TIMEOUT_MS = 15 * 1000;
const CLAUDE_TIMEOUT_MS = 35 * 1000;
const CLOUD_TIMEOUT_MS = 35 * 1000;

const MAX_QUERY_CHARS = 500;
const MAX_RESULT_CHARS = 3000;
const MAX_SOURCES = 5;

// Jo key Gemini se search nahi kar sakti (naya free user, hadd khatam), us par
// itni der seedha Claude — warna har search par teen nakaam calls (~1.5 s).
const GEMINI_SKIP_MS = 10 * 60 * 1000;

/** Har key ki yaad: kaun sa model chala, ya Gemini kab tak chhorna hai. Chabi khud nahi — sirf us ka nishan. */
const memory = new Map();
const MAX_MEMORY = 20;
const fingerprint = (key) => crypto.createHash('sha256').update(String(key)).digest('hex').slice(0, 16);

function remember(key, value) {
  const id = fingerprint(key);
  memory.delete(id);
  memory.set(id, value);
  while (memory.size > MAX_MEMORY) memory.delete(memory.keys().next().value);
}

/** Sirf test ke liye. */
function forgetAll() { memory.clear(); }

const cleanQuery = (value) => String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, MAX_QUERY_CHARS);

/** Model ke liye natija: jawab + sources, hadd ke andar. */
function formatResult({ text, sources }) {
  const list = (sources || []).filter((s) => s && s.uri).slice(0, MAX_SOURCES)
    .map((s) => `• ${s.title || s.uri}: ${s.uri}`).join('\n');
  const full = list ? `${String(text || '').trim()}\n\nSources:\n${list}` : String(text || '').trim();
  return full.length > MAX_RESULT_CHARS ? `${full.slice(0, MAX_RESULT_CHARS)}\n[...truncated]` : full;
}

const today = (now = new Date()) =>
  now.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });

// Claude (dono raaste) ko hidayat. Musbat aur chhoti: kya karna hai.
const searchInstruction = (now) => [
  'You look things up on the web for a voice assistant.',
  `Today is ${today(now)}.`,
  'Search the web, then answer the question in a few short sentences with the key facts (names, numbers, dates, places).',
  'Base the answer on what the search found, and say plainly when the search did not find it.',
].join(' ');

/** POST JSON; kabhi throw nahi karta. status 0 = network / waqt khatam (jawab aaya hi nahi). */
async function postJson(fetchImpl, url, headers, body, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...headers },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const raw = await response.text().catch(() => '');
    let data = null;
    try { data = raw ? JSON.parse(raw) : null; } catch { data = null; }
    return { status: response.status, data };
  } catch (err) {
    return { status: 0, data: null, error: controller.signal.aborted ? 'timed out' : (err?.message || String(err)) };
  } finally {
    clearTimeout(timer);
  }
}

const shortMessage = (value, limit = 220) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);

// ── Stonic Cloud ─────────────────────────────────────────────────────────────

/** Gateway ka /v1/search (search.js). Jawab: { text, sources: [{title, uri}] }. */
async function searchCloud({ url, token, query, fetchImpl = fetch }) {
  const res = await postJson(fetchImpl, `${url}/v1/search`, { authorization: `Bearer ${token}` }, { query }, CLOUD_TIMEOUT_MS);
  if (res.status === 200) {
    const text = String(res.data?.text || '').trim();
    const sources = (Array.isArray(res.data?.sources) ? res.data.sources : [])
      .filter((s) => s && s.uri).map((s) => ({ title: shortMessage(s.title || s.uri, 80), uri: String(s.uri) }));
    return { ok: true, via: 'cloud', text: text || 'No results found.', sources };
  }
  const code = String(res.data?.error?.message || '');
  if (res.status === 402 || code === 'insufficient_balance' || code === 'plan_required') {
    return { ok: false, code: 'cloud_no_plan', permanent: true };
  }
  if (res.status === 429) return { ok: false, code: 'cloud_limit', permanent: true };
  if (res.status === 401 || res.status === 403) return { ok: false, code: 'cloud_signin', permanent: true };
  return { ok: false, code: 'cloud_unavailable', permanent: false, detail: res.error || `HTTP ${res.status}` };
}

// ── Gemini (user ki key) ─────────────────────────────────────────────────────

function parseGemini(data) {
  const candidate = data?.candidates?.[0];
  const text = (candidate?.content?.parts || [])
    .map((part) => (part && part.thought !== true && typeof part.text === 'string' ? part.text : ''))
    .join('').trim();
  const sources = (candidate?.groundingMetadata?.groundingChunks || [])
    .filter((chunk) => chunk?.web?.uri)
    .map((chunk) => ({ title: shortMessage(chunk.web.title || chunk.web.uri, 80), uri: String(chunk.web.uri) }));
  return { text, sources };
}

/** Google kehta hai chabi hi ghalat / band hai — doosre models par bhi wohi hoga. */
const isBadKey = (res) => {
  const message = `${res.data?.error?.status || ''} ${res.data?.error?.message || ''} ${JSON.stringify(res.data?.error?.details || '')}`;
  return /API_KEY_INVALID|API key not valid|API key expired|API_KEY_SERVICE_BLOCKED|leaked/i.test(message);
};

/**
 * User ki Gemini key se Google Search. Models tarteeb se; jo chala wo yaad
 * rehta hai (agli dafa pehle wohi). Koi na chale to key thori der ke liye
 * "Gemini se search nahi" yaad — tab seedha Claude.
 *
 * @returns {Promise<{ok: true, via: string, text: string, sources: object[]} |
 *   {ok: false, network?: boolean, badKey?: boolean, reason: string}>}
 */
async function searchGemini({ key, query, fetchImpl = fetch, now = Date.now }) {
  const id = fingerprint(key);
  const seen = memory.get(id) || {};
  if (seen.skipUntil && seen.skipUntil > now()) return { ok: false, skipped: true, badKey: Boolean(seen.badKey), reason: seen.reason || '' };

  const order = seen.model
    ? [...GEMINI_SEARCH_MODELS].sort((a, b) => Number(b.model === seen.model) - Number(a.model === seen.model))
    : GEMINI_SEARCH_MODELS;
  const failures = [];
  let badKey = false;
  for (const entry of order) {
    const res = await postJson(fetchImpl, `${GEMINI_BASE}/models/${entry.model}:generateContent`, { 'x-goog-api-key': key }, {
      contents: [{ role: 'user', parts: [{ text: query }] }],
      tools: [{ googleSearch: {} }],
      generationConfig: { thinkingConfig: entry.thinkingConfig },
    }, GEMINI_TIMEOUT_MS);

    if (res.status === 0) {
      // Google tak pohnche hi nahi (internet / waqt khatam) — doosra model bhi
      // wahin atkega. Yaad mein kuch nahi likhte: ye key ka qasoor nahi.
      return { ok: false, network: true, reason: `Google did not answer (${res.error})` };
    }
    if (res.status === 200) {
      const parsed = parseGemini(res.data);
      if (parsed.text) {
        remember(key, { model: entry.model });
        return { ok: true, via: entry.model, ...parsed };
      }
      failures.push(`${entry.model}: empty answer`);
      continue;
    }
    failures.push(`${entry.model}: ${res.status} ${shortMessage(res.data?.error?.message)}`);
    if (isBadKey(res)) { badKey = true; break; }
  }

  const reason = failures.join(' | ');
  remember(key, { model: seen.model || null, skipUntil: now() + GEMINI_SKIP_MS, reason, badKey });
  return { ok: false, badKey, reason };
}

// ── Claude (Anthropic key) ───────────────────────────────────────────────────

function parseAnthropic(data) {
  const blocks = Array.isArray(data?.content) ? data.content : [];
  const text = blocks.filter((b) => b?.type === 'text').map((b) => b.text || '').join('').trim();
  const results = blocks.filter((b) => b?.type === 'web_search_tool_result');
  const sources = [];
  for (const block of results) {
    if (!Array.isArray(block.content)) continue;
    for (const item of block.content) {
      if (item?.url && !sources.some((s) => s.uri === item.url)) {
        sources.push({ title: shortMessage(item.title || item.url, 80), uri: String(item.url) });
      }
    }
  }
  // Search ki apni nakaami HTTP 200 ke andar aati hai (content ek object, list nahi).
  const errors = results.filter((b) => b.content && !Array.isArray(b.content)).map((b) => b.content.error_code).filter(Boolean);
  const searched = Number(data?.usage?.server_tool_use?.web_search_requests || 0) > 0 || sources.length > 0;
  return { text, sources, errors, searched };
}

async function searchAnthropic({ key, query, fetchImpl = fetch, now = () => new Date() }) {
  const res = await postJson(fetchImpl, ANTHROPIC_URL, { 'x-api-key': key, 'anthropic-version': '2023-06-01' }, {
    model: ANTHROPIC_SEARCH_MODEL,
    max_tokens: 2048,
    output_config: { effort: 'low' },
    system: searchInstruction(now()),
    tools: [{ type: 'web_search_20250305', name: 'web_search', max_uses: 3 }],
    messages: [{ role: 'user', content: query }],
  }, CLAUDE_TIMEOUT_MS);
  if (res.status !== 200) {
    return { ok: false, network: res.status === 0, reason: `Claude: ${res.status || ''} ${shortMessage(res.data?.error?.message || res.error)}`.trim() };
  }
  const parsed = parseAnthropic(res.data);
  if (!parsed.text || !parsed.searched) {
    return { ok: false, reason: `Claude web search: ${parsed.errors.join(', ') || 'no search result'}` };
  }
  return { ok: true, via: ANTHROPIC_SEARCH_MODEL, text: parsed.text, sources: parsed.sources };
}

// ── Claude (OpenRouter key) ──────────────────────────────────────────────────

async function searchOpenRouter({ key, query, fetchImpl = fetch, now = () => new Date() }) {
  const res = await postJson(fetchImpl, OPENROUTER_URL, {
    authorization: `Bearer ${key}`,
    'HTTP-Referer': 'https://stonicai.com',
    'X-Title': 'Stonic',
  }, {
    model: OPENROUTER_SEARCH_MODEL,
    max_tokens: 2048,
    plugins: [{ id: 'web', max_results: MAX_SOURCES }],
    messages: [
      { role: 'system', content: searchInstruction(now()) },
      { role: 'user', content: query },
    ],
  }, CLAUDE_TIMEOUT_MS);
  if (res.status !== 200) {
    return { ok: false, network: res.status === 0, reason: `OpenRouter: ${res.status || ''} ${shortMessage(res.data?.error?.message || res.error)}`.trim() };
  }
  const message = res.data?.choices?.[0]?.message || {};
  const text = typeof message.content === 'string' ? message.content.trim() : '';
  const sources = [];
  for (const note of Array.isArray(message.annotations) ? message.annotations : []) {
    const cite = note?.type === 'url_citation' ? note.url_citation : null;
    if (cite?.url && !sources.some((s) => s.uri === cite.url)) sources.push({ title: shortMessage(cite.title || cite.url, 80), uri: String(cite.url) });
  }
  if (!text) return { ok: false, reason: 'OpenRouter web search: empty answer' };
  return { ok: true, via: OPENROUTER_SEARCH_MODEL, text, sources };
}

// ── Custom API: poora silsila ────────────────────────────────────────────────

/**
 * User ki apni keys se search: pehle Gemini, phir Claude (jo key ho).
 *
 * @param {{gemini?: string, anthropic?: string, openrouter?: string}} keys
 * @returns {Promise<{ok: true, via: string, text: string, sources: object[]} |
 *   {ok: false, code: string, permanent: boolean, reason: string}>}
 */
async function searchWithOwnKeys({ keys = {}, query, fetchImpl = fetch, log = () => {} }) {
  const reasons = [];
  let network = false;

  if (keys.gemini) {
    const gemini = await searchGemini({ key: keys.gemini, query, fetchImpl });
    if (gemini.ok) return gemini;
    network = Boolean(gemini.network);
    reasons.push(`Gemini key — ${gemini.reason}`);
    log(`Gemini se search nahi hui${gemini.skipped ? ' (yaad se)' : ''}: ${gemini.reason}`);
  }

  // Claude ki search user ke Claude account par likhi jati hai — sirf us key par
  // jis se dimagh chalta hai (manager wohi deta hai).
  const claude = keys.anthropic ? searchAnthropic : keys.openrouter ? searchOpenRouter : null;
  if (claude && !network) {
    const result = await claude({ key: keys.anthropic || keys.openrouter, query, fetchImpl });
    if (result.ok) return result;
    network = Boolean(result.network);
    reasons.push(result.reason);
    log(`Claude se bhi search nahi hui: ${result.reason}`);
  }

  if (network) return { ok: false, code: 'search_network', permanent: false, reason: reasons.join(' | ') };
  if (!keys.gemini && !claude) return { ok: false, code: 'search_no_key', permanent: true, reason: 'no Gemini or Claude key' };
  return { ok: false, code: 'search_unavailable', permanent: true, reason: reasons.join(' | ') };
}

// ── Model ke liye jawab ──────────────────────────────────────────────────────

/** Nakaami par dimagh ko kya batana hai (wo user ko ek chhoti line mein bolta hai). */
function failureText(result) {
  switch (result.code) {
    case 'cloud_no_plan':
      return 'Web search could not run: the Stonic Cloud plan is not active. Tell the user in one short line that web search needs an active plan (stonicai.com), and continue without it.';
    case 'cloud_limit':
      return 'Web search could not run: the Stonic Cloud usage limit is reached for now. Tell the user in one short line, and continue without the search.';
    case 'cloud_signin':
      return 'Web search could not run: the Stonic Cloud sign-in needs refreshing. Tell the user in one short line to sign in again from Settings.';
    case 'search_no_key':
      return 'Web search needs an API key. Tell the user in one short line to add their Gemini key (or Claude key) in New voice Settings, and continue without the search.';
    case 'search_unavailable':
      return 'Web search is not available on the user\'s own API keys right now. '
        + `Details: ${shortMessage(result.reason, 400)}. `
        + 'Tell the user in one short line: Google allows web search on Gemini keys with billing turned on (aistudio.google.com), '
        + 'and answer from what you already know if you can. Wait for the user before searching again.';
    default:
      return 'The web search did not go through this time (network). Try once more with a shorter query; if it fails again, tell the user in one short line.';
  }
}

module.exports = {
  GEMINI_SEARCH_MODELS,
  ANTHROPIC_SEARCH_MODEL,
  OPENROUTER_SEARCH_MODEL,
  MAX_QUERY_CHARS,
  cleanQuery,
  formatResult,
  failureText,
  searchCloud,
  searchGemini,
  searchAnthropic,
  searchOpenRouter,
  searchWithOwnKeys,
  _forgetAll: forgetAll,
};
