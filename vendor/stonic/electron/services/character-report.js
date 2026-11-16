// ═══════════════════════════════════════════════════════════════════════════
// character-report.js — paid user abhi kis character par hai
// ---------------------------------------------------------------------------
// KYUN: Usman ko janna hai ke paid users mein kaunsa character sab se zyada
// chal raha hai, taake usi ko behtar banaya jaye. Server par har user ki EK
// row hai (supabase/user-characters.sql → `user_characters`): email +
// character. Ye file us row ko taza rakhti hai.
//
// KAB BHEJA JATA HAI (renderer bulata hai, lib/character-report.ts):
//   • app khulne par, jab access mil chuka ho — purana user character na bhi
//     badle to bhi gina jata hai;
//   • character badalte hi.
//
// KYA BHEJA JATA HAI: character ki id — built-in ("female_partner",
// "male_jarvis"…) ya custom ho to sirf "female_custom" / "male_custom".
// Custom character ka naam ya prompt kabhi nahi. Email body mein nahi jati:
// server login token se khud nikalta hai (koi kisi aur ke naam par na likh sake).
//
// …aur us ke saath `picked`:
//
//   true  = user ne KHUD ye character chuna
//   false = user ne kabhi kuch nahi chuna, app ka default chal raha hai
//           (male_jarvis / female_girlfriend)
//
// Ye farq is poori ginti ki jaan hai. App mein hamesha koi na koi character
// "chal raha" hota hai, is liye sirf id bhejna default ko khud ba khud sab se
// mashhoor bana deta — aur hum ghalat character par mehnat lagate rehte.
// Dono ko alag rakhne par do asal sawal ka jawab milta hai: log kya CHUNTE
// hain, aur default kitnon par chipka reh jata hai.
//
// Sirf paid: jab tak access ki aakhri tasdeeq "approved" na ho, kuch nahi jata.
//
// TELEMETRY WALA USOOL: ye file app ko kabhi nahi tor sakti. Har cheez
// try/catch mein, koi intezar startup ya kisi button ke raste mein nahi.
// Server band ho to chup chaap baad mein dobara koshish — app waise hi chalti.
// ═══════════════════════════════════════════════════════════════════════════

// axios pehli bhejne par load hota hai, boot par nahi (main.js ka note dekho).
const axios = { post: (...args) => require('axios').post(...args) };

/** Built-in id ya `<gender>_custom`. Server (set-character.cjs) bhi yehi maanta hai. */
const CHARACTER_RE = /^(female|male)_[a-z_]{1,30}$/;

const SEND_TIMEOUT_MS = 8000;
const RETRY_DELAY_MS = 2 * 60 * 1000;
/** Ek process mein itni nakaam koshishon ke baad ruk jao — agli launch phir bhejegi. */
const MAX_RETRIES = 3;

const isCharacterId = (value) => typeof value === 'string' && CHARACTER_RE.test(value);

/**
 * Dobara-bhejne se bachne ki pehchan. Sirf id par nahi — `picked` bhi is mein
 * hai, warna wohi banda jo default Jarvis par tha aur ab KHUD Jarvis chun
 * leta hai, dobara kabhi report na hota aur server par hamesha "default"
 * likha reh jata.
 */
const keyOf = (character, picked) => `${character}|${picked ? 1 : 0}`;

/**
 * @param {object} opts
 * @param {string} opts.endpoint      set-character function ka URL
 * @param {() => Promise<{token?: string, error?: string}>} opts.getBearer  login token
 * @param {() => boolean} opts.isApproved  aakhri access check ne andar aane diya?
 * @param {(url: string, body: object, config: object) => Promise<any>} [opts.post]  test ke liye
 * @param {number} [opts.retryDelayMs]
 */
function createCharacterReporter({ endpoint, getBearer, isApproved, post, retryDelayMs = RETRY_DELAY_MS, log = console }) {
  const send = post || ((url, body, config) => axios.post(url, body, config));

  let lastSent = null;   // server tak pohncha hua aakhri haal, `keyOf()` ki shakl mein
  let pending = null;    // bhejna baqi: { character, picked, key }
  let sending = false;
  let failures = 0;
  let retryTimer = null;

  const scheduleRetry = () => {
    if (retryTimer || failures >= MAX_RETRIES) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      void flush();
    }, retryDelayMs);
    // Timer app ko band hone se kabhi na roke.
    if (typeof retryTimer.unref === 'function') retryTimer.unref();
  };

  async function flush() {
    if (sending || !pending) return;
    if (pending.key === lastSent) { pending = null; return; }
    // Paid nahi (ya abhi tasdeeq nahi hui): kuch nahi bhejte. `pending` rehta
    // hai — agla report() (access milne par renderer khud bulata hai) le jayega.
    if (!safeApproved()) return;

    sending = true;
    const { character, picked, key } = pending;
    try {
      const bearer = await getBearer();
      if (!bearer?.token) throw new Error(bearer?.error || 'no_token');
      await send(endpoint, { character, picked }, {
        timeout: SEND_TIMEOUT_MS,
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${bearer.token}` },
      });
      lastSent = key;
      failures = 0;
      if (pending?.key === key) pending = null;
    } catch (err) {
      failures += 1;
      log.warn?.('[character-report] send failed (ignored, will retry):', err?.message || err);
      scheduleRetry();
    } finally {
      sending = false;
    }

    // Bhejte bhejte user ne phir character badal diya — naya wala bhi bhejo.
    if (pending && pending.key !== lastSent && failures === 0) void flush();
  }

  function safeApproved() {
    try { return Boolean(isApproved()); } catch { return false; }
  }

  /**
   * Renderer se: "abhi ye character chal raha hai". Foran wapas aata hai.
   *
   * @param {string} character  "female_partner" ya "<gender>_custom"
   * @param {boolean} picked    user ne khud chuna (true) ya default hai (false)
   */
  function report(character, picked) {
    try {
      if (!isCharacterId(character)) return false;
      // `picked` sirf saaf `true` par true. Purana renderer (jo ye daleel
      // bhejta hi nahi) `undefined` dega — us ko "default" ginna theek hai:
      // na-maloom ko pasand ginna ginti ko jhoota karta hai, ulta nahi.
      const chosen = picked === true;
      const key = keyOf(character, chosen);
      pending = { character, picked: chosen, key };
      // Naya haal = nayi koshish, purani nakamiyon ka hisaab saaf.
      if (key !== lastSent) failures = 0;
      void flush().catch(() => {});
      return true;
    } catch (err) {
      log.warn?.('[character-report] report failed (ignored):', err?.message || err);
      return false;
    }
  }

  return { report, flush, _state: () => ({ lastSent, pending, failures, sending }) };
}

module.exports = { createCharacterReporter, isCharacterId, CHARACTER_RE };
