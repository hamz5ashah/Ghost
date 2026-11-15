// Yaad nikalna (recall) — bina kisi AI ke, sirf is PC par.
//
// ════════════════════════════════════════════════════════════════════════════
//  YE KAISE KAAM KARTA HAI
// ════════════════════════════════════════════════════════════════════════════
//
//  Sawal aata hai ("Ali wala masla kya tha?"). Us ke kaam ke lafz nikalte hain
//  aur kai "channels" se umeedwar aate hain:
//
//    yaadein (nichor)   : poore lafz · lafz ke tukre · consonant skeleton
//                         · naam (entity) · trigger lafz
//    asal baat-cheet    : poore lafz · tukre · skeleton
//    waqiat (episodes)  : poore lafz · skeleton
//
//  Har channel apni tarteeb deta hai. RRF un tarteebon ko milata hai — wo sirf
//  ye dekhta hai ke cheez kis NUMBER par aayi, score nahi, is liye alag alag
//  channels ke score ka aapas mein muqabla nahi karna parta.
//
//  Phir DARWAZE (haan/na: mansookh? doosre character ki? hassas?) aur teen
//  halke wazan: kitni taaza / kaam aayi (A), kitni ahem (S), kitni pakki (C).
//
//      Final = R x (0.5 + coverage) x (1 + 0.25·A + 0.25·S) x C
//
//  Formula zarb wala hai aur wazan chhote hain: mutalliq hona hamesha haavi
//  rehta hai. Koi "ahem magar be-mauqa" yaad upar nahi aa sakti.
//
// ── MILA / KAMZOR / NAHI MILA ───────────────────────────────────────────────
//
//  RRF ka score mutlaq nahi hota, is liye "mila ya nahi" ka faisla COVERAGE par
//  hai: sawal ke kaam ke lafzon mein se kitne top natije mein waqai mojood hain.
//  Yehi teen haalatein model ko di jati hain taake wo "pakka", "shayad" aur
//  "yaad nahi" mein farq kar sake — gharne ki jagah.
//
// ── YE FILE KYA NAHI KARTI ──────────────────────────────────────────────────
//
//  Koi LLM call nahi, koi network nahi, koi likhayi nahi (siwaye bulane wale ke
//  `markUsed` ke). Is liye voice ke raaste par chalane mein koi der nahi.

const { queryTokens, skeleton, foldText } = require('./normalize');
const { synonymsOf } = require('./lexicon');
const { SEARCH, TURN_FLAGS } = require('./config');
const { scopesFor, parseJson } = require('./store');
const { whenText, DAY } = require('./timefmt');

/**
 * Sawal ke bharti ke lafz: "cigarette WALI BAAT", "Ali ke baare mein BATAO",
 * "YAAD hai...". Ye yaad ke baare mein hain, yaad ke andar ki cheez nahi — inhein
 * dhoondne se sirf shor aata hai. Sirf SAWAL se nikalte hain; mehfooz text waise
 * ka waisa rehta hai, is liye is list ko badalne se purana index nahi badalta.
 */
const QUERY_FILLERS = new Set([
  'wali', 'wala', 'wale', 'baat', 'baatein', 'cheez', 'sab', 'yaad', 'batao', 'bata', 'bataya',
  'tell', 'remember', 'recall', 'rakhna', 'sahab', 'sahib', 'abhi', 'thing', 'things', 'something',
].map((w) => foldText(w)));

/** FTS5 ke sawal mein lafz hamesha quotes mein — warna "or", "and", "-" hukm ban jate hain. */
const quote = (term) => `"${String(term).replace(/"/g, '""')}"`;

/** Roman Urdu ke lafz aakhir se badalte hain (karni / karna / karne) — aakhri vowel hata kar jar. */
function stemOf(fold) {
  if (fold.length < 5 || /[^a-z]/.test(fold)) return null;
  const stem = fold.replace(/[aeiouy]+$/, '');
  return stem.length >= 3 && stem !== fold ? stem : null;
}

/**
 * Do lafzon ke beech kitne huroof ka farq hai — `max` se zyada ho to foran ruk
 * jata hai. Naamon ki spelling ke liye: "osama" / "usama", "farhan" / "farhaan".
 */
function editDistance(a, b, max) {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i += 1) {
    const cur = [i];
    let low = cur[0];
    for (let j = 1; j <= b.length; j += 1) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      if (cur[j] < low) low = cur[j];
    }
    if (low > max) return max + 1;
    prev = cur;
  }
  return prev[b.length];
}

class MemorySearch {
  /** @param {import('./store').MemoryStore} store */
  constructor(store) {
    this.store = store;
    this.db = store.db;
  }

  _fts(table, match, limit) {
    if (!match) return [];
    try {
      return this.db.prepare(`select rowid as id from ${table} where ${table} match ? order by rank limit ?`)
        .all(match, limit).map((r) => Number(r.id));
    } catch {
      // Ghalat shakl ka sawal (FTS syntax) talash ko nahi girata — ye channel khali.
      return [];
    }
  }

  /** Sawal ke lafzon se teeno index ke sawal banana. */
  _matches(tokens) {
    const uni = [];
    const tri = [];
    const skel = [];
    for (const t of tokens) {
      if (!t.fold) continue;
      // Lambe lafz par prefix: "meeting" -> "meetings" bhi mile.
      uni.push(t.fold.length >= 5 ? `${quote(t.fold)}*` : quote(t.fold));
      if (t.fold.length >= 3) tri.push(quote(t.fold));
      const stem = stemOf(t.fold);
      if (stem) tri.push(quote(stem));
      if (t.skel && t.skel.length >= 2) skel.push(quote(t.skel));
      // Hum-maani lafz (lexicon.js): "salary" poocha to "tankhwa" bhi dhoondo.
      for (const alt of t.alts || []) {
        uni.push(quote(alt.fold));
        if (alt.skel && alt.skel.length >= 3) skel.push(quote(alt.skel));
      }
    }
    return {
      uni: uni.join(' OR '),
      tri: [...new Set(tri)].join(' OR '),
      skel: [...new Set(skel)].join(' OR '),
    };
  }

  /**
   * Har lafz ka WAZAN: jo lafz kam jagah aata hai wo zyada batata hai. "Tanaka"
   * ka milna "link" ke milne se kahin bara saboot hai — yehi idf hai.
   *
   * Jo lafz poore record mein kahin hai hi nahi (user ne doosra lafz istemal
   * kiya: "testing" ki jagah "staging") us ka wazan bohat kam rakha jata hai:
   * us ka na milna "ye baat nahi hui" ka saboot nahi, sirf alag lafzon ka hai.
   *
   * Ginti FTS5 ke apne lafz-naame (fts5vocab) se aati hai — index pehle se bana
   * hai, is liye ye chand microsecond ka sawal hai.
   */
  _weigh(tokens) {
    if (!this._vocabReady) {
      try {
        this.db.exec(`
          create virtual table if not exists temp.mem_vocab_turns using fts5vocab('main', 'turns_uni', 'row');
          create virtual table if not exists temp.mem_vocab_turns_skel using fts5vocab('main', 'turns_skel', 'row');
          create virtual table if not exists temp.mem_vocab_mem using fts5vocab('main', 'memories_uni', 'row');
          create virtual table if not exists temp.mem_vocab_mem_skel using fts5vocab('main', 'memories_skel', 'row');
        `);
        this._vocabReady = true;
      } catch {
        this._vocabReady = false;
      }
    }
    const total = Number(this.store.q('select (select count(*) from turns) + (select count(*) from memories) as n').get().n) || 0;
    const df = (table, term) => {
      if (!this._vocabReady || !term) return 0;
      try {
        const row = this.db.prepare(`select doc from temp.${table} where term = ?`).get(term);
        return row ? Number(row.doc) : 0;
      } catch { return 0; }
    };
    // Chhote record mein "kam-yaab" ka koi matlab nahi (har lafz ek do dafa hi
    // aaya hota hai), is liye wahan har mojood lafz barabar hai.
    const small = total < SEARCH.idfMinDocs;
    const idfOf = (d) => (small ? 1 : Math.log(1 + (total - d + 0.5) / (d + 0.5)));
    const top = idfOf(0);
    const rareBelow = Math.max(3, total * SEARCH.rareShare);
    return tokens.map((t) => {
      const alts = synonymsOf(t.fold);
      let exact = df('mem_vocab_turns', t.fold) + df('mem_vocab_mem', t.fold);
      // Hum-maani lafz bhi isi lafz ki "mojoodgi" hain: "salary" record mein na ho
      // magar "tankhwa" ho, to ye lafz ghair-hazir nahi.
      for (const alt of alts) exact += df('mem_vocab_turns', alt.fold) + df('mem_vocab_mem', alt.fold);
      const viaSkel = t.skel && t.skel.length >= 2 ? df('mem_vocab_turns_skel', t.skel) + df('mem_vocab_mem_skel', t.skel) : 0;
      const seen = exact || viaSkel;
      return {
        ...t,
        alts,
        present: seen > 0,
        weight: seen > 0 ? idfOf(seen) : top * SEARCH.absentWeight,
        idf: seen > 0 ? idfOf(seen) : 0,
        rare: seen > 0 && seen <= rareBelow,
      };
    });
  }

  /**
   * Sawal ke lafz is cheez mein kitne mojood hain — wazan ke saath (0..1).
   *
   *   coverage     wazni hissa jo mila
   *   strong       kam se kam ek lafz PAKKA mila (poora lafz, jar, ya lamba skeleton)
   *   matched      kitne lafz kisi na kisi tarah mile
   *   rareMatched  koi kam-yaab lafz (naam, khaas cheez) mila
   *
   * Skeleton (sirf consonants) doosri script ka pul hai: lamba skeleton
   * ("slgr" = salgirah = سالگرہ) kaafi pakka ishara hai, do huroof wala
   * ("kl" = kal / kala / call) kamzor.
   */
  _coverage(tokens, fold, skel) {
    if (!tokens.length) return { coverage: 0, strong: false, matched: 0, rareMatched: false, absent: 0 };
    const text = String(fold || '');
    const words = new Set(text.split(' '));
    const skels = new Set(String(skel || '').split(' '));
    let got = 0;
    let all = 0;
    let strong = false;
    let matched = 0;
    let rareMatched = false;
    for (const t of tokens) {
      let best = 0;
      if (words.has(t.fold)) best = 1;
      else {
        const stem = stemOf(t.fold);
        for (const w of words) {
          if (t.fold.length >= 4 && w.startsWith(t.fold)) { best = Math.max(best, 0.9); break; }
          if (stem && w.startsWith(stem)) best = Math.max(best, 0.8);
        }
        if (best < 0.8 && t.fold.length >= 4 && text.includes(t.fold)) best = 0.8;
        // Hum-maani lafz ka poora milna taqreeban utna hi pakka hai jitna khud lafz ka.
        if (best < 0.9) {
          for (const alt of t.alts || []) {
            if (words.has(alt.fold)) { best = 0.9; break; }
            if (best < 0.75 && alt.skel && alt.skel.length >= 4 && skels.has(alt.skel)) best = 0.75;
          }
        }
        if (best < 0.8 && t.skel && skels.has(t.skel)) {
          const bySkel = t.skel.length >= 4 ? 0.85 : (t.skel.length === 3 ? 0.7 : 0.4);
          best = Math.max(best, bySkel);
          // Wohi consonants aur sirf ek do huroof ka farq = wohi lafz, alag spelling
          // (naam aksar aise hi badalte hain: "osama" / "usama").
          if (best < 0.85 && t.fold.length >= 4) {
            const allow = t.fold.length >= 7 ? 2 : 1;
            for (const w of words) {
              if (Math.abs(w.length - t.fold.length) > allow) continue;
              if (editDistance(w, t.fold, allow) <= allow && skeleton(w) === t.skel) { best = 0.85; break; }
            }
          }
        }
      }
      if (best >= 0.8) strong = true;
      if (best >= 0.4) matched += 1;
      if (best >= 0.7 && t.rare) rareMatched = true;
      got += t.weight * best;
      all += t.weight;
    }
    // Wo lafz jo poore record mein kahin nahi (na khud, na hum-maani, na skeleton).
    const absent = tokens.filter((t) => !t.present).length;
    return { coverage: all > 0 ? got / all : 0, strong, matched, rareMatched, absent };
  }

  /** ACT-R: taaza aur baar baar kaam aane wali yaad zyada "jaagi" hoti hai. 0..1 */
  _activation(times, now) {
    let total = 0;
    for (const t of times) {
      if (!t) continue;
      const days = Math.max(0, (now - t) / DAY) + 1;
      total += days ** -SEARCH.activationDecay;
    }
    if (total <= 0) return 0;
    // ln(total): ek taaza zikr = 0 -> 0.5; purana -> neeche; baar baar -> upar.
    return 1 / (1 + Math.exp(-Math.log(total)));
  }

  _salience(m) {
    const w = SEARCH.salience;
    const u = Number(m.useful_count) || 0;
    return w.explicit * (m.sal_explicit || 0) + w.future * (m.sal_future || 0) + w.emotion * (m.sal_emotion || 0)
      + w.surprise * (m.sal_surprise || 0) + w.useful * (u / (1 + u));
  }

  /**
   * @param {{
   *   query: string, queryEn?: string,
   *   mind?: 'voice'|'hermes'|'awareness', characterId?: string|null,
   *   k?: number, purpose?: 'answer'|'proactive',
   *   since?: number, until?: number, includePast?: boolean,
   *   kinds?: Array<'memory'|'turn'|'episode'>, now?: number
   * }} input
   * @returns {{ status: 'FOUND'|'WEAK'|'NONE', items: object[], tokens: string[], tookMs: number }}
   */
  recall(input) {
    const started = Date.now();
    const now = input.now || this.store.now();
    const mind = input.mind || 'voice';
    const purpose = input.purpose || 'answer';
    const k = Math.max(1, Math.min(Number(input.k) || SEARCH.defaultK, 20));
    const kinds = new Set(input.kinds && input.kinds.length ? input.kinds : ['memory', 'turn', 'episode']);
    const empty = (status = 'NONE') => ({ status, items: [], tokens: [], tookMs: Date.now() - started });
    if (this.store.isPaused()) return empty();

    // Sawal apni zuban mein + (agar model ne diya) English mein. Umeedwar dono ke
    // lafzon se dhoonde jate hain, magar "kitna mila" har shakl ka ALAG napa jata
    // hai aur behtar wala liya jata hai: wohi sawal do zubanon mein likhne se us
    // ke lafz dugne ho jate hain, aur ek zuban ka na milna doosri ko saza na de.
    const meaningful = (text) => {
      const all = queryTokens(text || '').filter((t) => t.fold);
      const kept = all.filter((t) => !QUERY_FILLERS.has(t.fold));
      // Sawal sirf bharti ke lafzon ka ho to unhi par chalo — khali sawal se behtar.
      return kept.length ? kept : all;
    };
    const own = this._weigh(meaningful(input.query));
    const english = this._weigh(meaningful(input.queryEn));
    const seen = new Set();
    const tokens = [];
    for (const t of [...own, ...english]) {
      if (seen.has(t.fold)) continue;
      seen.add(t.fold);
      tokens.push(t);
    }
    if (tokens.length === 0) return empty();
    const variants = [own, english].filter((list) => list.length);
    const coverageOf = (fold, skel) => {
      let best = null;
      for (const list of variants) {
        const c = this._coverage(list, fold, skel);
        if (!best || c.coverage > best.coverage) best = { ...c, size: list.length };
      }
      return best;
    };

    const match = this._matches(tokens);
    const N = SEARCH.perChannel;
    const W = SEARCH.weights;
    const fused = new Map();
    const add = (kind, ids, weight, channel) => {
      ids.forEach((id, rank) => {
        const key = `${kind}:${id}`;
        const cur = fused.get(key) || { kind, id, rrf: 0, channels: new Set() };
        cur.rrf += weight / (SEARCH.rrfK + rank + 1);
        cur.channels.add(channel);
        fused.set(key, cur);
      });
    };

    const scopes = mind === 'hermes' ? ['user'] : scopesFor(input.characterId);

    if (kinds.has('memory')) {
      add('memory', this._fts('memories_uni', match.uni, N), W.memUni, 'uni');
      add('memory', this._fts('memories_tri', match.tri, N), W.memTri, 'tri');
      add('memory', this._fts('memories_skel', match.skel, N), W.memSkel, 'skel');
      const entityIds = this.store.entitiesByAliases(tokens.map((t) => t.fold), tokens.map((t) => t.skel));
      add('memory', this.store.memoriesForEntities(entityIds, N), W.memEntity, 'entity');
      add('memory', this._triggered(tokens, scopes), W.memTrigger, 'trigger');
    }
    if (kinds.has('turn')) {
      add('turn', this._fts('turns_uni', match.uni, N), W.turnUni, 'uni');
      add('turn', this._fts('turns_tri', match.tri, N), W.turnTri, 'tri');
      add('turn', this._fts('turns_skel', match.skel, N), W.turnSkel, 'skel');
    }
    if (kinds.has('episode')) {
      add('episode', this._fts('episodes_uni', match.uni, N), W.episodeUni, 'uni');
      add('episode', this._fts('episodes_skel', match.skel, N), W.episodeSkel, 'skel');
    }
    if (fused.size === 0) return { ...empty(), tokens: tokens.map((t) => t.fold) };

    const maxRrf = Math.max(...[...fused.values()].map((f) => f.rrf));
    const scored = [];
    for (const f of fused.values()) {
      const item = this._load(f, { now, mind, purpose, scopes, characterId: input.characterId, input });
      if (!item) continue;
      const cov = coverageOf(item._fold, item._skel);
      const triggered = f.channels.has('trigger');
      const coverage = triggered ? Math.max(cov.coverage, SEARCH.foundCoverage) : cov.coverage;
      const R = f.rrf / maxRrf;
      item.coverage = Number(coverage.toFixed(3));
      item.strong = cov.strong || triggered || f.channels.has('entity');
      item.channels = [...f.channels];
      item._gate = {
        // PAKKA: zyada-tar wazan mila, koi lafz pakka mila, aur (ek se zyada lafzon
        // wale sawal mein) kam se kam do lafz mile — ek aam lafz ka ittefaq "mil gaya" nahi.
        // Aur sawal ka koi lafz aisa na ho jo record mein kahin hai hi nahi: "Ali ke
        // restaurant ki kitni BRANCHES hain" — Ali aur restaurant milte hain, magar
        // "branches" kabhi kaha hi nahi gaya, to ye "pakka mila" nahi ho sakta.
        found: triggered || (coverage >= SEARCH.foundCoverage && item.strong && cov.absent === 0 && (cov.size === 1 || cov.matched >= 2)),
        // SHAYAD: koi kam-yaab lafz (naam, khaas cheez) mila, ya wazan ka kaafi hissa.
        weak: cov.rareMatched || coverage >= SEARCH.weakCoverage || f.channels.has('entity'),
      };
      // Yaadash user ke baare mein hai: us ka apna jumla assistant ke jawab se zara upar.
      const speakerBoost = item.kind === 'turn' && item.speaker === 'user' ? SEARCH.userTurnBoost : 1;
      // Tarteeb mein asal wazan "sawal ka kitna hissa mila" ka hai; channels ki
      // tarteeb (R) us ke andar farq karti hai. Sirf R par chalne se doosri script
      // wala sahi jumla (jo sirf skeleton se milta hai) hamesha neeche reh jata tha.
      const relevance = coverage * (SEARCH.coverageFloor + (1 - SEARCH.coverageFloor) * R);
      item.score = relevance * speakerBoost * (1 + SEARCH.activationWeight * item._A + SEARCH.salienceWeight * item._S) * item._C;
      scored.push(item);
    }
    scored.sort((a, b) => b.score - a.score);

    const ranked = this._dedupe(scored);
    const first = ranked[0];
    let status = 'NONE';
    if (first) {
      if (first._gate.found) status = 'FOUND';
      else if (first._gate.weak) status = 'WEAK';
    }
    // Bina pooche bolne ke liye darwaza sakht: kamzor yaad par chup rehna behtar.
    if (purpose === 'proactive' && !(first && first._gate.found && first.coverage >= SEARCH.proactiveCoverage)) status = 'NONE';

    // WEAK par sirf wo natije jate hain jin ke paas khud koi saboot hai — warna
    // pehle "shayad" wale natije ke peeche chaar be-taalluq jumle bhi model tak
    // pahunch jate aur wo un mein se kuch "yaad" kar leta.
    const kept = status === 'FOUND' || input.keepNone ? ranked : ranked.filter((it) => it._gate.weak || it._gate.found);
    const items = kept.slice(0, k).map((it) => {
      const { _fold, _skel, _A, _S, _C, _gate, ...rest } = it;
      return { ...rest, score: Number(it.score.toFixed(4)) };
    });

    return {
      status,
      // `keepNone` sirf nap-tol ke liye (scripts/memory-eval): NONE par bhi tarteeb
      // dikhti hai, taake pata chale ke kami talash ki hai ya darwaze ki.
      items: status === 'NONE' && !input.keepNone ? [] : items,
      tokens: tokens.map((t) => t.fold),
      tookMs: Date.now() - started,
    };
  }

  /** Wo yaadein jin ka trigger lafz sawal mein aaya — talash ke rehm par nahi. */
  _triggered(tokens, scopes) {
    const folds = new Set(tokens.map((t) => t.fold));
    const marks = scopes.map(() => '?').join(',');
    const rows = this.db.prepare(`select id, triggers from memories where deleted = 0 and status = 'active'
      and triggers != '[]' and scope in (${marks}) order by pinned desc, mentioned_at desc`).all(...scopes);
    const out = [];
    for (const r of rows) {
      if (parseJson(r.triggers, []).some((w) => folds.has(w))) out.push(Number(r.id));
    }
    return out;
  }

  /** Umeedwar ko kholna aur DARWAZON se guzarna. `null` = bahar. */
  _load(f, ctx) {
    const { now, mind, purpose, scopes, characterId, input } = ctx;
    if (f.kind === 'memory') {
      const m = this.store.getMemory(f.id);
      if (!m) return null;
      if (!scopes.includes(m.scope)) return null;
      if (m.status === 'retracted') return null;
      if (m.status === 'superseded' && !input.includePast) return null;
      // Andaza (candidate) sirf seedhe sawal par, aur kabhi bina pooche nahi.
      if (m.status === 'candidate' && purpose !== 'answer') return null;
      if (m.sensitivity === 'sensitive' && purpose === 'proactive') return null;
      const when = m.event_start || m.mentioned_at;
      if (input.since && when < input.since) return null;
      if (input.until && when > input.until) return null;
      const ev = this.store.q('select quote, turn_id from memory_evidence where memory_id = ? order by id limit 1').get(m.id);
      return {
        kind: 'memory', id: Number(m.id), uid: m.uid, type: m.type,
        text: m.text_original, textEn: m.text_en,
        when, whenText: whenText(when, now),
        origin: m.origin, status: m.status, pinned: !!m.pinned, sensitive: m.sensitivity === 'sensitive',
        confidence: m.confidence, quote: ev ? ev.quote : '', turnId: ev ? ev.turn_id : null,
        past: m.status === 'superseded',
        _fold: m.fold, _skel: m.skel,
        _A: this._activation([m.mentioned_at, m.recorded_at, ...this.store.activationTimes(m.id)], now),
        _S: this._salience(m),
        _C: m.status === 'candidate' ? Math.min(m.confidence, 0.7) : m.confidence,
      };
    }
    if (f.kind === 'turn') {
      const t = this.store.getTurn(f.id);
      if (!t) return null;
      if (t.flags & (TURN_FLAGS.automated | TURN_FLAGS.forgotten)) return null;
      // Baat-cheet har character ki apni hai; `hermes` nazariya (bina character) sab dekhta hai.
      if (mind !== 'hermes' && t.character_id && characterId && t.character_id !== characterId) return null;
      if (input.since && t.ts < input.since) return null;
      if (input.until && t.ts > input.until) return null;
      // Bina pooche raw baat-cheet nahi uthai jati — sirf pakki yaadein.
      if (purpose === 'proactive') return null;
      const window = this.store.getTurnWindow(t.id, SEARCH.turnWindow)
        .filter((w) => !(w.flags & (TURN_FLAGS.automated | TURN_FLAGS.forgotten)))
        .map((w) => ({ id: Number(w.id), speaker: w.speaker, text: w.text }));
      return {
        kind: 'turn', id: Number(t.id), text: t.text, speaker: t.speaker, channel: t.channel,
        sessionKey: t.session_id, characterId: t.character_id,
        when: t.ts, whenText: whenText(t.ts, now), context: window, confidence: 1,
        _fold: t.fold, _skel: t.skel,
        _A: this._activation([t.ts], now), _S: 0, _C: 1,
      };
    }
    if (f.kind === 'episode') {
      const e = this.store.getEpisode(f.id);
      if (!e) return null;
      if (mind !== 'hermes' && e.character_id && characterId && e.character_id !== characterId) return null;
      if (input.since && e.end_ts < input.since) return null;
      if (input.until && e.start_ts > input.until) return null;
      if (purpose === 'proactive') return null;
      return {
        kind: 'episode', id: Number(e.id), uid: e.uid, text: e.title, summary: e.summary_en,
        sessionKey: e.session_id, firstTurn: e.first_turn, lastTurn: e.last_turn,
        when: e.end_ts, whenText: whenText(e.end_ts, now), confidence: 0.9,
        _fold: e.fold, _skel: e.skel,
        _A: this._activation([e.end_ts], now), _S: Number(e.salience) || 0, _C: 0.9,
      };
    }
    return null;
  }

  /**
   * Ek hi baat do shaklon mein na aaye: yaad aur us ka apna saboot-turn, ya ek
   * hi baat-cheet ke aage peeche ke turns. Pehla (behtar score wala) rehta hai.
   */
  _dedupe(sorted) {
    const out = [];
    const turnsTaken = new Set();
    for (const it of sorted) {
      if (it.kind === 'memory') {
        if (it.turnId) turnsTaken.add(it.turnId);
        out.push(it);
        continue;
      }
      if (it.kind === 'turn') {
        const near = it.context.map((c) => c.id);
        if (near.some((id) => turnsTaken.has(id))) continue;
        near.forEach((id) => turnsTaken.add(id));
        out.push(it);
        continue;
      }
      out.push(it);
    }
    return out;
  }
}

module.exports = { MemorySearch, stemOf };
