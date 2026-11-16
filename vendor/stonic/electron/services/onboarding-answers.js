// ═══════════════════════════════════════════════════════════════════════════
// onboarding-answers.js — Step 6 ke jawab, lead report ke liye saaf karna
// ---------------------------------------------------------------------------
// Renderer (lib/onboarding/about.ts → answersForStats) har jawab diye hue
// sawal ka { q, question, a, answer } bhejta hai. Ye wohi shakl yahan pakki
// karta hai, phir save-lead website ke `leads.onboarding_answers` mein likhta
// hai. Server (save-lead.cjs) yehi qaide dobara lagata hai — desktop par
// bharosa nahi, ye sirf faltu data network par jane se rokta hai.
//
// Sawal ka matn saath jata hai taake sawal badle ya naya aaye to admin mein
// wohi dikhe jo user ne dekha — koi list yahan ya website par nahi.
// ═══════════════════════════════════════════════════════════════════════════

const ID_RE = /^[a-z0-9_]{1,40}$/;
const MAX_ANSWERS = 10;
const MAX_TEXT = 200;

const cleanText = (value) => {
  if (typeof value !== 'string') return '';
  // Control characters hata do, khali jagah ek kar do.
  return value.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, MAX_TEXT);
};

/** Saaf list, ya `undefined` (kuch na bache to field bheji hi nahi jati). */
function cleanOnboardingAnswers(raw) {
  if (!Array.isArray(raw)) return undefined;
  const seen = new Set();
  const out = [];
  for (const item of raw.slice(0, MAX_ANSWERS)) {
    if (!item || typeof item !== 'object') continue;
    const q = typeof item.q === 'string' ? item.q : '';
    const a = typeof item.a === 'string' ? item.a : '';
    if (!ID_RE.test(q) || !ID_RE.test(a) || seen.has(q)) continue;
    const question = cleanText(item.question) || q;
    const answer = cleanText(item.answer) || a;
    seen.add(q);
    out.push({ q, question, a, answer });
  }
  return out.length ? out : undefined;
}

module.exports = { cleanOnboardingAnswers };
