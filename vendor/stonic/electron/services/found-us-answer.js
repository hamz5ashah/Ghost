// ═══════════════════════════════════════════════════════════════════════════
// found-us-answer.js — "Where did you first hear about me?" ka jawab, bahar
// bhejne se pehle saaf karna
// ---------------------------------------------------------------------------
// Renderer (src/lib/onboarding/found-us.ts) jawab { source, note?, version,
// position } bhejta hai (sab ek hi screen par — koi doosra sawal nahi). Ye sirf shakl pakki karta hai taake faltu
// data network par na jaye; asal pehra website par hai (found-us-lib.cjs:
// option fehrist mein hai ya nahi, label server khud lagata hai).
// ═══════════════════════════════════════════════════════════════════════════

const ID_RE = /^[a-z0-9_]{1,30}$/;
const NOTE_MAX = 120;

const toInt = (v, min, max) => (Number.isInteger(v) && v >= min && v <= max ? v : undefined);

/** Saaf jawab, ya `undefined` (kuch ghalat ho to bheja hi nahi jata). */
function cleanFoundAnswer(raw) {
  if (!raw || typeof raw !== 'object') return undefined;
  const source = typeof raw.source === 'string' ? raw.source : '';
  if (!ID_RE.test(source)) return undefined;
  const out = { source };
  if (typeof raw.note === 'string') {
    const note = raw.note.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, NOTE_MAX);
    if (note) out.note = note;
  }
  const version = toInt(raw.version, 1, 100000);
  if (version !== undefined) out.version = version;
  const position = toInt(raw.position, 0, 40);
  if (position !== undefined) out.position = position;
  return out;
}

/**
 * Website ki /found-us.json — sirf itna ke JSON object ho aur had se bara na
 * ho. Har khana renderer (normalizeFoundUsConfig) saaf karta hai.
 */
function acceptFoundUsConfig(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  if (!Array.isArray(data.options)) return null;
  try {
    if (JSON.stringify(data).length > 32_000) return null;
  } catch {
    return null;
  }
  return data;
}

module.exports = { cleanFoundAnswer, acceptFoundUsConfig };
