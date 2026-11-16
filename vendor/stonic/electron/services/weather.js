'use strict';

// Mausam — MET Norway (api.met.no) ka Locationforecast 2.0 "compact".
//
// Kyun ye: muft, koi key nahi, aur commercial use jaiz (CC BY 4.0 / NLOD —
// card par "Weather: MET Norway" likhna zaroori). Shartein jo yahan poori hoti
// hain:
//   - apni pehchan wala User-Agent (app + website)
//   - cache: `Expires` tak dobara nahi poochhte; phir `If-Modified-Since`
//   - coordinates 4 decimal se zyada nahi — hum 2 bhejte hain (~1 km, shehar
//     ki satah; ghar ka pata kabhi nahi)
//
// Renderer ko poora JSON nahi jata — sirf agle ~36 nuqte, chhoti shakl mein
// (src/lib/mind/weather.ts ka MetPoint).

const ENDPOINT = 'https://api.met.no/weatherapi/locationforecast/2.0/compact';
const USER_AGENT = 'StonicAI/3 (+https://stonicai.com)';
const TIMEOUT_MS = 15_000;
const MIN_TTL_MS = 10 * 60_000;
const DEFAULT_TTL_MS = 30 * 60_000;
const MAX_TTL_MS = 3 * 60 * 60_000;
/** Nakaami ke baad itni der dobara koshish nahi (MET par bojh nahi). */
const FAIL_BACKOFF_MS = 5 * 60_000;
/** 403 / 429 = MET ne roka — zyada der ruko. */
const BLOCKED_BACKOFF_MS = 30 * 60_000;
const MAX_POINTS = 36;
const MAX_PLACES = 8;

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const round2 = (n) => Math.round(n * 100) / 100;
const errorText = (err) => String((err && err.message) || err || 'unknown error').slice(0, 160);

/** lat/lon theek hon to 2 decimal par, warna null. */
function cleanCoords(lat, lon) {
  const la = Number(lat);
  const lo = Number(lon);
  if (!Number.isFinite(la) || !Number.isFinite(lo)) return null;
  if (la < -90 || la > 90 || lo < -180 || lo > 180) return null;
  return { lat: round2(la), lon: round2(lo) };
}

/** MET ka JSON → { updatedAt, series: MetPoint[] }. Kachra = null. */
function shapeForecast(json) {
  const props = json && json.properties;
  const list = props && Array.isArray(props.timeseries) ? props.timeseries : null;
  if (!list) return null;
  const series = [];
  for (const entry of list) {
    if (series.length >= MAX_POINTS) break;
    const t = Date.parse(entry && entry.time);
    const data = entry && entry.data;
    const d = data && data.instant && data.instant.details;
    if (!Number.isFinite(t) || !d) continue;
    const temp = num(d.air_temperature);
    if (temp === null) continue;
    const n1 = data.next_1_hours || null;
    const n6 = data.next_6_hours || null;
    const sym = (n1 && n1.summary && n1.summary.symbol_code) || (n6 && n6.summary && n6.summary.symbol_code) || '';
    series.push({
      t,
      temp,
      rh: num(d.relative_humidity),
      wind: num(d.wind_speed),
      dir: num(d.wind_from_direction),
      cloud: num(d.cloud_area_fraction),
      sym: String(sym).slice(0, 40),
      p1: n1 && n1.details ? num(n1.details.precipitation_amount) : null,
      p6: n6 && n6.details ? num(n6.details.precipitation_amount) : null,
    });
  }
  if (!series.length) return null;
  const updatedAt = Date.parse(props.meta && props.meta.updated_at);
  return { updatedAt: Number.isFinite(updatedAt) ? updatedAt : null, series };
}

/** `Expires` header se cache ki umar (hadd ke andar). */
function ttlFrom(headers, now) {
  const raw = headers && typeof headers.get === 'function' ? headers.get('expires') : null;
  const at = raw ? Date.parse(raw) : NaN;
  if (!Number.isFinite(at)) return DEFAULT_TTL_MS;
  return Math.max(MIN_TTL_MS, Math.min(MAX_TTL_MS, at - now));
}

/**
 * @param {object} opts
 * @param {Function} opts.fetchImpl  (url, init) => Promise<Response>
 * @param {Function} [opts.now]
 * @param {object}   [opts.logger]
 */
function createWeather({ fetchImpl, now = () => Date.now(), logger = console } = {}) {
  if (typeof fetchImpl !== 'function') throw new TypeError('createWeather: fetchImpl is required');
  /** key → { data, fetchedAt, expires, lastModified, retryAt } */
  const cache = new Map();
  const inflight = new Map();

  const answer = (entry, extra) => ({
    ok: true,
    updatedAt: entry.data.updatedAt,
    fetchedAt: entry.fetchedAt,
    expires: entry.expires,
    series: entry.data.series,
    ...extra,
  });

  function remember(key, entry) {
    cache.delete(key);
    cache.set(key, entry);
    while (cache.size > MAX_PLACES) cache.delete(cache.keys().next().value);
  }

  async function fetchFresh(key, coords, prev) {
    const at = now();
    const headers = { 'User-Agent': USER_AGENT, Accept: 'application/json' };
    if (prev && prev.lastModified) headers['If-Modified-Since'] = prev.lastModified;
    const url = `${ENDPOINT}?lat=${coords.lat}&lon=${coords.lon}`;
    let res;
    try {
      res = await fetchImpl(url, { headers, signal: AbortSignal.timeout(TIMEOUT_MS) });
    } catch (err) {
      return fail(key, prev, at, FAIL_BACKOFF_MS, err.name === 'TimeoutError' || err.name === 'AbortError' ? 'timeout' : 'network');
    }
    if (res.status === 304 && prev && prev.data) {
      const entry = { ...prev, expires: at + ttlFrom(res.headers, at), retryAt: 0 };
      remember(key, entry);
      return answer(entry, { cached: false });
    }
    if (res.status === 403 || res.status === 429) return fail(key, prev, at, BLOCKED_BACKOFF_MS, `http_${res.status}`);
    if (!res.ok) return fail(key, prev, at, FAIL_BACKOFF_MS, `http_${res.status}`);
    let data = null;
    try {
      data = shapeForecast(await res.json());
    } catch {
      data = null;
    }
    if (!data) return fail(key, prev, at, FAIL_BACKOFF_MS, 'bad_data');
    const entry = {
      data,
      fetchedAt: at,
      expires: at + ttlFrom(res.headers, at),
      lastModified: (res.headers && typeof res.headers.get === 'function' && res.headers.get('last-modified')) || '',
      retryAt: 0,
    };
    remember(key, entry);
    return answer(entry, { cached: false });
  }

  /** Nakaam: purana data ho to wohi (stale), warna ghalti. Dobara koshish baad mein. */
  function fail(key, prev, at, backoff, reason) {
    try { logger.warn && logger.warn(`[Weather] ${reason}`); } catch { /* */ }
    if (prev && prev.data) {
      const entry = { ...prev, retryAt: at + backoff };
      remember(key, entry);
      return answer(entry, { stale: true, error: reason });
    }
    remember(key, { data: null, fetchedAt: 0, expires: 0, lastModified: '', retryAt: at + backoff });
    return { ok: false, error: reason, retryAt: at + backoff };
  }

  async function get(lat, lon) {
    const coords = cleanCoords(lat, lon);
    if (!coords) return { ok: false, error: 'bad_coords' };
    const key = `${coords.lat},${coords.lon}`;
    const prev = cache.get(key) || null;
    const at = now();
    if (prev && prev.data && prev.expires > at) return answer(prev, { cached: true });
    if (prev && prev.retryAt > at) {
      return prev.data ? answer(prev, { stale: true }) : { ok: false, error: 'retry_later', retryAt: prev.retryAt };
    }
    if (inflight.has(key)) return inflight.get(key);
    const job = fetchFresh(key, coords, prev).catch((err) => ({ ok: false, error: errorText(err) }))
      .finally(() => inflight.delete(key));
    inflight.set(key, job);
    return job;
  }

  return { get };
}

module.exports = { createWeather, shapeForecast, cleanCoords, USER_AGENT, ENDPOINT };
