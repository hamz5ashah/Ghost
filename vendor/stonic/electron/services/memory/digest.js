// Nichor (digest): baat khatam hone ke baad sasta AI us mein se yaad nikalta hai.
//
// ════════════════════════════════════════════════════════════════════════════
//  IS FILE KE DO HISSE — AUR ASAL KAAM DOOSRA HAI
// ════════════════════════════════════════════════════════════════════════════
//
//  1. PROMPT: model ko baat-cheet di jati hai aur wo JSON mein tajweez deta hai
//     (yaadein, tabdeeliyan, waade, tukre).
//  2. GATE: code har tajweez ko jaanchta hai. Model ki koi baat seedhi store
//     mein nahi jati.
//
//  Research ka sabaq yehi tha: memory systems "dhoondne" mein nahi, "likhne"
//  mein nakaam hote hain — ya kachra jama karte hain ya gharte hain. Is liye
//  yahan model sirf TAJWEEZ deta hai, FAISLA code karta hai.
//
// ── GATE KE DARWAZE ─────────────────────────────────────────────────────────
//
//   - QUOTE CHECK: har yaad ke saath model ko user ka asal jumla dena parta
//     hai. Code dekhta hai ke wo jumla waqai user ke kisi turn mein mojood hai.
//     Na mile to yaad RAD. (Ghari hui yaad ka rasta yahin band hota hai.)
//   - user ke baare mein fact SIRF user ke jumlon se; assistant ke jumle sirf
//     samajhne ke liye saath jate hain.
//   - jo text model ko pehle "yaad" ke taur par dikhaya gaya tha wo input mein
//     jata hi nahi — warna wohi baat dobara "nayi" ban kar likhi jati rehti.
//   - jis session mein web page / document parha gaya, us se sirf khud kahi
//     baatein (andaze nahi).
//   - hassas baat sirf wo jo user ne khud kahi.
//   - fi session hadd; wohi baat pehle se ho to mazboot hoti hai, dobara nahi banti;
//     "bhool jao" wali baat wapas nahi aati (dono store.addMemory mein).
//
// ── YE FILE ELECTRON AUR NETWORK KE BAGHAIR HAI ─────────────────────────────
//
//  LLM call bahar se aati hai (`llm` function). Test mein nakli model lagta hai
//  aur poora gate bina internet ke jaancha jata hai.

const { foldText } = require('./normalize');
const {
  DIGEST, LIMITS, INTENTIONS, MEMORY_TYPES, SINGLE_VALUED_TYPES, TURN_FLAGS, DAY,
} = require('./config');
const { absoluteDate, relativeTime, startOfDay } = require('./timefmt');
const { wordOverlap } = require('./store');

const oneLine = (text) => String(text || '').replace(/\s+/g, ' ').trim();
const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const shortId = (uid) => String(uid || '').slice(0, 10);
const isoLocal = (ts) => {
  const d = new Date(ts);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};

const SYSTEM_PROMPT = `You maintain the long-term memory of a personal AI companion. You are given one finished conversation between the USER and the companion (ASSISTANT). Decide what, if anything, is worth remembering about the USER for months to come. You output one JSON object and nothing else.

The USER may speak Urdu, Roman Urdu, Hindi or English, often mixed. Understand all of them.

# memories — lasting facts about the USER, taken ONLY from USER lines
Types: identity (name, city, job, age, family facts) · preference (likes, dislikes, how they want to be treated) · relationship (a person in their life and who that person is) · project (ongoing work, goals, decisions made) · routine (habits, schedule) · event (a specific dated thing that happened or will happen) · opinion (a view they hold) · instruction (a standing "always / never do X" for the companion).
For each memory give:
- "type"
- "text_original": one short, complete sentence in the USER's own language and script, in third person, that still makes sense when read alone months later.
- "text_en": the same fact as one short English sentence.
- "quote": the exact words copied from the USER line that prove it (3 to 25 words, copied character for character).
- "turn": the number of that USER line.
- "origin": "stated" if the USER said it directly, "inferred" if you concluded it.
- "event_date": "YYYY-MM-DD" when the fact is about a specific day (resolve words like kal, parson, next Friday against the conversation date), otherwise null.
- "entities": people, places, organisations or projects named: [{"name", "type": "person|place|org|project|other", "aliases": [other names used for the same thing]}].
- "sensitivity": "sensitive" for health, religion, politics, money trouble, sexuality or legal trouble; otherwise "normal".
- "explicit": true only if the USER asked for this to be remembered.
- "emotion": 0 to 1, how emotionally charged this was for the USER.
- "triggers": up to 3 single words (a name, a project) that should bring this to mind — only when "explicit" is true, otherwise [].

# updates — a USER line repeats or changes something in KNOWN MEMORIES
{"id", "relation": "same" | "refines" | "contradicts", "text_original", "text_en", "quote", "turn"}
"same": they simply said it again (text fields may be empty). "refines": same fact, more detail. "contradicts": it is no longer true and this replaces it. Never also list it under memories.

# intentions — something to do or follow up later
Said by the USER, or promised to the USER by the ASSISTANT. Do not list one that is already in OPEN PROMISES.
{"what" (in the USER's language), "what_en", "who_for", "due": "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD" or null, "quote", "turn"}

# resolved — ids from OPEN PROMISES that this conversation shows are finished
{"id", "how": "done" | "cancelled"}

# episodes — split the conversation into 1 to 4 topic segments, covering every line
{"first", "last", "title" (max 8 words), "summary_en" (2 to 4 sentences: what was discussed, what was decided, concrete names, numbers and dates), "mood" (one word for the USER's mood), "salience": 0 to 1}

# DO NOT EXTRACT as a memory
- anything the ASSISTANT said, suggested or assumed — ASSISTANT lines are context only
- questions the USER asked, general knowledge, facts about the world
- passing states ("tired today", "busy right now") — these belong in the episode summary only
- judgements about the USER's personality or psychology
- a mistaken belief of the USER stated as if it were true; a view they hold is type "opinion"
- passwords, codes, card or account numbers, keys
- anything already in KNOWN MEMORIES that has not changed
- anything you are unsure about. When in doubt, leave it out.

Most conversations produce 0 to 3 memories. Zero is a normal and correct result. Never invent a quote.

# Output
{"memories": [], "updates": [], "intentions": [], "resolved": [], "episodes": []}`;

class MemoryDigest {
  /**
   * @param {import('./store').MemoryStore} store
   * @param {import('./search').MemorySearch} search
   * @param {{ llm: (req: {system: string, user: string, maxOutputTokens: number}) => Promise<{text: string, costUsd?: number}> }} deps
   */
  constructor(store, search, deps) {
    this.store = store;
    this.search = search;
    this.llm = deps.llm;
  }

  /** Is session ke wo turns jin ka nichor baqi hai — khud-kaar aur "yaad dikhayi gayi" wale chhor kar. */
  pendingTurns(session) {
    return this.store.getTurns(session.id, { afterId: session.digested_upto })
      .filter((t) => !(t.flags & (TURN_FLAGS.automated | TURN_FLAGS.recalled | TURN_FLAGS.forgotten)));
  }

  /** Lambi baat-cheet ko tukron mein — har tukra ek LLM call. */
  chunks(turns) {
    const out = [];
    let cur = [];
    let size = 0;
    for (const t of turns) {
      if (cur.length && size + t.text.length > DIGEST.maxCharsPerCall) { out.push(cur); cur = []; size = 0; }
      cur.push(t);
      size += t.text.length + 20;
    }
    if (cur.length) out.push(cur);
    return out;
  }

  /** Model ko "pehle se yaad" ki chhoti list: taake dobara na likhe aur tabdeeli pakar sake. */
  related(turns) {
    const userText = turns.filter((t) => t.speaker === 'user').map((t) => t.text).join(' ').slice(0, 2000);
    const found = this.search.recall({ query: userText, mind: 'hermes', kinds: ['memory'], k: 14, purpose: 'answer' });
    const byId = new Map();
    for (const it of found.items) byId.set(it.id, this.store.getMemory(it.id));
    for (const m of this.store.listMemories({ status: 'active', pinned: true, limit: 20 })) byId.set(Number(m.id), m);
    return [...byId.values()].filter(Boolean).slice(0, 24);
  }

  buildUserPrompt(session, turns, known, openIntentions, now) {
    const when = turns.length ? turns[0].ts : now;
    const d = new Date(when);
    const lines = turns.map((t, i) => `[${i + 1}] ${t.speaker === 'user' ? 'USER' : 'ASSISTANT'}: ${oneLine(t.text)}`);
    const knownLines = known.map((m) => `- (${shortId(m.uid)}) [${m.type}] ${oneLine(m.text_original)}`);
    const intentLines = openIntentions.map((i) => `- (${shortId(i.uid)}) ${oneLine(i.what)}${i.due_ts ? ` — due ${isoLocal(i.due_ts)}` : ''}`);
    return [
      `Conversation date: ${WEEKDAYS[d.getDay()]}, ${absoluteDate(when)} (${isoLocal(when)}), ${relativeTime(when, now) || 'today'}.`,
      session.external_content
        ? 'NOTE: during this conversation the assistant read outside content (a web page, document or email). Extract only what the USER stated in their own words. Do not infer anything.'
        : '',
      '',
      'KNOWN MEMORIES (do not repeat these; use "updates" if one changed)',
      knownLines.length ? knownLines.join('\n') : '(none)',
      '',
      'OPEN PROMISES',
      intentLines.length ? intentLines.join('\n') : '(none)',
      '',
      'CONVERSATION',
      lines.join('\n'),
    ].filter((l) => l !== '').join('\n');
  }

  /** Model ke jawab mein se JSON nikalna. Nakaami par `null`. */
  static parse(text) {
    const raw = String(text || '');
    const start = raw.indexOf('{');
    const end = raw.lastIndexOf('}');
    if (start < 0 || end <= start) return null;
    try {
      const obj = JSON.parse(raw.slice(start, end + 1));
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null;
      const arr = (v) => (Array.isArray(v) ? v.filter((x) => x && typeof x === 'object') : []);
      return {
        memories: arr(obj.memories), updates: arr(obj.updates), intentions: arr(obj.intentions),
        resolved: arr(obj.resolved), episodes: arr(obj.episodes),
      };
    } catch {
      return null;
    }
  }

  /**
   * QUOTE CHECK. Quote kis turn mein hai? Pehle wo turn jo model ne bataya, phir
   * baqi (model number ghalat de sakta hai). `userOnly` par sirf user ke turns.
   * @returns {object|null} turn
   */
  static findQuote(quote, turnNo, turns, userOnly) {
    const q = foldText(oneLine(quote));
    if (q.length < 6 || q.split(' ').length < 2) return null;
    const ok = (t) => t && (!userOnly || t.speaker === 'user') && foldText(t.text).includes(q);
    const named = turns[Number(turnNo) - 1];
    if (ok(named)) return named;
    return turns.find(ok) || null;
  }

  static parseDate(value, anchor) {
    if (!value || typeof value !== 'string') return null;
    const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(value.trim());
    if (!m) return null;
    const hasTime = m[4] != null;
    const ts = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), hasTime ? Number(m[4]) : 9, hasTime ? Number(m[5]) : 0, 0).getTime();
    if (!Number.isFinite(ts)) return null;
    // Model kabhi kabhi saal ghalat likh deta hai. Aqal ki hadd se bahar tareekh rad.
    if (ts < anchor - 20 * 365 * DAY || ts > anchor + 5 * 365 * DAY) return null;
    return ts;
  }

  /**
   * Tajweez shuda waadon mein se wo jo voice isi baat-cheet mein khud likh chuki
   * hai (`follow_up` tool -> service.followUp). Nichor wohi baat apne lafzon mein
   * dobara tajweez karta hai; yahan wo ruk jati hai — warna ek hi waada do dafa
   * list mein hota aur do dafa uthaya jata. Poore ho chuke waade bhi dekhe jate
   * hain, taake band kiya hua waada nichor se zinda na ho jaye.
   *
   * Har likha hua waada zyada se zyada EK tajweez ko rokta hai (sab se milti
   * julti ko): "5 baje Ali ko call" likha ho to "5 baje Ahmed ko call" phir bhi banta hai.
   *
   * @returns {Set<number>} `items` ke wo index jo pehle se likhe hain
   */
  alreadyNoted(session, items, turns, anchor) {
    const taken = new Set();
    const made = this.store.intentionsFromSession(session.id, 'voice');
    if (!made.length) return taken;

    const pairs = [];
    items.forEach((item, index) => {
      const what = oneLine(item.what || item.what_en);
      if (!what) return;
      const turn = MemoryDigest.findQuote(item.quote, item.turn, turns, false);
      const dueTs = MemoryDigest.parseDate(item.due, anchor);
      made.forEach((row, slot) => {
        const overlap = Math.max(wordOverlap(what, row.what), wordOverlap(oneLine(item.what_en), row.what));
        const sameTurn = !!turn && row.source_turn != null && Number(row.source_turn) === Number(turn.id);
        const sameDay = !!dueTs && !!row.due_ts && startOfDay(dueTs) === startOfDay(row.due_ts);
        const cue = sameTurn || sameDay;
        if (overlap >= (cue ? INTENTIONS.sameOverlapWithCue : INTENTIONS.sameOverlap)) {
          pairs.push({ index, slot, rank: overlap + (cue ? 1 : 0) });
        }
      });
    });
    pairs.sort((a, b) => b.rank - a.rank);
    const used = new Set();
    for (const p of pairs) {
      if (taken.has(p.index) || used.has(p.slot)) continue;
      taken.add(p.index);
      used.add(p.slot);
    }
    return taken;
  }

  /**
   * Model ki tajweez ko gate se guzaar kar store mein lagana.
   * @returns {{ memories: number, updates: number, intentions: number, resolved: number, episodes: number,
   *   rejected: Array<{ kind: string, why: string, text: string }> }}
   */
  apply(session, turns, parsed, now) {
    const out = { memories: 0, updates: 0, intentions: 0, resolved: 0, episodes: 0, rejected: [] };
    const reject = (kind, why, item) => out.rejected.push({ kind, why, text: oneLine(item.text_en || item.text_original || item.what_en || item.what || '').slice(0, 120) });
    const anchor = turns.length ? turns[0].ts : now;
    const external = !!session.external_content;
    const base = { sourceSession: session.id, createdBy: 'digest', promptVersion: DIGEST.promptVersion, actor: 'digest' };

    const run = this.store.db.transaction(() => {
      // ── yaadein ──
      for (const item of parsed.memories) {
        if (out.memories >= LIMITS.digestMemoriesPerSession) { reject('memory', 'session_cap', item); continue; }
        const text = oneLine(item.text_original || item.text_en);
        if (!text) { reject('memory', 'empty', item); continue; }
        if (!MEMORY_TYPES.includes(item.type)) { reject('memory', 'bad_type', item); continue; }
        const turn = MemoryDigest.findQuote(item.quote, item.turn, turns, true);
        if (!turn) { reject('memory', 'quote_not_found', item); continue; }
        const origin = item.origin === 'stated' ? 'stated' : 'inferred';
        if (external && origin !== 'stated') { reject('memory', 'external_inferred', item); continue; }
        const sensitive = item.sensitivity === 'sensitive';
        if (sensitive && origin !== 'stated') { reject('memory', 'sensitive_inferred', item); continue; }
        if (/^(the )?user (asked|asks|is asking|wants to know|wanted to know|greeted|said (hi|hello))/i.test(oneLine(item.text_en))) {
          reject('memory', 'question_not_fact', item); continue;
        }
        const eventStart = MemoryDigest.parseDate(item.event_date, anchor);
        const explicit = item.explicit === true;
        const made = this.store.addMemory({
          ...base,
          type: item.type, text, textEn: oneLine(item.text_en), origin,
          scope: 'user',
          sensitivity: sensitive ? 'sensitive' : 'normal',
          eventStart,
          mentionedAt: turn.ts,
          triggers: explicit && Array.isArray(item.triggers) ? item.triggers.slice(0, 3) : [],
          evidence: [{ turnId: turn.id, quote: oneLine(item.quote) }],
          entities: Array.isArray(item.entities) ? item.entities.slice(0, 6) : [],
          salience: {
            explicit: explicit ? 1 : 0,
            future: eventStart && eventStart > now ? 1 : 0,
            emotion: Math.max(0, Math.min(1, Number(item.emotion) || 0)),
          },
          cause: 'digest',
        });
        if (made.ok) out.memories += 1; else reject('memory', made.reason || 'rejected', item);
      }

      // ── tabdeeliyan ──
      for (const item of parsed.updates) {
        const old = this.store.findMemoryByShortId(item.id);
        if (!old) { reject('update', 'unknown_id', item); continue; }
        const turn = MemoryDigest.findQuote(item.quote, item.turn, turns, true);
        if (!turn) { reject('update', 'quote_not_found', item); continue; }
        const evidence = [{ turnId: turn.id, quote: oneLine(item.quote) }];
        if (item.relation === 'same') {
          this.store.reinforce(Number(old.id), { evidence, sourceSession: session.id });
          out.updates += 1;
          continue;
        }
        const text = oneLine(item.text_original || item.text_en);
        if (!text) { reject('update', 'empty', item); continue; }
        const next = { ...base, text, textEn: oneLine(item.text_en), origin: 'stated', evidence, mentionedAt: turn.ts, cause: `digest:${item.relation}` };
        if (item.relation === 'refines' || SINGLE_VALUED_TYPES.has(old.type)) {
          const made = this.store.supersede(Number(old.id), next, { actor: 'digest', cause: `digest:${item.relation}` });
          if (made.ok) out.updates += 1; else reject('update', made.reason || 'rejected', item);
        } else {
          // Jama hone wali qism (log, projects): nayi baat aane se purani khatam nahi
          // hoti. Dono rehti hain, purani ka bharosa thora ghat-ta hai.
          const made = this.store.addMemory({ ...next, type: old.type, scope: old.scope });
          if (made.ok) { this.store.weaken(Number(old.id), 0.2, { actor: 'digest', cause: 'digest:contradicts' }); out.updates += 1; }
          else reject('update', made.reason || 'rejected', item);
        }
      }

      // ── waade ──
      const noted = this.alreadyNoted(session, parsed.intentions, turns, anchor);
      for (const [index, item] of parsed.intentions.entries()) {
        if (noted.has(index)) { reject('intention', 'already_saved', item); continue; }
        if (out.intentions >= LIMITS.digestIntentionsPerSession) { reject('intention', 'session_cap', item); continue; }
        const what = oneLine(item.what || item.what_en);
        if (!what) { reject('intention', 'empty', item); continue; }
        // Waada assistant ne bhi kiya ho sakta hai — is liye yahan dono ke turns.
        const turn = MemoryDigest.findQuote(item.quote, item.turn, turns, false);
        if (!turn) { reject('intention', 'quote_not_found', item); continue; }
        const dueTs = MemoryDigest.parseDate(item.due, anchor);
        const made = this.store.addIntention({
          what, whatEn: oneLine(item.what_en), whoFor: item.who_for ? oneLine(item.who_for) : null,
          cueType: 'time', dueTs,
          expiryTs: dueTs ? dueTs + INTENTIONS.expiryAfterDueMs : turn.ts + INTENTIONS.expiryUndatedMs,
          sourceTurn: turn.id, sourceSession: session.id, quote: oneLine(item.quote),
          createdBy: 'digest', characterId: session.character_id || null, actor: 'digest', cause: 'digest',
        });
        if (made.ok && !made.duplicateOf) out.intentions += 1;
      }

      // ── jo waade poore ho gaye ──
      for (const item of parsed.resolved) {
        const row = this.store.findIntentionByShortId(item.id);
        if (!row || row.status !== 'open') continue;
        this.store.resolveIntention(Number(row.id), item.how === 'cancelled' ? 'dismissed' : 'done', { actor: 'digest', cause: 'digest' });
        out.resolved += 1;
      }

      // ── tukre (episodes) ──
      for (const item of parsed.episodes.slice(0, 4)) {
        const first = turns[Math.max(1, Number(item.first) || 1) - 1];
        const last = turns[Math.min(turns.length, Number(item.last) || turns.length) - 1];
        if (!first || !last || last.id < first.id) continue;
        this.store.addEpisode({
          sessionKey: session.id, channel: session.channel, characterId: session.character_id,
          firstTurn: first.id, lastTurn: last.id, startTs: first.ts, endTs: last.ts,
          title: oneLine(item.title), summaryEn: oneLine(item.summary_en), mood: item.mood,
          salience: Number(item.salience) || 0, digestVersion: DIGEST.promptVersion,
        });
        out.episodes += 1;
      }

      this.store.markDigested(session.id, turns[turns.length - 1].id);
    });
    run();
    return out;
  }

  /**
   * Ek session ka nichor. Kabhi throw nahi karta: nakaami `ok:false` aur wajah.
   *
   * @returns {Promise<{ ok: boolean, reason?: string, skipped?: boolean, calls: number, costUsd: number,
   *   memories: number, updates: number, intentions: number, resolved: number, episodes: number, rejected: object[] }>}
   */
  async digestSession(sessionKey, now = this.store.now()) {
    const total = { ok: true, calls: 0, costUsd: 0, memories: 0, updates: 0, intentions: 0, resolved: 0, episodes: 0, rejected: [] };
    const session = this.store.getSession(sessionKey);
    if (!session) return { ...total, ok: false, reason: 'no_session' };
    if (this.store.isPaused()) return { ...total, ok: false, reason: 'paused' };

    const pending = this.pendingTurns(session);
    const all = this.store.getTurns(session.id, { afterId: session.digested_upto });
    if (!all.length) return { ...total, skipped: true, reason: 'nothing_new' };
    const lastId = all[all.length - 1].id;

    // Awareness ki apni baatein user ka fact nahi bantin; aur do jumlon se kam
    // ki baat-cheet (hello / hi) mein seekhne ko kuch nahi. Dono ko "ho gaya"
    // mark kar dete hain taake qataar mein baar baar na aayen.
    const userTurns = pending.filter((t) => t.speaker === 'user').length;
    if (session.channel === 'awareness' || userTurns < DIGEST.minUserTurns) {
      this.store.markDigested(session.id, lastId);
      return { ...total, skipped: true, reason: session.channel === 'awareness' ? 'awareness' : 'too_small' };
    }

    for (const turns of this.chunks(pending)) {
      const known = this.related(turns);
      const open = this.store.listIntentions({ status: 'open', limit: 20 });
      let reply;
      try {
        reply = await this.llm({
          system: SYSTEM_PROMPT,
          user: this.buildUserPrompt(session, turns, known, open, now),
          maxOutputTokens: 1800,
        });
      } catch (err) {
        return { ...total, ok: false, reason: `llm:${err && err.code ? err.code : (err && err.message) || 'error'}` };
      }
      total.calls += 1;
      total.costUsd += Number.isFinite(reply && reply.costUsd) ? reply.costUsd : DIGEST.fallbackCostUsd;
      const parsed = MemoryDigest.parse(reply && reply.text);
      if (!parsed) return { ...total, ok: false, reason: 'bad_json' };
      const res = this.apply(session, turns, parsed, now);
      for (const k of ['memories', 'updates', 'intentions', 'resolved', 'episodes']) total[k] += res[k];
      total.rejected.push(...res.rejected);
    }
    // Agar aakhri turns khud-kaar the (jo pending mein nahi the) to nishan wahan tak le jao.
    this.store.markDigested(session.id, lastId);
    return total;
  }
}

module.exports = { MemoryDigest, SYSTEM_PROMPT };
