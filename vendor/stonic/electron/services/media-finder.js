'use strict';
// ═══════════════════════════════════════════════════════════════
//  Media finder — gaane / videos dhoondna (media-browser.js user ke browser mein kholta hai)
//
//  `play_media` tool (dono voice + chat) ka main-process wala hissa. Teen kaam:
//
//    1. YouTube search   results page ka `ytInitialData` parse — title, channel,
//                        duration, views, kitna purana. Koi API key / kharcha
//                        nahi (naapa: 5 videos ~1.2 s).
//    2. "Up next"        watch page ki related list — gaana chalao to side mein
//                        USI qisam ke gaane aayein, ek hi gaane ke 4 version nahi.
//    3. Links            YouTube / TikTok / Instagram / Facebook / Vimeo /
//                        Dailymotion ka link → us platform ka OFFICIAL embed.
//
//  TikTok / Instagram ki search: DuckDuckGo (`site:tiktok.com …`) — un ki apni
//  search login maangti hai. Video download / stream nikaalna (yt-dlp waghera)
//  jaan bujh kar NAHI: platforms ki shartein, aur bechne wali app ke liye khatra.
//
//  `fetchImpl` bahar se aata hai (main.js mein Electron ka `net.fetch` — system
//  proxy maanta hai); test naqli fetch dete hain.
// ═══════════════════════════════════════════════════════════════

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36';
const YT = 'https://www.youtube.com';
const PAGE_HEADERS = {
  'User-Agent': UA,
  'Accept-Language': 'en-US,en;q=0.9',
  // EU mein YouTube pehle consent page deta hai; ye cookie seedha results deti hai.
  Cookie: 'CONSENT=YES+1; SOCS=CAI',
};

// YouTube ke apne filter (unke UI ke URL jaise hi, do dafa encode).
const UPLOADED_THIS_WEEK = 'EgIIAw%253D%253D';
const UPLOADED_THIS_MONTH = 'EgIIBA%253D%253D';

const SEARCH_TIMEOUT_MS = 9000;
const RELATED_TIMEOUT_MS = 6000;
const OEMBED_TIMEOUT_MS = 4000;

/** Player mein zyada se zyada itni videos (1 chalti + baqi list / reserve). */
const MAX_ITEMS = 10;

const YT_ID = /^[\w-]{11}$/;

// ── Chhote madadgar ──────────────────────────────────────────────

const text = (node) => {
  if (!node) return '';
  if (typeof node === 'string') return node;
  if (typeof node.simpleText === 'string') return node.simpleText;
  if (typeof node.content === 'string') return node.content;
  if (Array.isArray(node.runs)) return node.runs.map((r) => r?.text || '').join('');
  return '';
};

const clean = (s, max = 200) => String(s || '').replace(/\s+/g, ' ').trim().slice(0, max);

/** "1:02:03" / "4:58" → seconds. Na samjhe to 0. */
function durationSeconds(label) {
  const m = String(label || '').trim().match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
  if (!m) return 0;
  return m[3] !== undefined ? Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]) : Number(m[1]) * 60 + Number(m[2]);
}

const ytThumb = (id) => `https://i.ytimg.com/vi/${id}/mqdefault.jpg`;
const ytWatch = (id) => `${YT}/watch?v=${id}`;

function ytItem(fields) {
  const id = fields.id;
  return {
    key: `youtube:${id}`,
    platform: 'youtube',
    id,
    title: clean(fields.title) || 'YouTube video',
    channel: clean(fields.channel, 80),
    duration: fields.duration || '',
    durationSec: durationSeconds(fields.duration),
    views: clean(fields.views, 40),
    published: clean(fields.published, 40),
    live: Boolean(fields.live),
    verified: Boolean(fields.verified),
    artist: Boolean(fields.artist),
    thumbnail: ytThumb(id),
    pageUrl: ytWatch(id),
  };
}

// ── ytInitialData nikaalna ───────────────────────────────────────
//
// Regex se `;</script>` tak kaatna kabhi kabhi title ke andar ke `};` par toot
// jata hai. Is liye braces gin kar poora JSON — strings aur escapes ka khayal
// rakh kar.

const INITIAL_DATA_MARKERS = ['var ytInitialData = ', 'window["ytInitialData"] = ', 'ytInitialData = '];

function extractJsonObject(src, start) {
  if (src[start] !== '{') return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < src.length; i += 1) {
    const ch = src[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') inString = true;
    else if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  return null;
}

function parseInitialData(html) {
  const src = String(html || '');
  for (const marker of INITIAL_DATA_MARKERS) {
    const at = src.indexOf(marker);
    if (at < 0) continue;
    let start = at + marker.length;
    while (src[start] === ' ') start += 1;
    // Kabhi string ke andar JSON hota hai: ytInitialData = '{\x22…'. Wo shakl hum nahi parhte.
    const json = extractJsonObject(src, start);
    if (!json) continue;
    try {
      return JSON.parse(json);
    } catch {
      /* agla marker */
    }
  }
  return null;
}

// ── Videos ikatthi karna (search + related, dono shaklein) ───────
//
// YouTube do shaklein deta hai: purana `videoRenderer` / `compactVideoRenderer`
// aur naya `lockupViewModel` (2025 se related list mein). Ads, Shorts,
// playlists, channels — sab chhod diye jate hain.

const SKIP_KEYS = /^(ad|promoted)|Ad(Slot|Layout)|Pyv|reelShelf|shortsLockup|reelItem|playlistRenderer|radioRenderer|channelRenderer|movieRenderer/i;

function badgeFlags(badges) {
  let verified = false;
  let artist = false;
  let live = false;
  for (const b of Array.isArray(badges) ? badges : []) {
    const style = String(b?.metadataBadgeRenderer?.style || '');
    if (style.includes('VERIFIED_ARTIST')) artist = true;
    if (style.includes('VERIFIED')) verified = true;
    if (style.includes('LIVE')) live = true;
  }
  return { verified, artist, live };
}

function fromVideoRenderer(v) {
  if (!v || !YT_ID.test(String(v.videoId || ''))) return null;
  const owner = badgeFlags(v.ownerBadges);
  const badges = badgeFlags(v.badges);
  const overlays = JSON.stringify(v.thumbnailOverlays || []);
  const duration = text(v.lengthText);
  const views = text(v.shortViewCountText) || text(v.viewCountText);
  const live = badges.live || /"style":"LIVE"/.test(overlays) || (!duration && /watching/i.test(views));
  return ytItem({
    id: v.videoId,
    title: text(v.title),
    channel: text(v.ownerText) || text(v.longBylineText) || text(v.shortBylineText),
    duration,
    views,
    published: text(v.publishedTimeText),
    live,
    verified: owner.verified,
    artist: owner.artist,
  });
}

function fromLockup(l) {
  if (!l || String(l.contentType || '') !== 'LOCKUP_CONTENT_TYPE_VIDEO') return null;
  if (!YT_ID.test(String(l.contentId || ''))) return null;
  const meta = l.metadata?.lockupMetadataViewModel;
  const rows = meta?.metadata?.contentMetadataViewModel?.metadataRows || [];
  const parts = rows.map((r) => (r?.metadataParts || []).map((p) => text(p?.text)).filter(Boolean));
  const flat = parts.flat();
  const image = JSON.stringify(l.contentImage || {});
  const duration = (image.match(/"text":"(\d{1,2}:\d{2}(?::\d{2})?)"/) || [])[1] || '';
  const live = /"text":"LIVE"/.test(image) || flat.some((p) => /watching/i.test(p));
  return ytItem({
    id: l.contentId,
    title: text(meta?.title),
    channel: parts[0]?.[0] || '',
    duration,
    views: flat.find((p) => /views?|watching/i.test(p)) || '',
    published: flat.find((p) => /\bago\b|streamed/i.test(p)) || '',
    live,
    verified: /VERIFIED/.test(JSON.stringify(meta?.metadata || {})),
  });
}

function collectVideos(root, limit = 30) {
  const out = [];
  const seen = new Set();
  const visit = (node, depth) => {
    if (out.length >= limit || !node || typeof node !== 'object' || depth > 60) return;
    if (Array.isArray(node)) {
      for (const child of node) visit(child, depth + 1);
      return;
    }
    let item = null;
    if (node.videoRenderer) item = fromVideoRenderer(node.videoRenderer);
    else if (node.compactVideoRenderer) item = fromVideoRenderer(node.compactVideoRenderer);
    else if (node.lockupViewModel) item = fromLockup(node.lockupViewModel);
    if (item || node.videoRenderer || node.compactVideoRenderer || node.lockupViewModel) {
      if (item && !seen.has(item.id)) {
        seen.add(item.id);
        out.push(item);
      }
      return;
    }
    for (const key of Object.keys(node)) {
      if (SKIP_KEYS.test(key)) continue;
      visit(node[key], depth + 1);
    }
  };
  visit(root, 0);
  return out;
}

// ── "Sahi" gaana chunna ──────────────────────────────────────────
//
// YouTube ki apni tarteeb zyada tar theek hoti hai — hum us ke upar sirf wo
// cheezein neeche karte hain jo user ne nahi maangi: cover, slowed, live
// stream, ghante bhar ki compilation. Official artist channel thora upar.

const STOP_WORDS = new Set([
  'the', 'a', 'an', 'by', 'of', 'and', 'ka', 'ki', 'ke', 'ko', 'se', 'wala', 'wali',
  'song', 'songs', 'gaana', 'gana', 'video', 'play', 'chalao', 'please', 'mujhe', 'mere', 'liye',
]);
const VARIANTS = [
  'cover', 'karaoke', 'reaction', 'slowed', 'reverb', 'lofi', 'lo fi', 'nightcore', '8d', 'instrumental',
  'ringtone', 'status', 'remix', 'mashup', 'sped up', 'tutorial', 'piano', 'guitar', 'dance', 'shorts',
];
const LONG_OK = /\b(mix|playlist|jukebox|nonstop|non stop|mashup|album|podcast|episode|movie|lecture|live|compilation|hours?|ghante|naat|qawwali|bayan|dars|interview|documentary|full)\b/;

const normalize = (s) => String(s || '')
  .toLowerCase()
  .normalize('NFKD')
  .replace(/[\u0300-\u036f]/g, '')
  .replace(/[^\p{L}\p{N}\s]/gu, ' ')
  .replace(/\s+/g, ' ')
  .trim();

function rankForPlay(query, items) {
  const q = normalize(query);
  const tokens = q.split(' ').filter((t) => t.length > 1 && !STOP_WORDS.has(t));
  const scored = items.map((item, idx) => {
    const title = normalize(item.title);
    const hay = `${title} ${normalize(item.channel)}`;
    let score = 100 - idx * 5;
    if (tokens.length) {
      const hits = tokens.filter((t) => hay.includes(t)).length;
      score += (hits / tokens.length) * 30;
    }
    if (VARIANTS.some((v) => title.includes(v) && !q.includes(v))) score -= 35;
    if (item.live && !/\blive\b/.test(q)) score -= 60;
    if (item.durationSec > 15 * 60 && !LONG_OK.test(q)) score -= 30;
    if (item.durationSec > 0 && item.durationSec < 60) score -= 20;
    if (item.artist) score += 12;
    else if (item.verified) score += 5;
    if (/\bofficial\b/.test(title) && !q.includes('official')) score += 6;
    return { item, score, idx };
  });
  scored.sort((a, b) => b.score - a.score || a.idx - b.idx);
  return scored.map((s) => s.item);
}

/** "Up next" list mein se wo jo player mein achhi lagti hain. */
function usableRelated(items, excludeIds) {
  return items.filter((item) => !excludeIds.has(item.id) && !item.live && !(item.durationSec > 0 && item.durationSec < 45));
}

// Unwan ke wo lafz jo kisi gaane ko doosre se juda nahi karte.
const GENERIC_WORDS = new Set([
  ...STOP_WORDS, 'official', 'video', 'videos', 'lyrical', 'lyric', 'lyrics', 'audio', 'full', 'hd', '4k', '8k', 'new',
  'latest', 'music', 'feat', 'ft', 'with', 'from', 'movie', 'film', 'version', 'remastered', 'remaster', 'visualiser',
  'visualizer', 'out', 'now', 'best', 'top', 'hits', 'for', 'you', 'your', 'this', 'that', 'in', 'on', 'to',
]);
const keyWords = (s) => normalize(s).split(' ').filter((t) => t.length > 2 && !GENERIC_WORDS.has(t) && !/^\d+$/.test(t));

/**
 * Gaane ka apna naam: unwan ka pehla hissa jo channel / artist ka naam na ho.
 *   "Kesariya - Brahmastra | Arijit"            (Sony Music India) → kesariya
 *   "Rick Astley - Never Gonna Give You Up"      (Rick Astley)      → never gonna give
 */
function songKeyWords(title, channel) {
  const channelWords = new Set(keyWords(channel));
  const segments = String(title || '').split(/\s[|\-–—:]\s|[|([]/).map(keyWords).filter((words) => words.length);
  for (const words of segments) {
    const own = words.filter((w) => !channelWords.has(w));
    if (own.length * 2 >= words.length && own.length) return words;
  }
  return [];
}

/**
 * Chalti video ke saath wali list, teen hisson mein (har hisse ke andar
 * YouTube ki apni tarteeb):
 *   1. milti-julti — wohi channel, ya wohi artist / film / mauzoo
 *   2. baqi — YouTube ki "Up next" (bina login) kabhi ilaqe ki trending bhi
 *      deti hai: Rick Astley ke neeche ARY ka drama, naapa hua
 *   3. usi gaane ke doosre version (lyrical, cover, slowed) — sab se aakhir.
 *      `versions`: wo IDs jo pehle se version maani jayein (gaane ki search ke
 *      baqi natije zyada tar usi gaane ke doosre upload hote hain).
 * Live stream, aur gaane ke baad ghante bhar ki compilation, bilkul nahi.
 */
function orderRelated(seed, pool, versions = new Set()) {
  const key = songKeyWords(seed.title, seed.channel);
  const keySet = new Set(key);
  const seedWords = new Set(keyWords(`${seed.title} ${seed.channel}`).filter((w) => !keySet.has(w)));
  const seedChannel = normalize(seed.channel);
  // Chhote gaane ke baad 14-15 minute ki "jukebox" bhi nahi (naapa: Kesariya ke
  // neeche do aisi aayin). Lambi video (vlog, lecture) ho to koi hadd nahi.
  const maxSec = seed.durationSec > 15 * 60 ? Infinity
    : seed.durationSec > 0 && seed.durationSec < 10 * 60 ? Math.max(12 * 60, seed.durationSec * 3)
      : 20 * 60;
  const seen = new Set([seed.id]);
  const groups = [[], [], []];
  for (const item of pool) {
    if (!item || seen.has(item.id)) continue;
    seen.add(item.id);
    if (item.live) continue;
    if (item.durationSec > maxSec) continue;
    const titleWords = new Set(keyWords(item.title));
    const isVersion = versions.has(item.id) || (key.length > 0 && key.every((w) => titleWords.has(w)));
    const sameChannel = Boolean(seedChannel) && normalize(item.channel) === seedChannel;
    const connected = sameChannel || keyWords(`${item.title} ${item.channel}`).some((w) => seedWords.has(w));
    groups[isVersion ? 2 : connected ? 0 : 1].push(item);
  }
  return groups.flat();
}

/** "Kya chal raha hai": pehle khabar ki videos; LIVE stream zyada se zyada ek, teesre number par. */
function orderLatest(items) {
  const recorded = items.filter((i) => !i.live);
  const live = items.filter((i) => i.live);
  return [...recorded.slice(0, 2), ...live.slice(0, 1), ...recorded.slice(2), ...live.slice(1)];
}

// ── Links ────────────────────────────────────────────────────────

const PLATFORM_HOSTS = {
  youtube: ['youtube.com', 'youtu.be', 'youtube-nocookie.com'],
  tiktok: ['tiktok.com'],
  instagram: ['instagram.com'],
  facebook: ['facebook.com', 'fb.watch'],
  vimeo: ['vimeo.com'],
  dailymotion: ['dailymotion.com', 'dai.ly'],
};

const hostMatches = (host, domains) => domains.some((d) => host === d || host.endsWith(`.${d}`));

function platformOf(host) {
  for (const [platform, domains] of Object.entries(PLATFORM_HOSTS)) {
    if (hostMatches(host, domains)) return platform;
  }
  return null;
}

/** Koi bhi link → { platform, id, pageUrl, embedUrl } ya null. */
function parseLink(raw) {
  let url;
  try {
    url = new URL(String(raw || '').trim());
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
  const host = url.hostname.toLowerCase().replace(/^www\.|^m\.|^music\./, '');
  const platform = platformOf(host);
  const path = url.pathname;
  if (platform === 'youtube') {
    let id = '';
    if (host === 'youtu.be') id = path.split('/')[1] || '';
    else id = url.searchParams.get('v') || (path.match(/^\/(?:embed|shorts|live|v)\/([\w-]{11})/) || [])[1] || '';
    if (!YT_ID.test(id)) return null;
    return { platform, id, pageUrl: ytWatch(id) };
  }
  if (platform === 'tiktok') {
    const id = (path.match(/\/video\/(\d{8,25})/) || path.match(/\/(?:embed\/v2|player\/v1)\/(\d{8,25})/) || [])[1];
    if (!id) return null;
    return {
      platform, id,
      pageUrl: `https://www.tiktok.com${path.split('?')[0]}`,
      embedUrl: `https://www.tiktok.com/player/v1/${id}?autoplay=1&rel=0&music_info=1&description=1`,
    };
  }
  if (platform === 'instagram') {
    const m = path.match(/^\/(reel|reels|p|tv)\/([\w-]{5,40})/);
    if (!m) return null;
    const kind = m[1] === 'reels' ? 'reel' : m[1];
    return {
      platform, id: m[2],
      pageUrl: `https://www.instagram.com/${kind}/${m[2]}/`,
      embedUrl: `https://www.instagram.com/${kind}/${m[2]}/embed/`,
    };
  }
  if (platform === 'facebook') {
    const isVideo = /\/videos\/|\/watch\/?|\/reel\/|\/share\/[vr]\//.test(path) || host === 'fb.watch';
    if (!isVideo) return null;
    const pageUrl = `https://${url.hostname}${path}${url.searchParams.get('v') ? `?v=${url.searchParams.get('v')}` : ''}`;
    return {
      platform, id: pageUrl,
      pageUrl,
      embedUrl: `https://www.facebook.com/plugins/video.php?href=${encodeURIComponent(pageUrl)}&show_text=false&autoplay=true&width=1280`,
    };
  }
  if (platform === 'vimeo') {
    const id = (path.match(/\/(?:video\/)?(\d{5,12})(?:\/|$)/) || [])[1];
    if (!id) return null;
    return { platform, id, pageUrl: `https://vimeo.com/${id}`, embedUrl: `https://player.vimeo.com/video/${id}?autoplay=1` };
  }
  if (platform === 'dailymotion') {
    const id = host === 'dai.ly' ? path.split('/')[1] : (path.match(/\/video\/([a-z0-9]{5,12})/i) || [])[1];
    if (!id) return null;
    return {
      platform, id,
      pageUrl: `https://www.dailymotion.com/video/${id}`,
      embedUrl: `https://www.dailymotion.com/embed/video/${id}?autoplay=1`,
    };
  }
  return null;
}

const PLATFORM_NAMES = {
  youtube: 'YouTube', tiktok: 'TikTok', instagram: 'Instagram', facebook: 'Facebook', vimeo: 'Vimeo', dailymotion: 'Dailymotion',
};

const HTML_NAMED = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&nbsp;': ' ', '&apos;': "'" };
const safeChar = (code) => (code > 0 && code < 0x110000 ? String.fromCodePoint(code) : '');

function decodeHtml(s) {
  return String(s || '')
    .replace(/&#x([0-9a-f]{1,6});/gi, (_, hex) => safeChar(parseInt(hex, 16)))
    .replace(/&#(\d{1,7});/g, (_, dec) => safeChar(Number(dec)))
    .replace(/&(?:amp|lt|gt|quot|nbsp|apos);/g, (m) => HTML_NAMED[m] || m);
}

/** "Arham on TikTok: caption … | TikTok" → "caption …". */
function socialTitle(raw) {
  const title = clean(raw, 300)
    .replace(/\s*[|\-–]\s*(TikTok|Instagram)\s*$/i, '')
    .replace(/^.{1,60}?\s+on\s+(TikTok|Instagram)\s*:\s*/i, '')
    .replace(/^["“](.*)["”]$/, '$1');
  return clean(title, 140);
}

function frameItem(link, extra = {}) {
  return {
    key: `${link.platform}:${link.id}`,
    platform: link.platform,
    id: link.id,
    title: clean(extra.title) || `${PLATFORM_NAMES[link.platform]} video`,
    channel: clean(extra.channel, 80),
    duration: '',
    durationSec: 0,
    views: '',
    published: '',
    live: false,
    verified: false,
    artist: false,
    thumbnail: extra.thumbnail || '',
    pageUrl: link.pageUrl,
    embedUrl: link.embedUrl,
  };
}

// ── Finder ───────────────────────────────────────────────────────

function createMediaFinder({ fetchImpl } = {}) {
  const doFetch = fetchImpl || globalThis.fetch;
  if (typeof doFetch !== 'function') throw new Error('media-finder: fetch nahi mila');

  async function getText(url, { timeoutMs = SEARCH_TIMEOUT_MS, headers = PAGE_HEADERS } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await doFetch(url, { headers, signal: ctrl.signal, redirect: 'follow' });
      if (!res.ok) {
        const err = new Error(`HTTP ${res.status}`);
        err.status = res.status;
        throw err;
      }
      return await res.text();
    } finally {
      clearTimeout(timer);
    }
  }

  async function getJson(url, timeoutMs = OEMBED_TIMEOUT_MS) {
    try {
      return JSON.parse(await getText(url, { timeoutMs, headers: { 'User-Agent': UA } }));
    } catch {
      return null;
    }
  }

  /** YouTube search. `filter` = YouTube ka `sp` (upload date waghera). */
  async function searchYouTube(query, { filter = '', limit = 30 } = {}) {
    const url = `${YT}/results?search_query=${encodeURIComponent(query)}&hl=en${filter ? `&sp=${filter}` : ''}`;
    const data = parseInitialData(await getText(url));
    if (!data) throw new Error('YouTube ka jawab samajh nahi aaya');
    const contents = data.contents?.twoColumnSearchResultsRenderer?.primaryContents || data.contents || data;
    return collectVideos(contents, limit);
  }

  /**
   * Watch page ki "Up next" list. `seed` (chalti video) ho to us se milti-julti
   * pehle. Nakaam ho to khali list — player phir bhi chalta hai.
   */
  async function related(id, { exclude = [], seed = null } = {}) {
    if (!YT_ID.test(String(id || ''))) return [];
    try {
      const html = await getText(`${ytWatch(id)}&hl=en`, { timeoutMs: RELATED_TIMEOUT_MS });
      const data = parseInitialData(html);
      const secondary = data?.contents?.twoColumnWatchNextResults?.secondaryResults;
      if (!secondary) return [];
      const list = usableRelated(collectVideos(secondary, 30), new Set([id, ...exclude]));
      return seed ? orderRelated({ id, title: '', channel: '', durationSec: 0, ...seed }, list) : list;
    } catch {
      return [];
    }
  }

  /**
   * Chalti video ke saath ki list: "Up next" + `extra`, milti-julti pehle.
   * `extraAreVersions`: extra = isi gaane ki search ke baqi natije (version).
   */
  async function listFor(seed, extra = [], { extraAreVersions = false } = {}) {
    const upNext = await related(seed.id);
    const versions = extraAreVersions ? new Set(extra.map((item) => item.id)) : undefined;
    return orderRelated(seed, [...upNext, ...extra], versions).slice(0, MAX_ITEMS - 1);
  }

  /** Ek YouTube video ka naam/channel (link wale raaste ke liye). */
  async function youtubeDetails(id) {
    const info = await getJson(`${YT}/oembed?format=json&url=${encodeURIComponent(ytWatch(id))}`);
    return ytItem({ id, title: info?.title || '', channel: info?.author_name || '' });
  }

  // TikTok ka oEmbed ab naam / tasveer nahi deta (Oct 2026 naapa) — naam search
  // ke natije se aata hai, channel link ke `@naam` se.
  async function frameDetails(link, extra = {}) {
    if (link.platform === 'tiktok') {
      const handle = (link.pageUrl.match(/\/@([\w.-]+)\//) || [])[1];
      return frameItem(link, { title: extra.title, channel: handle ? `@${handle}` : '' });
    }
    if (link.platform === 'instagram') {
      return frameItem(link, { title: extra.title, channel: extra.channel });
    }
    if (link.platform === 'vimeo') {
      const info = await getJson(`https://vimeo.com/api/oembed.json?url=${encodeURIComponent(link.pageUrl)}`);
      return frameItem(link, { title: info?.title, channel: info?.author_name, thumbnail: info?.thumbnail_url });
    }
    if (link.platform === 'dailymotion') {
      return frameItem(link, { thumbnail: `https://www.dailymotion.com/thumbnail/video/${link.id}` });
    }
    return frameItem(link);
  }

  /** TikTok / Instagram: unki apni search login maangti hai — DuckDuckGo `site:` se links. */
  async function searchSocial(platform, query) {
    const site = platform === 'tiktok' ? 'tiktok.com' : 'instagram.com/reel';
    const html = await getText(`https://html.duckduckgo.com/html/?q=${encodeURIComponent(`site:${site} ${query}`)}`, {
      headers: { 'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9' },
    });
    const pattern = platform === 'tiktok'
      ? /https?:\/\/(?:www\.)?tiktok\.com\/@[\w.-]+\/video\/\d+/
      : /https?:\/\/(?:www\.)?instagram\.com\/(?:reel|p)\/[\w-]+/;
    // Har natija: <a class="result__a" href="…uddg=<link>…">unwan</a>
    const found = [];
    const anchor = /<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/g;
    for (let m = anchor.exec(html); m; m = anchor.exec(html)) {
      const href = decodeHtml(m[1]);
      let target = href;
      try {
        target = new URL(href, 'https://duckduckgo.com').searchParams.get('uddg') || href;
      } catch { /* jaisa hai */ }
      const link = (target.match(pattern) || [])[0];
      if (link) found.push({ url: link, title: socialTitle(decodeHtml(m[2].replace(/<[^>]+>/g, ''))) });
    }
    const unique = [];
    const seen = new Set();
    for (const { url, title } of found) {
      const link = parseLink(url);
      if (!link || seen.has(link.id)) continue;
      seen.add(link.id);
      unique.push(await frameDetails(link, { title }));
      if (unique.length >= 6) break;
    }
    return unique;
  }

  /**
   * Asal kaam. Jawab:
   *   { items, mode, platform, notice?, related?: Promise<items> }
   * `related` sirf 'play' mode mein — pehli video foran chalti hai, list baad mein aati hai.
   */
  async function find(request = {}) {
    const query = clean(request.query, 200);
    const mode = request.mode === 'latest' ? 'latest' : 'play';
    const platform = ['tiktok', 'instagram'].includes(request.platform) ? request.platform : 'youtube';
    // `related: false` = sirf chalne wali video chahiye, saath ki list nahi (kam requests).
    const wantRelated = request.related !== false;

    // 1. Link diya hai — wohi chalao.
    if (request.url) {
      const link = parseLink(request.url);
      if (!link) {
        const err = new Error('This link is not a video link Stonic can play (YouTube, TikTok, Instagram, Facebook, Vimeo, Dailymotion).');
        err.code = 'UNSUPPORTED_LINK';
        throw err;
      }
      if (link.platform === 'youtube') {
        const first = await youtubeDetails(link.id);
        if (!wantRelated) return { items: [first], mode: 'play', platform: 'youtube' };
        // Sirf "Up next" kabhi ilaqe ki trending deti hai — usi channel / artist ki
        // videos saath mila kar milti-julti pehle.
        const extra = first.channel ? searchYouTube(first.channel).catch(() => []) : Promise.resolve([]);
        const list = extra.then((more) => listFor(first, more));
        return { items: [first], mode: 'play', platform: 'youtube', related: list };
      }
      const first = await frameDetails(link);
      const rest = query ? rankForPlay(query, await searchYouTube(query).catch(() => [])).slice(0, MAX_ITEMS - 1) : [];
      return { items: [first, ...rest], mode: 'play', platform: link.platform };
    }

    if (!query) {
      const err = new Error('Tell me what to play — a song, a video, or a person or topic.');
      err.code = 'EMPTY_QUERY';
      throw err;
    }

    // 2. TikTok / Instagram khaas taur par maanga.
    if (platform !== 'youtube') {
      const social = await searchSocial(platform, query).catch(() => []);
      if (social.length) return { items: social.slice(0, MAX_ITEMS), mode, platform };
      // Wahan kuch na mila — khaali haath lautne se behtar YouTube.
      const fallback = await find({ query, mode, related: request.related });
      return { ...fallback, notice: `${PLATFORM_NAMES[platform]} search gave nothing, so these are from YouTube.` };
    }

    // 3. "Kya chal raha hai" — is hafte ki videos (kam milein to is mahine, phir koi bhi).
    if (mode === 'latest') {
      let items = [];
      for (const filter of [UPLOADED_THIS_WEEK, UPLOADED_THIS_MONTH, '']) {
        const found = (await searchYouTube(query, { filter }).catch(() => []))
          .filter((item) => !(item.durationSec > 0 && item.durationSec < 45));
        const seen = new Set(items.map((i) => i.id));
        items = items.concat(found.filter((i) => !seen.has(i.id)));
        if (items.length >= 5) break;
      }
      if (!items.length) throw Object.assign(new Error(`No recent videos found about "${query}".`), { code: 'NOT_FOUND' });
      return { items: orderLatest(items).slice(0, MAX_ITEMS), mode, platform };
    }

    // 4. Gaana / video — sab se sahi wala; list us ki "Up next" + search ke baqi natije.
    const results = await searchYouTube(query);
    if (!results.length) throw Object.assign(new Error(`No video found for "${query}".`), { code: 'NOT_FOUND' });
    const ranked = rankForPlay(query, results);
    const best = ranked[0];
    if (!wantRelated) return { items: [best], mode, platform };
    return { items: [best], mode, platform, related: listFor(best, ranked.slice(1), { extraAreVersions: true }) };
  }

  return { find, related, searchYouTube, parseLink };
}

/** Player ke bahar khulne wale link (browser mein) — sirf in platforms ke. */
function isAllowedExternal(raw) {
  try {
    const url = new URL(String(raw || ''));
    if (url.protocol !== 'https:') return false;
    return Boolean(platformOf(url.hostname.toLowerCase().replace(/^www\.|^m\.|^music\./, '')));
  } catch {
    return false;
  }
}

/** Embed jo player window ke andar khul sakta hai (renderer ki taraf bhi yehi jaanch). */
const EMBED_ORIGINS = [
  'https://www.youtube.com',
  'https://www.tiktok.com',
  'https://www.instagram.com',
  'https://www.facebook.com',
  'https://player.vimeo.com',
  'https://www.dailymotion.com',
];

module.exports = {
  createMediaFinder,
  parseInitialData,
  collectVideos,
  rankForPlay,
  orderRelated,
  orderLatest,
  socialTitle,
  decodeHtml,
  parseLink,
  durationSeconds,
  isAllowedExternal,
  EMBED_ORIGINS,
  MAX_ITEMS,
  PLATFORM_NAMES,
};
