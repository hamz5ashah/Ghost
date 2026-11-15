'use strict';
// ═══════════════════════════════════════════════════════════════
//  World Feed — "Today's Headlines" card ki apni khabrein
//
//  Zariya: GDELT DOC 2.0 API (khula, bina API key). GDELT ki shart: commercial
//  istemaal jaiz hai magar har jagah "GDELT Project" ka naam + gdeltproject.org
//  ka link. Is liye har jawab mein `attribution` + `attributionUrl` aate hain.
//
//  Sab kuch MAIN process mein: renderer kabhi bahar ka content load nahi karta.
//  Thumbnails bhi yahin download ho kar chhote JPEG `data:` URL bante hain.
//
//  Cache: memory + `cacheFile` (JSON, atomic write). TTL 4 ghante (din mein
//  ~6 refresh). Nakaami par purani khabrein rehti hain (`stale: true`) aur
//  15 min ruk kar dobara koshish. Ek waqt mein sirf EK refresh (sab usi ka
//  intezar karte hain) — GDELT ~1 request / 5 s se zyada nahi maanta.
//
//  Bahar se aata hai (taake ye file saade Node mein test ho sake):
//    fetchImpl      main mein Electron ka `net.fetch` (system proxy maanta hai)
//    makeThumbnail  buffer → chhota JPEG data URL (main mein nativeImage)
//
//  GDELT ka data (title, url, image) PARAYA hai: har cheez saaf / jaanchi
//  jati hai, aur sirf neeche ki outlets ke https link qubool hain.
// ═══════════════════════════════════════════════════════════════

const fsp = require('fs/promises');
const path = require('path');
const crypto = require('crypto');

const GDELT_DOC_API = 'https://api.gdeltproject.org/api/v2/doc/doc';
const ATTRIBUTION = 'GDELT Project';
const ATTRIBUTION_URL = 'https://www.gdeltproject.org/';
// Seedha naam, koi user / machine ki maloomat nahi.
const USER_AGENT = 'StonicAI-Desktop/WorldFeed';

const DEFAULTS = Object.freeze({
  ttlMs: 4 * 60 * 60 * 1000,
  backoffMs: 15 * 60 * 1000,
  forceMinGapMs: 60 * 1000,
  // GDELT: "one request every 5 seconds". Thora fasla zyada rakha.
  queryGapMs: 6000,
  // 429 / 5xx / network par ruk kar dobara (10 s, phir 20 s) — fetchQuery.
  retryDelayMs: 10000,
  firstLoadTimeoutMs: 20000,
  // GDELT yahan se ~11–12 s leta hai (429 bhi itni der mein) — live naapa.
  requestTimeoutMs: 25000,
  thumbTimeoutMs: 8000,
});

const MAX_ITEMS = 20;
const MAX_PER_SOURCE = 5;
const MIN_PER_TOPIC = 6;
const THUMB_COUNT = 8;
const THUMB_CONCURRENCY = 4;
const THUMB_MAX_BYTES = 3 * 1024 * 1024;
const THUMB_MAX_PIXELS = 40 * 1000 * 1000; // decode bomb se bachao
const THUMB_MIN_SIDE = 32; // tracking pixel / chhote logo nahi
const MAX_REDIRECTS = 3;
const TRANSIENT_RETRIES = 2;
const API_MAX_BYTES = 2 * 1024 * 1024;
const TITLE_MAX = 160;
const TITLE_MIN = 12;
// "Aaj ki" khabar = 24 ghante. Query 72 ghante ki hai (DateDesc, to taaza
// pehle aati hain): GDELT ka DOC index kabhi ghanton peeche reh jata hai
// (4 Nov 2026 ko live dekha: poore 24h mein sirf ek 15-min batch). Itni
// taaza khabrein na milein to purani (asli tareekh ke saath) se card bharo.
const FRESH_MS = 24 * 60 * 60 * 1000;
const MIN_FRESH = 8;
const MAX_AGE_MS = 72 * 60 * 60 * 1000;
const URL_MAX = 2048;
const DATA_URL_MAX = 200 * 1024;
const CARRY_OVER_MS = 36 * 60 * 60 * 1000;
const CACHE_VERSION = 1;

// GDELT lambi query nahi maanta: 200 + text/html "Your query was too short
// or too long." (live dekha: 263 harf = rad, 224 = theek). Har query ki
// had QUERY_MAX_CHARS, test isay pakka karta hai. `domain:` (`domainis:` se
// chhota) — OR group ke saath live chala. `domain:` aadha-milan bhi le sakta
// hai, magar jawab mein sirf neeche ke outlets ke link qubool hain.
const QUERY_MAX_CHARS = 220;

// Har topic: domain → dikhane wala naam. Query sirf inhi se khabrein maangti
// hai, aur jawab mein bhi sirf inhi ke link qubool hain (dohri hifazat:
// `open()` kabhi kisi aur site ka link nahi kholta).
//  - bbc.com nahi: GDELT BBC ko bbc.co.uk likhta hai (live data mein 0 bbc.com).
//  - abc.net.au: muft, GDELT mein live maujood (card patla na rahe).
//  - Science mein BBC nahi: domain filter se BBC ki HAR khabar aa jati.
const QUERIES = Object.freeze([
  {
    topic: 'world',
    outlets: {
      'reuters.com': 'Reuters',
      'apnews.com': 'AP News',
      'bbc.co.uk': 'BBC',
      'aljazeera.com': 'Al Jazeera',
      'theguardian.com': 'The Guardian',
      'npr.org': 'NPR',
      'dw.com': 'DW',
      'france24.com': 'France 24',
      'abc.net.au': 'ABC News (AU)',
    },
  },
  {
    topic: 'science',
    outlets: {
      'nasa.gov': 'NASA',
      'space.com': 'Space.com',
      'nature.com': 'Nature',
      'arstechnica.com': 'Ars Technica',
      'theverge.com': 'The Verge',
      'techcrunch.com': 'TechCrunch',
      'wired.com': 'WIRED',
      'livescience.com': 'Live Science',
      'phys.org': 'Phys.org',
    },
  },
].map((q) => Object.freeze({ ...q, domains: Object.keys(q.outlets) })));

const OUTLETS = Object.freeze(Object.assign({}, ...QUERIES.map((q) => q.outlets)));
const TOPICS = new Set(QUERIES.map((q) => q.topic));

// Khabar nahi: audio / TV (BBC Sounds — GDELT mein bohat aate hain) aur
// index pages (author, tag, topic, AP "hub") — asli data mein dekhe gaye.
const NON_ARTICLE_PATH = /(^|\/)(sounds|iplayer|programmes|podcasts?|audio|authors?|tags?|topics?|category|categories|hub|sections?|profiles?)(\/|$)/i;

// ── Chhote madadgar ──────────────────────────────────────────────

const noop = () => {};
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));

function makeLogger(logger) {
  const base = logger && typeof logger === 'object' ? logger : console;
  const pick = (name) => (typeof base[name] === 'function' ? base[name].bind(base) : noop);
  const warn = pick('warn');
  const info = typeof base.info === 'function' ? pick('info') : pick('log');
  return {
    warn: (...args) => { try { warn('[WorldFeed]', ...args); } catch { /* log kabhi na roke */ } },
    info: (...args) => { try { info('[WorldFeed]', ...args); } catch { /* log kabhi na roke */ } },
  };
}

const clip = (s, max) => {
  const str = String(s == null ? '' : s);
  return str.length > max ? str.slice(0, max) : str;
};

// `domain:` SUBSTRING milan hai (live dekha: npr.org → knpr.org, dw.com →
// wrdw.com, apnews.com → kelownacapnews.com — 49 mein se 20 faltu). Chhote,
// takrane wale domains par exact `domainis:` (2 harf mehnga, is liye sab par nahi).
const EXACT_DOMAINS = new Set(['dw.com', 'npr.org', 'apnews.com', 'space.com']);

/** GDELT query: OR wale domains qaus mein (GDELT ka usool) + sirf English. */
const buildQuery = (domains) => `(${domains
  .map((d) => `${EXACT_DOMAINS.has(d) ? 'domainis' : 'domain'}:${d}`)
  .join(' OR ')}) sourcelang:english`;

function buildQueryUrl(domains) {
  const params = new URLSearchParams({
    query: buildQuery(domains),
    mode: 'ArtList',
    format: 'json',
    maxrecords: '150',
    timespan: '72h',
    sort: 'DateDesc',
  });
  return `${GDELT_DOC_API}?${params.toString()}`;
}

/** Sirf saaf https URL (koi login / port nahi). Nahi to null. */
function safeHttpsUrl(value) {
  if (typeof value !== 'string') return null;
  const raw = value.trim();
  if (!raw || raw.length > URL_MAX) return null;
  let u;
  try { u = new URL(raw); } catch { return null; }
  if (u.protocol !== 'https:') return null;
  if (u.username || u.password) return null;
  if (u.port && u.port !== '443') return null;
  if (!u.hostname || !u.hostname.includes('.')) return null;
  return u.toString();
}

/**
 * Image ka URL: https + aam host. IP literal / localhost / .local nahi —
 * paraya data ghar ke router ya local server ko request na bhijwa sake.
 */
function isSafeImageUrl(value) {
  const url = safeHttpsUrl(value);
  if (!url) return false;
  const host = new URL(url).hostname.toLowerCase();
  if (host.startsWith('[') || /^[\d.]+$/.test(host)) return false;
  if (host === 'localhost' || /\.(localhost|local|internal|lan|home|corp)$/.test(host)) return false;
  return true;
}

/** Host kis outlet ka hai (subdomain bhi chalega). Na ho to null. */
function outletFor(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/\.$/, '');
  for (const domain of Object.keys(OUTLETS)) {
    if (host === domain || host.endsWith(`.${domain}`)) return domain;
  }
  return null;
}

const NAMED_ENTITIES = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ',
  ndash: '–', mdash: '—', lsquo: '‘', rsquo: '’', ldquo: '“', rdquo: '”',
  sbquo: '‚', bdquo: '„', hellip: '…', laquo: '«', raquo: '»', middot: '·',
  bull: '•', deg: '°', pound: '£', euro: '€', copy: '©', reg: '®', trade: '™',
  eacute: 'é', egrave: 'è', aacute: 'á', agrave: 'à', iacute: 'í', oacute: 'ó',
  uacute: 'ú', ntilde: 'ñ', ccedil: 'ç', uuml: 'ü', ouml: 'ö', auml: 'ä',
};

function decodeEntitiesOnce(s) {
  return s.replace(/&(#[xX][0-9a-fA-F]{1,6}|#\d{1,7}|[a-zA-Z][a-zA-Z0-9]{1,15});/g, (match, code) => {
    if (code[0] === '#') {
      const n = code[1] === 'x' || code[1] === 'X' ? parseInt(code.slice(2), 16) : parseInt(code.slice(1), 10);
      if (!Number.isFinite(n) || n <= 0 || n > 0x10ffff || (n >= 0xd800 && n <= 0xdfff)) return '';
      return String.fromCodePoint(n);
    }
    const named = NAMED_ENTITIES[code.toLowerCase()];
    return named === undefined ? match : named;
  });
}

/** HTML tags / entities / control chars hata kar ek line ka saada text. */
function cleanText(value) {
  let s = String(value == null ? '' : value);
  s = s.replace(/<[^>]*>/g, ' ');
  // Do dafa: feeds mein "&amp;#39;" jaisi dohri encoding aam hai.
  s = decodeEntitiesOnce(decodeEntitiesOnce(s));
  // Decode ke baad bani hui tags (sirf tag jaisi shakl) bhi nahi.
  s = s.replace(/<\/?[a-zA-Z][^<>]*>/g, ' ');
  // Control / zero-width / bidi chars: dikhte nahi magar text ulat sakte hain.
  s = s.replace(/[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩﻿]/g, ' ');
  return s.replace(/\s+/g, ' ').trim();
}

const alnum = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');

/** Aakhir mein " - BBC News" / " | Reuters" jaisa outlet ka naam ho to hatao. */
function stripOutletSuffix(title, source, domain) {
  const markers = new Set([alnum(source), alnum(domain), alnum(String(domain || '').split('.')[0])].filter((m) => m.length >= 2));
  let out = title;
  for (let i = 0; i < 2; i += 1) {
    const m = out.match(/^(.*\S)\s+[|\-–—]\s+([^|\-–—]{2,40})$/);
    if (!m) break;
    const tail = alnum(m[2]);
    if (![...markers].some((mk) => tail.includes(mk))) break;
    out = m[1];
  }
  return out;
}

/** Title ko itne harf tak, lafz beech mein na kate. */
function clampTitle(s, max = TITLE_MAX) {
  if (s.length <= max) return s;
  const cut = s.slice(0, max - 1);
  const space = cut.lastIndexOf(' ');
  return `${(space > max * 0.6 ? cut.slice(0, space) : cut).replace(/[\s,;:.\-–—]+$/, '')}…`;
}

/**
 * GDELT ke title saaf: HTML/entities, outlet ka naam, aur GDELT ki apni
 * aadatein (asli jawab mein dekhi): punctuation se pehle space ("charts :
 * What", "year , Trump"), hataye gaye quotes ki jagah dohri space, aur "UK"
 * ki jagah chhote harfon mein "united kingdom".
 */
function cleanTitle(value, source, domain) {
  let s = cleanText(value);
  if (!s) return '';
  s = s.replace(/\bunited kingdom\b/g, 'UK').replace(/\bunited states\b/g, 'US');
  s = s.replace(/(\d)\s+:\s+(\d)/g, '$1:$2');
  // "high - speed" / "Twenty - year" → jora hua lafz. Aage bara harf ho
  // ("Ukraine - Russia", "Today - UK") to asli separator hai, rehne do.
  s = s.replace(/([\p{L}\d]) - (?=\p{Ll})/gu, '$1-');
  s = s.replace(/\s+([,.;:!?%)\]}])/g, '$1');
  s = s.replace(/([(\[{])\s+/g, '$1');
  s = stripOutletSuffix(s, source, domain);
  return clampTitle(s.trim());
}

/** "20261104T013000Z" → ms epoch (UTC). Ghalat ho to null. */
function parseSeenDate(value) {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(String(value || '').trim());
  if (!m) return null;
  const [y, mo, d, h, mi, se] = m.slice(1).map(Number);
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || se > 59) return null;
  const t = Date.UTC(y, mo - 1, d, h, mi, se);
  return Number.isFinite(t) ? t : null;
}

/**
 * GDELT ka jawab → articles[]. Koi khabar na ho to GDELT `{}` deta hai.
 * Ghalat query par 200 ke saath saada text aata hai (JSON nahi) → error.
 * Kabhi kabhi title mein kachcha backslash / control char JSON tod deta hai:
 * ek dafa marammat kar ke dobara parse.
 */
function parseGdeltBody(text) {
  const body = String(text == null ? '' : text).replace(/^﻿/, '').trim();
  if (!body) return [];
  if (body[0] !== '{') {
    const err = new Error(`GDELT non-JSON reply: ${clip(cleanText(body), 120)}`);
    err.code = 'bad-response';
    throw err;
  }
  let data;
  try {
    data = JSON.parse(body);
  } catch {
    try {
      data = JSON.parse(repairJson(body));
    } catch {
      const err = new Error('GDELT reply is not valid JSON');
      err.code = 'bad-response';
      throw err;
    }
  }
  return data && Array.isArray(data.articles) ? data.articles : [];
}

function repairJson(s) {
  return s
    .replace(/[\u0000-\u001f]/g, ' ')
    // Sahi escape jaisa hai rahe; akela / ghalat backslash dohra.
    .replace(/\\(["\\/bfnrt]|u[0-9a-fA-F]{4})|\\/g, (match, esc) => (esc ? match : '\\\\'));
}

const hashId = (url) => crypto.createHash('sha256').update(url).digest('hex').slice(0, 16);

/** Ek GDELT article → hamara item (ya null). `imageUrl` andar ka hai, bahar nahi jata. */
function normalizeArticle(raw, topic, nowMs) {
  if (!raw || typeof raw !== 'object') return null;
  const url = safeHttpsUrl(raw.url);
  if (!url) return null;
  const parsed = new URL(url);
  const domain = outletFor(parsed.hostname);
  if (!domain) return null;
  if (NON_ARTICLE_PATH.test(parsed.pathname)) return null;
  if (raw.language != null && String(raw.language).trim().toLowerCase() !== 'english') return null;
  const seen = parseSeenDate(raw.seendate);
  if (seen == null || nowMs - seen > MAX_AGE_MS) return null;
  const source = OUTLETS[domain];
  const title = cleanTitle(raw.title, source, domain);
  if (title.length < TITLE_MIN) return null;
  const country = clip(cleanText(raw.sourcecountry), 60);
  return {
    id: hashId(url),
    title,
    url,
    source,
    domain,
    publishedAt: Math.min(seen, nowMs),
    image: null,
    country: country || null,
    topic,
    imageUrl: isSafeImageUrl(raw.socialimage) ? safeHttpsUrl(raw.socialimage) : null,
  };
}

const titleKey = (title) => String(title || '')
  .toLowerCase()
  .normalize('NFKD')
  .replace(/[̀-ͯ]/g, '')
  .replace(/[^\p{L}\p{N}]+/gu, ' ')
  .trim();

const TRACKING_PARAM = /^(utm_|fbclid$|gclid$|ocid$|cmpid$|at_)/i;

function urlKey(url) {
  try {
    const u = new URL(url);
    let host = u.hostname.toLowerCase().replace(/^(www|amp|m|mobile)\./, '');
    if (host === 'bbc.co.uk') host = 'bbc.com';
    const params = [...u.searchParams].filter(([k]) => !TRACKING_PARAM.test(k)).sort(([a], [b]) => a.localeCompare(b));
    const query = params.length ? `?${new URLSearchParams(params).toString()}` : '';
    return `${host}${u.pathname.replace(/\/+$/, '')}${query}`;
  } catch {
    return String(url || '');
  }
}

const newestFirst = (a, b) => (b.publishedAt - a.publishedAt) || a.title.localeCompare(b.title);

/**
 * Pehle 3 mein mix: doosra item doosre topic (query) aur outlet se, teesra
 * kisi naye outlet se — agar mile. Baqi list newest-first hi rehti hai.
 */
function interleaveTop(list) {
  if (list.length < 3) return list.slice();
  const pool = list.slice();
  const head = [pool.shift()];
  const pick = (pred) => {
    const i = pool.findIndex(pred);
    return i === -1 ? null : pool.splice(i, 1)[0];
  };
  head.push(
    pick((it) => it.topic !== head[0].topic && it.source !== head[0].source)
      || pick((it) => it.source !== head[0].source)
      || pool.shift(),
  );
  head.push(pick((it) => it.source !== head[0].source && it.source !== head[1].source) || pool.shift());
  return head.concat(pool);
}

/**
 * Dedupe (title + URL) → 24h wali hi (agar MIN_FRESH mil jayein) → har topic
 * ke kam se kam MIN_PER_TOPIC → baqi newest se bharo (ek outlet ke
 * MAX_PER_SOURCE se zyada nahi, jab tak jagah khali na reh jaye) →
 * newest-first → pehle 3 ka mix.
 */
function selectItems(candidates, nowMs = Date.now(), max = MAX_ITEMS) {
  const sorted = candidates.filter(Boolean).slice().sort(newestFirst);
  const seenTitles = new Set();
  const seenUrls = new Set();
  let unique = [];
  for (const it of sorted) {
    const tk = titleKey(it.title);
    const uk = urlKey(it.url);
    if (!tk || seenTitles.has(tk) || seenUrls.has(uk)) continue;
    seenTitles.add(tk);
    seenUrls.add(uk);
    unique.push(it);
  }
  const fresh = unique.filter((it) => nowMs - it.publishedAt <= FRESH_MS);
  if (fresh.length >= MIN_FRESH) unique = fresh;

  const picked = new Set();
  const perSource = new Map();
  const take = (it) => {
    picked.add(it);
    perSource.set(it.source, (perSource.get(it.source) || 0) + 1);
  };
  const underCap = (it) => (perSource.get(it.source) || 0) < MAX_PER_SOURCE;

  for (const topic of TOPICS) {
    let n = 0;
    for (const it of unique) {
      if (n >= MIN_PER_TOPIC || picked.size >= max) break;
      if (it.topic === topic && !picked.has(it) && underCap(it)) { take(it); n += 1; }
    }
  }
  for (const it of unique) {
    if (picked.size >= max) break;
    if (!picked.has(it) && underCap(it)) take(it);
  }
  // Outlets kam hon to cap ki wajah se list khali na rahe.
  for (const it of unique) {
    if (picked.size >= max) break;
    if (!picked.has(it)) take(it);
  }
  return interleaveTop([...picked].sort(newestFirst));
}

/**
 * JPEG / PNG ka size header se (decode kiye baghair). Koi aur format (WebP,
 * GIF, SVG…) → null: nativeImage sirf PNG/JPEG samajhta hai.
 */
function imageInfo(buf) {
  if (!Buffer.isBuffer(buf) || buf.length < 24) return null;
  if (buf.readUInt32BE(0) === 0x89504e47 && buf.readUInt32BE(4) === 0x0d0a1a0a && buf.toString('latin1', 12, 16) === 'IHDR') {
    return { type: 'png', width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
  }
  if (buf[0] === 0xff && buf[1] === 0xd8) {
    let i = 2;
    while (i + 8 < buf.length) {
      if (buf[i] !== 0xff) return null;
      const marker = buf[i + 1];
      if (marker === 0xff) { i += 1; continue; }
      if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd8)) { i += 2; continue; }
      if (marker === 0xd9 || marker === 0xda) return null;
      const len = buf.readUInt16BE(i + 2);
      if (len < 2) return null;
      // SOF0..SOF15, siwaye DHT (C4), JPG (C8), DAC (CC).
      if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
        return { type: 'jpeg', height: buf.readUInt16BE(i + 5), width: buf.readUInt16BE(i + 7) };
      }
      i += 2 + len;
    }
  }
  return null;
}

const THUMB_DATA_URL = /^data:image\/(jpeg|png);base64,[A-Za-z0-9+/]+={0,2}$/;
const isThumbDataUrl = (v) => typeof v === 'string' && v.length <= DATA_URL_MAX && THUMB_DATA_URL.test(v);

function discardBody(res) {
  try {
    const p = res && res.body && typeof res.body.cancel === 'function' ? res.body.cancel() : null;
    if (p && typeof p.catch === 'function') p.catch(noop);
  } catch { /* koi baat nahi */ }
}

/** Body parho, `max` bytes se zyada ho to beech mein hi chhor do. */
async function readCapped(res, max) {
  const tooBig = () => {
    const err = new Error('response too large');
    err.code = 'too-large';
    return err;
  };
  if (res.body && typeof res.body.getReader === 'function') {
    const reader = res.body.getReader();
    const chunks = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      total += value.byteLength;
      if (total > max) {
        try { reader.cancel().catch(noop); } catch { /* koi baat nahi */ }
        throw tooBig();
      }
      chunks.push(Buffer.from(value.buffer, value.byteOffset, value.byteLength));
    }
    return Buffer.concat(chunks, total);
  }
  const buf = Buffer.from(await res.arrayBuffer());
  if (buf.length > max) throw tooBig();
  return buf;
}

/** UI ke liye chhota, saaf error code (GDELT ka kachcha text nahi). */
function errorCode(err) {
  if (!err) return 'unknown';
  if (err.code && /^[a-z-]{2,24}$/.test(err.code)) return err.code;
  if (err.name === 'AbortError' || err.name === 'TimeoutError') return 'timeout';
  return 'network';
}

function publicItem(it) {
  return {
    id: it.id,
    title: it.title,
    url: it.url,
    source: it.source,
    domain: it.domain,
    publishedAt: it.publishedAt,
    image: it.image || null,
    country: it.country || null,
    topic: it.topic,
  };
}

/** Cache file se aaya item dobara jaancho (file haath se badli ja sakti hai). */
function reviveCachedItem(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const url = safeHttpsUrl(raw.url);
  if (!url || url !== raw.url) return null;
  const parsed = new URL(url);
  const domain = outletFor(parsed.hostname);
  if (!domain || NON_ARTICLE_PATH.test(parsed.pathname)) return null;
  const publishedAt = Number(raw.publishedAt);
  if (!Number.isFinite(publishedAt) || publishedAt <= 0) return null;
  const source = OUTLETS[domain];
  const title = clampTitle(cleanText(raw.title));
  if (title.length < TITLE_MIN) return null;
  return {
    id: hashId(url),
    title,
    url,
    source,
    domain,
    publishedAt,
    image: isThumbDataUrl(raw.image) ? raw.image : null,
    country: clip(cleanText(raw.country), 60) || null,
    topic: TOPICS.has(raw.topic) ? raw.topic : 'world',
  };
}

async function renameWithRetry(from, to) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      await fsp.rename(from, to);
      return;
    } catch (err) {
      // Windows: antivirus / indexer file ko pal bhar pakad leta hai.
      if (attempt >= 3 || !['EPERM', 'EBUSY', 'EACCES'].includes(err && err.code)) throw err;
      await delay(60 * (attempt + 1));
    }
  }
}

// ── Service ──────────────────────────────────────────────────────

/**
 * @param {object} opts
 * @param {Function} opts.fetchImpl  (url, init) => Promise<Response>
 * @param {string}   [opts.cacheFile]  JSON cache ka raasta (na ho to sirf memory)
 * @param {Function} [opts.now]
 * @param {Function} [opts.makeThumbnail]  (Buffer) => data URL | null (sync ya async)
 * @param {object}   [opts.logger]
 * @param {Function} [opts.sleep]  (ms) => Promise — test ke liye
 * @param {object}   [opts.timings]  DEFAULTS mein se koi bhi badlo — test ke liye
 */
function createWorldFeed({
  fetchImpl,
  cacheFile = null,
  now = Date.now,
  makeThumbnail = null,
  logger,
  sleep = delay,
  timings = {},
} = {}) {
  if (typeof fetchImpl !== 'function') throw new TypeError('createWorldFeed: fetchImpl is required');
  const cfg = { ...DEFAULTS };
  for (const [k, v] of Object.entries(timings || {})) {
    if (k in DEFAULTS && Number.isFinite(v) && v >= 0) cfg[k] = v;
  }
  const log = makeLogger(logger);
  const listeners = new Set();
  const queries = QUERIES.map((q) => ({ ...q, url: buildQueryUrl(q.domains) }));

  const state = {
    items: [],
    fetchedAt: null,
    lastAttemptAt: 0,
    nextRefreshAt: 0,
    lastError: null,
  };
  let loadPromise = null;
  let inFlight = null;
  let itemsReady = null; // pehli khabrein tayyar (thumbnails se pehle)
  let writeChain = Promise.resolve();

  // ── Cache file ──

  async function loadCache() {
    if (!cacheFile) return;
    let text;
    try {
      text = await fsp.readFile(cacheFile, 'utf8');
    } catch (err) {
      if (err && err.code !== 'ENOENT') log.warn('cache read failed:', err.message);
      return;
    }
    try {
      const data = JSON.parse(text);
      if (!data || typeof data !== 'object' || data.version !== CACHE_VERSION || !Array.isArray(data.items)) {
        throw new Error('unexpected shape');
      }
      const items = data.items.slice(0, MAX_ITEMS).map(reviveCachedItem).filter(Boolean);
      const num = (v) => (Number.isFinite(v) && v > 0 ? v : 0);
      state.items = items;
      state.fetchedAt = num(data.fetchedAt) || null;
      state.lastAttemptAt = num(data.lastAttemptAt);
      state.nextRefreshAt = Math.min(num(data.nextRefreshAt), now() + cfg.ttlMs);
      state.lastError = typeof data.lastError === 'string' && /^[a-z-]{2,24}$/.test(data.lastError) ? data.lastError : null;
    } catch (err) {
      log.warn('cache file ignored (corrupt):', err.message);
    }
  }

  const ensureLoaded = () => {
    if (!loadPromise) loadPromise = loadCache().catch(noop);
    return loadPromise;
  };

  function persist() {
    if (!cacheFile) return Promise.resolve();
    const body = JSON.stringify({
      version: CACHE_VERSION,
      fetchedAt: state.fetchedAt,
      lastAttemptAt: state.lastAttemptAt,
      nextRefreshAt: state.nextRefreshAt,
      lastError: state.lastError,
      items: state.items.map(publicItem),
    });
    const write = async () => {
      const tmp = `${cacheFile}.${process.pid}.${crypto.randomBytes(4).toString('hex')}.tmp`;
      try {
        await fsp.mkdir(path.dirname(cacheFile), { recursive: true });
        await fsp.writeFile(tmp, body, 'utf8');
        await renameWithRetry(tmp, cacheFile);
      } catch (err) {
        log.warn('cache write failed:', err && err.message);
        try { await fsp.unlink(tmp); } catch { /* shayad bani hi nahi */ }
      }
    };
    writeChain = writeChain.then(write, write);
    return writeChain;
  }

  // ── Network ──

  /** Ek GDELT request. Error par `transient: true` = dobara koshish ke laiq. */
  async function fetchQueryOnce(query) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), cfg.requestTimeoutMs);
    const fail = (message, code, transient) => Object.assign(new Error(message), { code, transient });
    try {
      let res;
      try {
        res = await fetchImpl(query.url, {
          method: 'GET',
          headers: { 'User-Agent': USER_AGENT, Accept: 'application/json' },
          credentials: 'omit',
          signal: controller.signal,
        });
      } catch (err) {
        // Network / connect timeout: haan. Hamara apna 25 s timeout: nahi.
        if (err && typeof err === 'object' && !controller.signal.aborted) err.transient = true;
        throw err;
      }
      if (res.status === 429) {
        discardBody(res);
        throw fail('GDELT rate limit (429)', 'rate-limited', true);
      }
      if (!res.ok) {
        discardBody(res);
        throw fail(`GDELT HTTP ${res.status}`, 'http-error', res.status >= 500);
      }
      const buf = await readCapped(res, API_MAX_BYTES);
      return parseGdeltBody(buf.toString('utf8'));
    } finally {
      clearTimeout(timer);
    }
  }

  // 429 / 5xx / network: ruk kar dobara (10 s, phir 20 s). Live: GDELT ne
  // 12–15 s ke fasle par bhi aksar 429 diya, aur kabhi connect hi nahi hua.
  async function fetchQuery(query) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await fetchQueryOnce(query);
      } catch (err) {
        if (attempt >= TRANSIENT_RETRIES || !(err && err.transient)) throw err;
        await sleep(cfg.retryDelayMs * (attempt + 1));
      }
    }
  }

  /**
   * Image bytes (ya null). Redirect khud follow — har hop dobara jaancha.
   * Electron ka net.fetch `redirect: 'manual'` par 3xx lautane ki jagah
   * reject karta hai ("Redirect was cancelled") → wahan redirect wali image
   * bas null. Jaan bujh kar: bina jaanch ke kisi aur host par hop nahi.
   */
  async function fetchImage(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), cfg.thumbTimeoutMs);
    try {
      let current = url;
      for (let hop = 0; hop <= MAX_REDIRECTS; hop += 1) {
        if (!isSafeImageUrl(current)) return null;
        const res = await fetchImpl(current, {
          method: 'GET',
          // nativeImage sirf JPEG/PNG samajhta hai: WebP/AVIF mat bhejo.
          headers: { 'User-Agent': USER_AGENT, Accept: 'image/jpeg,image/png;q=0.9,image/*;q=0.1' },
          credentials: 'omit',
          redirect: 'manual',
          signal: controller.signal,
        });
        if (res.status >= 300 && res.status < 400) {
          const location = res.headers.get('location');
          discardBody(res);
          if (!location) return null;
          try { current = new URL(location, current).toString(); } catch { return null; }
          continue;
        }
        if (!res.ok) { discardBody(res); return null; }
        const type = String(res.headers.get('content-type') || '').toLowerCase();
        if (!type.startsWith('image/')) { discardBody(res); return null; }
        const declared = Number(res.headers.get('content-length'));
        if (Number.isFinite(declared) && declared > THUMB_MAX_BYTES) { discardBody(res); return null; }
        return await readCapped(res, THUMB_MAX_BYTES);
      }
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  async function thumbnailFor(imageUrl) {
    if (typeof makeThumbnail !== 'function' || !imageUrl) return null;
    try {
      const buf = await fetchImage(imageUrl);
      if (!buf) return null;
      const info = imageInfo(buf);
      if (!info || Math.min(info.width, info.height) < THUMB_MIN_SIDE || info.width * info.height > THUMB_MAX_PIXELS) return null;
      const out = await makeThumbnail(buf);
      return isThumbDataUrl(out) ? out : null;
    } catch {
      return null; // thumbnail na bane to bas `image: null`
    }
  }

  async function addThumbnails(items) {
    const queue = items.slice(0, THUMB_COUNT).filter((it) => !it.image && it.imageUrl);
    let next = 0;
    const worker = async () => {
      while (next < queue.length) {
        const it = queue[next];
        next += 1;
        it.image = await thumbnailFor(it.imageUrl);
      }
    };
    await Promise.all(Array.from({ length: Math.min(THUMB_CONCURRENCY, queue.length) }, worker));
  }

  // ── Refresh ──

  const signature = () => state.items.map((it) => `${it.id}:${it.image ? 1 : 0}`).join('|');

  async function doRefresh(markItemsReady) {
    await ensureLoaded();
    const before = signature();
    state.lastAttemptAt = now();

    const results = [];
    for (let i = 0; i < queries.length; i += 1) {
      if (i > 0) await sleep(cfg.queryGapMs);
      const q = queries[i];
      try {
        results.push({ topic: q.topic, ok: true, articles: await fetchQuery(q) });
      } catch (err) {
        const cause = err && err.cause ? ` (${clip(err.cause.code || err.cause.message, 60)})` : '';
        log.warn(`${q.topic} query failed:`, `${clip(err && err.message, 160)}${cause}`);
        results.push({ topic: q.topic, ok: false, error: errorCode(err) });
      }
      // Pehli dafa (cache khali): GDELT har query par ~11 s leta hai, to
      // pehli query ki khabrein foran dikhao; baqi + thumbnails baad mein
      // (`world-feed:updated` se).
      const last = results[results.length - 1];
      if (i < queries.length - 1 && !state.items.length && last.ok) {
        const early = selectItems(last.articles.map((raw) => normalizeArticle(raw, last.topic, now())).filter(Boolean), now());
        if (early.length) {
          state.items = early.map(publicItem);
          state.fetchedAt = now();
          state.lastError = null;
          markItemsReady();
        }
      }
    }

    const t = now();
    const okCount = results.filter((r) => r.ok).length;
    const candidates = [];
    for (const r of results) {
      if (!r.ok) continue;
      for (const raw of r.articles) {
        const it = normalizeArticle(raw, r.topic, t);
        if (it) candidates.push(it);
      }
    }
    // Ek query fail, doosri chal gayi: fail wale topic ki purani (taaza)
    // khabrein rakh lo, taake card aadha khali na ho.
    const failedTopics = new Set(results.filter((r) => !r.ok).map((r) => r.topic));
    if (okCount > 0 && failedTopics.size) {
      for (const old of state.items) {
        if (failedTopics.has(old.topic) && t - old.publishedAt < CARRY_OVER_MS) candidates.push({ ...old, imageUrl: null });
      }
    }

    const selected = okCount > 0 ? selectItems(candidates, t) : [];
    if (!selected.length) {
      state.lastError = (results.find((r) => !r.ok) || {}).error || 'no-headlines';
      state.nextRefreshAt = t + cfg.backoffMs;
      await persist();
      return signature() !== before;
    }

    // Pehle wali thumbnails dobara download na hon.
    const oldImages = new Map(state.items.filter((it) => it.image).map((it) => [it.id, it.image]));
    for (const it of selected) if (!it.image && oldImages.has(it.id)) it.image = oldImages.get(it.id);

    state.items = selected.map(publicItem);
    state.fetchedAt = t;
    state.lastError = null;
    // Aadhi kamyabi: jaldi dobara koshish (backoff), poori: TTL.
    state.nextRefreshAt = failedTopics.size ? t + cfg.backoffMs : t + cfg.ttlMs;
    markItemsReady();

    await addThumbnails(selected);
    state.items = selected.map(publicItem);
    await persist();
    log.info(`refreshed: ${state.items.length} items, ${state.items.filter((it) => it.image).length} thumbnails${failedTopics.size ? ' (partial)' : ''}`);
    return signature() !== before;
  }

  function snapshot() {
    const t = now();
    const hasItems = state.items.length > 0;
    const stale = Boolean(state.lastError) || (hasItems && (!state.fetchedAt || t - state.fetchedAt >= cfg.ttlMs));
    const out = {
      ok: true,
      items: state.items.map(publicItem),
      fetchedAt: state.fetchedAt,
      stale,
      refreshing: Boolean(inFlight),
      attribution: ATTRIBUTION,
      attributionUrl: ATTRIBUTION_URL,
    };
    if (state.lastError) out.error = state.lastError;
    return out;
  }

  function emit(payload) {
    for (const fn of listeners) {
      try { fn(payload); } catch (err) { log.warn('listener failed:', err && err.message); }
    }
  }

  /** Ek hi refresh: chal raha ho to wohi promise. Kabhi reject nahi hota. */
  function refresh() {
    if (inFlight) return inFlight;
    let resolveReady;
    itemsReady = new Promise((resolve) => { resolveReady = resolve; });
    const run = doRefresh(() => resolveReady()).catch((err) => {
      log.warn('refresh crashed:', err && err.message);
      state.lastError = 'internal';
      state.nextRefreshAt = now() + cfg.backoffMs;
      return true;
    });
    inFlight = run.then((changed) => {
      resolveReady();
      inFlight = null;
      const payload = { ...snapshot(), changed: Boolean(changed) };
      emit(payload);
      return payload;
    });
    return inFlight;
  }

  async function waitForFirstItems(ms) {
    let timer;
    const timeout = new Promise((resolve) => { timer = setTimeout(resolve, ms); });
    try {
      await Promise.race([itemsReady, inFlight, timeout].filter(Boolean));
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Foran cache se jawab. Cache khali → pehli khabron ka intezar (max
   * ~20 s). Purana → peeche refresh, abhi purana data + `refreshing: true`.
   * `force` → refresh, magar pichli koshish 60 s se kam pehle na ho.
   */
  async function get({ force = false } = {}) {
    await ensureLoaded();
    const t = now();
    // Ghari peeche chali gayi (time zone / BIOS) to intezar mat karo.
    const clockWentBack = state.lastAttemptAt > 0 && t < state.lastAttemptAt - cfg.forceMinGapMs;
    const due = t >= state.nextRefreshAt || clockWentBack;
    const forceAllowed = Boolean(force) && (clockWentBack || t - state.lastAttemptAt >= cfg.forceMinGapMs);
    if (!inFlight && (due || forceAllowed)) refresh();
    if (!state.items.length && inFlight) await waitForFirstItems(cfg.firstLoadTimeoutMs);
    return snapshot();
  }

  /** Sirf wohi https link jo is waqt cache mein hai (renderer koi aur URL na khulwa sake). */
  function open(url) {
    if (typeof url !== 'string' || !url || url.length > URL_MAX) return false;
    if (safeHttpsUrl(url) !== url) return false;
    return state.items.some((it) => it.url === url);
  }

  function onUpdate(fn) {
    if (typeof fn !== 'function') return noop;
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  return {
    get,
    open,
    onUpdate,
    _refresh: refresh,
    _state: () => ({ ...state, items: state.items.map(publicItem) }),
    _whenIdle: () => Promise.all([inFlight, writeChain].filter(Boolean)),
  };
}

module.exports = {
  createWorldFeed,
  ATTRIBUTION,
  ATTRIBUTION_URL,
  USER_AGENT,
  QUERIES,
  OUTLETS,
  _internals: {
    buildQuery,
    buildQueryUrl,
    QUERY_MAX_CHARS,
    cleanTitle,
    cleanText,
    parseSeenDate,
    parseGdeltBody,
    normalizeArticle,
    selectItems,
    imageInfo,
    safeHttpsUrl,
    isSafeImageUrl,
    urlKey,
    titleKey,
    DEFAULTS,
  },
};
