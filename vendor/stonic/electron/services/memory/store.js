// Memory Core ka store — likhne aur parhne ka WAHID darwaza.
//
// ════════════════════════════════════════════════════════════════════════════
//  IS FILE KA USOOL
// ════════════════════════════════════════════════════════════════════════════
//
//  Database mein koi bhi cheez yahin se likhi jati hai. Talash (search.js),
//  nichor (digest.js) aur service — sab isi ke functions bulate hain, koi
//  apni `insert` nahi likhta. Wajah: har likhayi ke saath chaar kaam LAZMI
//  hain, aur wo ek hi jagah hon to bhoole nahi jate:
//
//    1. raaz nikalna        (secrets.js — password, OTP, card)
//    2. talash ki shaklein  (normalize.js — fold aur skel)
//    3. hisaab likhna       (memory_events — kis ne, kya, kyun)
//    4. haddein             (config.js — pin ki ginti, lambai)
//
//  Ye file Electron ke baghair hai: `db` aur `now` bahar se aate hain.
//
// ── TEEN BAATEIN JO YAHAN KABHI NAHI HOTIN ──────────────────────────────────
//
//  - Yaad MIT-TI nahi (siwaye user ke "poori tarah mita do" ke). Badle to
//    mansookh, ghalat ho to retract — dono history mein rehte hain.
//  - Hadd par chup-chaap kat-ta nahi. Pin ki hadd par likhna RAD hota hai aur
//    wajah wapas jati hai, taake user ko pata chale.
//  - "Bhool jao" ke baad wohi baat dobara nahi ban sakti (tombstone) — jab tak
//    user khud dobara na kahe.

const crypto = require('crypto');
const { normalizeForSearch, foldText, skeleton, contentHash, queryTokens } = require('./normalize');
const { redactSecrets } = require('./secrets');
const {
  LIMITS, MEMORY_TYPES, MEMORY_STATUSES, ORIGINS, CHANNELS, SPEAKERS,
  TURN_FLAGS, AUTOMATED_PREFIXES, DIGEST,
} = require('./config');

const uid = (prefix) => `${prefix}_${crypto.randomBytes(8).toString('hex')}`;
const clip = (text, max) => (text.length > max ? text.slice(0, max) : text);
const clean = (text) => String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
const parseJson = (raw, fallback) => {
  try { const v = JSON.parse(raw); return v == null ? fallback : v; } catch { return fallback; }
};
const sessionKey = (channel, sessionId) => `${channel}:${sessionId}`;
/**
 * Do jumlon ke kaam ke lafzon ka mel (0..1), chhote jumle ke hisaab se:
 * "payouts check karna" poora ka poora "Usman ko payouts check karne ka yaad
 * dilana" ke andar hai. Spelling ka farq fold hata deta hai. Ek lafz ka jumla
 * sirf apne jaise ek lafz ke jumle se milta hai — akela "call" har call wale
 * waade se na mil jaye.
 */
function wordOverlap(a, b) {
  const A = new Set(queryTokens(a).map((t) => t.fold));
  const B = new Set(queryTokens(b).map((t) => t.fold));
  if (!A.size || !B.size) return 0;
  let hits = 0;
  for (const w of A) if (B.has(w)) hits += 1;
  if (Math.min(A.size, B.size) < 2) return A.size === B.size && hits === A.size ? 1 : 0;
  return hits / Math.min(A.size, B.size);
}

/** Ek character ko kaun si yaadein nazar aati hain: sab ki mushtarak + us ki apni. */
const scopesFor = (characterId) => (characterId ? ['user', `character:${characterId}`] : ['user']);

class MemoryStore {
  /**
   * @param {object} db  better-sqlite3 ka khula database (db.js se)
   * @param {{ now?: () => number }} [opts]
   */
  constructor(db, opts = {}) {
    this.db = db;
    this.now = opts.now || (() => Date.now());
    this._stmt = new Map();
  }

  /** Statement ek dafa banta hai, phir wohi baar baar chalta hai. */
  q(sql) {
    let s = this._stmt.get(sql);
    if (!s) { s = this.db.prepare(sql); this._stmt.set(sql, s); }
    return s;
  }

  // ── meta ──────────────────────────────────────────────────────────────────

  getMeta(key, fallback = null) {
    const row = this.q('select value from meta where key = ?').get(key);
    return row ? row.value : fallback;
  }

  setMeta(key, value) {
    this.q('insert into meta(key, value) values (?, ?) on conflict(key) do update set value = excluded.value')
      .run(key, String(value));
  }

  /** Pause: na kuch likha jata hai, na kuch yaad ke taur par diya jata hai. */
  isPaused() { return this.getMeta('paused', '0') === '1'; }

  setPaused(paused, actor = 'user') {
    this.setMeta('paused', paused ? '1' : '0');
    this.audit(actor, paused ? 'pause' : 'resume', 'store', null, null, null, null);
  }

  // ── hisaab (audit) ────────────────────────────────────────────────────────

  audit(actor, op, targetKind, targetId, before, after, cause) {
    const pack = (v) => (v == null ? null : clip(typeof v === 'string' ? v : JSON.stringify(v), 2000));
    this.q(`insert into memory_events(ts, actor, op, target_kind, target_id, before, after, cause)
      values (?, ?, ?, ?, ?, ?, ?, ?)`)
      .run(this.now(), String(actor || 'system'), op, targetKind, targetId == null ? null : String(targetId),
        pack(before), pack(after), cause ? clip(String(cause), 500) : null);
  }

  listAudit({ limit = 100, targetKind = null, targetId = null } = {}) {
    if (targetKind && targetId != null) {
      return this.q('select * from memory_events where target_kind = ? and target_id = ? order by id desc limit ?')
        .all(targetKind, String(targetId), limit);
    }
    return this.q('select * from memory_events order by id desc limit ?').all(limit);
  }

  // ── sessions aur turns (ASAL RECORD) ──────────────────────────────────────

  /**
   * Baat-cheet ke jumle likhta hai. Wohi message dobara aaye to dobara nahi
   * likha jata (`sourceRef`); agar wohi message LAMBA ho kar aaye (adhoora
   * transcript mukammal hua) to purana badh jata hai.
   *
   * @param {{
   *   channel: string, sessionId: string, characterId?: string|null, title?: string|null,
   *   external?: boolean,
   *   turns: Array<{ speaker: string, text: string, ts?: number, sourceRef?: string|null, flags?: number }>
   * }} input
   * @returns {{ ok: boolean, paused?: boolean, sessionKey: string, added: number, updated: number,
   *   skipped: number, redacted: number, turnIds: number[] }}
   */
  appendTurns(input) {
    const channel = CHANNELS.includes(input && input.channel) ? input.channel : null;
    const sessionId = clean(input && input.sessionId);
    const out = { ok: false, sessionKey: '', added: 0, updated: 0, skipped: 0, redacted: 0, turnIds: [] };
    if (!channel || !sessionId) return out;
    out.sessionKey = sessionKey(channel, sessionId);
    if (this.isPaused()) return { ...out, ok: true, paused: true };

    const turns = Array.isArray(input.turns) ? input.turns : [];
    const characterId = input.characterId ? clean(input.characterId).slice(0, 80) : null;
    const now = this.now();

    const run = this.db.transaction(() => {
      let lastTs = 0;
      let firstTs = 0;
      for (const t of turns) {
        const speaker = SPEAKERS.includes(t && t.speaker) ? t.speaker : null;
        const raw = String((t && t.text) == null ? '' : t.text).trim();
        if (!speaker || !raw) { out.skipped += 1; continue; }

        let flags = Number(t.flags) || 0;
        const red = redactSecrets(raw);
        let text = red.text;
        if (red.found.length) { flags |= TURN_FLAGS.redacted; out.redacted += red.found.length; }
        if (text.length > LIMITS.turnMaxChars) { text = text.slice(0, LIMITS.turnMaxChars); flags |= TURN_FLAGS.truncated; }
        if (AUTOMATED_PREFIXES.some((p) => text.startsWith(p))) flags |= TURN_FLAGS.automated;
        if (input.external) flags |= TURN_FLAGS.external;

        const ts = Number(t.ts) > 0 ? Number(t.ts) : now;
        const ref = t.sourceRef ? clean(t.sourceRef).slice(0, 200) : null;
        const norm = normalizeForSearch(text);

        if (!firstTs || ts < firstTs) firstTs = ts;
        if (ts > lastTs) lastTs = ts;
        this._ensureSession(out.sessionKey, channel, characterId, input.title, ts);

        if (ref) {
          const existing = this.q('select id, text from turns where channel = ? and source_ref = ?').get(channel, ref);
          if (existing) {
            if (existing.text !== text && text.length > existing.text.length) {
              this.q('update turns set text = ?, fold = ?, skel = ?, script = ?, flags = flags | ? where id = ?')
                .run(text, norm.fold, norm.skel, norm.script, flags, existing.id);
              out.updated += 1;
              out.turnIds.push(Number(existing.id));
            } else {
              out.skipped += 1;
            }
            continue;
          }
        }
        const info = this.q(`insert into turns(session_id, ts, speaker, channel, character_id, text, fold, skel, script, source_ref, flags, created_at)
          values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
          .run(out.sessionKey, ts, speaker, channel, characterId, text, norm.fold, norm.skel, norm.script, ref, flags, now);
        out.added += 1;
        out.turnIds.push(Number(info.lastInsertRowid));
      }
      if (out.added) {
        this.q(`update sessions set turn_count = turn_count + ?, last_turn_at = max(last_turn_at, ?),
          external_content = max(external_content, ?), character_id = coalesce(?, character_id), ended_at = null
          where id = ?`)
          .run(out.added, lastTs || now, input.external ? 1 : 0, characterId, out.sessionKey);
      }
    });
    run();
    out.ok = true;
    return out;
  }

  _ensureSession(key, channel, characterId, title, ts) {
    this.q(`insert into sessions(id, channel, character_id, title, started_at, last_turn_at)
      values (?, ?, ?, ?, ?, ?) on conflict(id) do nothing`)
      .run(key, channel, characterId, title ? clip(clean(title), 200) : null, ts, ts);
  }

  /** Baat-cheet khatam: ab is ka nichor nikal sakta hai (khamoshi ke baad). */
  endSession(channel, sessionId) {
    const key = sessionKey(channel, sessionId);
    this.q('update sessions set ended_at = ? where id = ?').run(this.now(), key);
    return key;
  }

  getSession(key) { return this.q('select * from sessions where id = ?').get(key) || null; }

  listSessions({ limit = 50, offset = 0, channel = null } = {}) {
    if (channel) {
      return this.q('select * from sessions where channel = ? order by last_turn_at desc limit ? offset ?').all(channel, limit, offset);
    }
    return this.q('select * from sessions order by last_turn_at desc limit ? offset ?').all(limit, offset);
  }

  getTurns(key, { afterId = 0, limit = 2000 } = {}) {
    return this.q('select * from turns where session_id = ? and id > ? order by id limit ?').all(key, afterId, limit);
  }

  getTurn(id) { return this.q('select * from turns where id = ?').get(id) || null; }

  /** Ek turn ke aage peeche ke jumle — usi baat-cheet ke andar. */
  getTurnWindow(turnId, radius = 1) {
    const turn = this.getTurn(turnId);
    if (!turn) return [];
    const before = this.q('select * from turns where session_id = ? and id < ? order by id desc limit ?')
      .all(turn.session_id, turn.id, radius).reverse();
    const after = this.q('select * from turns where session_id = ? and id > ? order by id limit ?')
      .all(turn.session_id, turn.id, radius);
    return [...before, turn, ...after];
  }

  /** Sessions jin ka nichor baqi hai aur jo `quietMs` se khamosh hain. */
  sessionsNeedingDigest(now = this.now(), quietMs = DIGEST.quietMs) {
    return this.q(`select s.* from sessions s
      where s.last_turn_at <= ? and s.digest_attempts < ?
        and exists (select 1 from turns t where t.session_id = s.id and t.id > s.digested_upto)
      order by s.last_turn_at`).all(now - quietMs, DIGEST.maxAttempts);
  }

  markDigested(key, uptoTurnId) {
    this.q('update sessions set digested_upto = max(digested_upto, ?), digest_attempts = 0 where id = ?').run(uptoTurnId, key);
  }

  bumpDigestAttempt(key) {
    this.q('update sessions set digest_attempts = digest_attempts + 1 where id = ?').run(key);
  }

  // ── entities (log, jagahein, projects) ────────────────────────────────────

  /**
   * Naam ya kisi doosre naam se mile to wohi entity, warna nayi.
   * @param {{ name: string, type?: string, aliases?: string[] }} input
   * @returns {number|null} entity id
   */
  upsertEntity(input) {
    const name = clip(clean(input && input.name), 80);
    if (!name) return null;
    const names = [name, ...((input.aliases || []).map((a) => clip(clean(a), 80)).filter(Boolean))];
    const folds = [...new Set(names.map((n) => foldText(n)).filter(Boolean))];
    if (folds.length === 0) return null;

    let id = null;
    for (const f of folds) {
      const row = this.q('select entity_id from entity_aliases where alias_fold = ? limit 1').get(f);
      if (row) { id = Number(row.entity_id); break; }
    }
    if (id == null) {
      const info = this.q('insert into entities(uid, canonical_name, type, created_at) values (?, ?, ?, ?)')
        .run(uid('e'), name, clean(input.type) || 'other', this.now());
      id = Number(info.lastInsertRowid);
    }
    for (const n of names) {
      const f = foldText(n);
      if (!f) continue;
      this.q('insert or ignore into entity_aliases(entity_id, alias_fold, alias_skel) values (?, ?, ?)').run(id, f, skeleton(n));
    }
    return id;
  }

  entityNames(memoryId) {
    return this.q(`select e.canonical_name as name, a.alias_fold as alias from memory_entities me
      join entities e on e.id = me.entity_id
      left join entity_aliases a on a.entity_id = e.id
      where me.memory_id = ?`).all(memoryId);
  }

  /** In fold-shuda lafzon mein se jo kisi entity ka naam hain, un entities ki ids. */
  entitiesByAliases(folds, skels = []) {
    const ids = new Set();
    for (const f of folds) {
      for (const r of this.q('select entity_id from entity_aliases where alias_fold = ?').all(f)) ids.add(Number(r.entity_id));
    }
    for (const s of skels) {
      if (!s || s.length < 3) continue; // chhota skeleton bohat se lafzon se takrata hai
      for (const r of this.q('select entity_id from entity_aliases where alias_skel = ?').all(s)) ids.add(Number(r.entity_id));
    }
    return [...ids];
  }

  memoriesForEntities(entityIds, limit = 30) {
    if (!entityIds.length) return [];
    const marks = entityIds.map(() => '?').join(',');
    return this.db.prepare(`select distinct m.id from memories m join memory_entities me on me.memory_id = m.id
      where me.entity_id in (${marks}) and m.deleted = 0 order by m.mentioned_at desc limit ?`).all(...entityIds, limit).map((r) => Number(r.id));
  }

  // ── memories ──────────────────────────────────────────────────────────────

  _searchText(text, textEn, triggers, memoryId) {
    const extra = memoryId ? this.entityNames(memoryId).flatMap((r) => [r.name, r.alias]).filter(Boolean) : [];
    const joined = [text, textEn, ...triggers, ...extra].filter(Boolean).join(' ');
    return normalizeForSearch(joined);
  }

  _normTriggers(list) {
    const out = [];
    for (const w of Array.isArray(list) ? list : []) {
      const f = foldText(clean(w));
      if (f && f.length >= 2 && !out.includes(f)) out.push(f);
      if (out.length >= LIMITS.triggerWordsPerMemory) break;
    }
    return out;
  }

  pinnedUsage() {
    const row = this.q(`select count(*) as n, coalesce(sum(length(text_original)), 0) as chars from memories
      where pinned = 1 and deleted = 0 and status = 'active'`).get();
    return { count: Number(row.n), chars: Number(row.chars), max: LIMITS.pinnedMax, maxChars: LIMITS.pinnedMaxChars };
  }

  _pinRoom(extraChars) {
    const use = this.pinnedUsage();
    if (use.count + 1 > use.max) return `pin_limit_count:${use.max}`;
    if (use.chars + extraChars > use.maxChars) return `pin_limit_chars:${use.maxChars}`;
    return null;
  }

  /**
   * Nayi yaad. Wohi baat pehle se ho to nayi nahi banti — purani mazboot hoti hai.
   *
   * @returns {{ ok: boolean, id?: number, uid?: string, duplicateOf?: number, reason?: string }}
   *   reason: 'empty' | 'too_long' | 'secret_only' | 'forgotten' | 'pin_limit_count:N' | 'pin_limit_chars:N'
   */
  addMemory(input) {
    const actor = input.actor || 'system';
    const red = redactSecrets(clean(input.text));
    const text = red.text;
    if (!text) return { ok: false, reason: 'empty' };
    // Hadd se lambi yaad kaati nahi jati: aadhi kati yaad ghalat matlab de sakti
    // hai aur likhne wale ko pata bhi nahi chalta. Rad — bulane wala wajah batata hai.
    if (text.length > LIMITS.memoryMaxChars) return { ok: false, reason: 'too_long' };
    // Poori "yaad" hi raaz thi (sirf password) — rakhne ko kuch nahi bacha.
    if (/^(\[redacted [a-z_]+\]\s*)+$/.test(text)) return { ok: false, reason: 'secret_only' };

    const textEn = clip(redactSecrets(clean(input.textEn)).text, LIMITS.memoryMaxChars);
    const type = MEMORY_TYPES.includes(input.type) ? input.type : 'other';
    const origin = ORIGINS.includes(input.origin) ? input.origin : 'inferred';
    const scope = clean(input.scope) || 'user';
    const status = MEMORY_STATUSES.includes(input.status) ? input.status : (origin === 'inferred' ? 'candidate' : 'active');
    const hash = contentHash(text);
    const now = this.now();
    const explicit = origin === 'manual' || origin === 'stated' || origin === 'assistant_tool';

    if (this.isTombstoned(hash)) {
      // "Bhool jao" kaha tha. Sirf user ka apna dobara kehna isay wapas la sakta hai.
      if (!(explicit && input.userRequested)) return { ok: false, reason: 'forgotten' };
      this.q('delete from tombstones where content_hash = ?').run(hash);
    }

    const dup = this.q(`select id, status from memories where content_hash = ? and scope = ? and deleted = 0
      and status in ('active', 'candidate') order by id limit 1`).get(hash, scope);
    if (dup) {
      this.reinforce(Number(dup.id), { evidence: input.evidence, sourceSession: input.sourceSession, promote: status === 'active' });
      if (input.pinned) this.setPinned(Number(dup.id), true, { actor });
      return { ok: true, id: Number(dup.id), duplicateOf: Number(dup.id) };
    }

    if (input.pinned) {
      const full = this._pinRoom(text.length);
      if (full) return { ok: false, reason: full };
    }

    const triggers = this._normTriggers(input.triggers);
    const norm = this._searchText(text, textEn, triggers, null);
    const sal = input.salience || {};
    const id = this.db.transaction(() => {
      const info = this.q(`insert into memories(uid, type, text_original, text_en, fold, skel, origin, trust_tier, confidence,
          evidence_count, session_count, scope, sensitivity, status, pinned, triggers, event_start, event_end,
          mentioned_at, recorded_at, valid_from, sal_explicit, sal_future, sal_emotion, sal_surprise,
          content_hash, source_session, created_by, prompt_version, updated_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
        .run(uid('m'), type, text, textEn, norm.fold, norm.skel, origin,
          Number(input.trustTier) || (origin === 'inferred' ? 2 : 1),
          Number.isFinite(input.confidence) ? Math.max(0, Math.min(1, input.confidence)) : (origin === 'inferred' ? 0.6 : 1),
          scope, input.sensitivity === 'sensitive' ? 'sensitive' : 'normal', status, input.pinned ? 1 : 0,
          JSON.stringify(triggers), input.eventStart || null, input.eventEnd || null,
          input.mentionedAt || now, now, input.validFrom || input.mentionedAt || now,
          Number(sal.explicit) || (explicit ? 1 : 0), Number(sal.future) || 0, Number(sal.emotion) || 0, Number(sal.surprise) || 0,
          hash, input.sourceSession || null, input.createdBy || actor, input.promptVersion || null, now);
      const memoryId = Number(info.lastInsertRowid);
      this._addEvidence(memoryId, input.evidence);
      if (Array.isArray(input.entities) && input.entities.length) {
        for (const e of input.entities) {
          const entityId = this.upsertEntity(e);
          if (entityId != null) this.q('insert or ignore into memory_entities(memory_id, entity_id) values (?, ?)').run(memoryId, entityId);
        }
        // Naam ab talash ke text ka hissa hain — "Ammi" likhne par "Mother" wali yaad mile.
        const withNames = this._searchText(text, textEn, triggers, memoryId);
        this.q('update memories set fold = ?, skel = ? where id = ?').run(withNames.fold, withNames.skel, memoryId);
      }
      this.audit(actor, 'add', 'memory', memoryId, null, { type, text, origin, status, scope, pinned: !!input.pinned }, input.cause);
      return memoryId;
    })();
    return { ok: true, id, uid: this.getMemory(id).uid };
  }

  _addEvidence(memoryId, evidence) {
    for (const ev of Array.isArray(evidence) ? evidence : []) {
      const turn = ev && ev.turnId ? this.getTurn(ev.turnId) : null;
      this.q('insert into memory_evidence(memory_id, turn_id, session_id, quote, created_at) values (?, ?, ?, ?, ?)')
        .run(memoryId, turn ? turn.id : null, turn ? turn.session_id : (ev && ev.sessionId) || null,
          clip(clean(ev && ev.quote), 400), this.now());
    }
  }

  /**
   * Wohi baat dobara kahi gayi: saboot barhta hai. Naye session se aaye to
   * `session_count` bhi — aur kaafi sessions par andaza PAKKA ho jata hai.
   */
  reinforce(id, { evidence = [], sourceSession = null, promote = false } = {}) {
    const m = this.getMemory(id);
    if (!m) return false;
    const sessions = new Set(this.q('select distinct session_id from memory_evidence where memory_id = ? and session_id is not null')
      .all(id).map((r) => r.session_id));
    if (m.source_session) sessions.add(m.source_session);
    const before = sessions.size;
    this._addEvidence(id, evidence);
    for (const ev of evidence || []) {
      const turn = ev && ev.turnId ? this.getTurn(ev.turnId) : null;
      if (turn) sessions.add(turn.session_id);
    }
    if (sourceSession) sessions.add(sourceSession);
    const sessionCount = Math.max(Number(m.session_count) || 1, sessions.size, before);
    const shouldPromote = m.status === 'candidate' && (promote || sessionCount >= DIGEST.promoteAfterSessions);
    const confidence = shouldPromote ? Math.max(m.confidence, 0.9) : Math.min(0.9, m.confidence + 0.1);
    this.q(`update memories set evidence_count = evidence_count + 1, session_count = ?, mentioned_at = ?,
      confidence = ?, status = ?, updated_at = ? where id = ?`)
      .run(sessionCount, this.now(), m.origin === 'inferred' ? confidence : m.confidence,
        shouldPromote ? 'active' : m.status, this.now(), id);
    if (shouldPromote) this.audit('system', 'promote', 'memory', id, { status: 'candidate' }, { status: 'active' }, 'evidence');
    return true;
  }

  getMemory(id) { return this.q('select * from memories where id = ? and deleted = 0').get(id) || null; }

  getMemoryByUid(memoryUid) { return this.q('select * from memories where uid = ? and deleted = 0').get(memoryUid) || null; }

  /** Chhoti id (uid ke pehle huroof) se — prompt mein poori id mehngi parti hai. */
  findMemoryByShortId(short) {
    const s = clean(short).replace(/[^a-z0-9_]/gi, '');
    if (s.length < 6) return null;
    const like = s.startsWith('m_') ? `${s}%` : `m_${s}%`;
    const rows = this.q('select * from memories where uid like ? and deleted = 0 limit 2').all(like);
    return rows.length === 1 ? rows[0] : null;
  }

  getEvidence(memoryId) {
    return this.q(`select ev.*, t.text as turn_text, t.ts as turn_ts, t.speaker as turn_speaker
      from memory_evidence ev left join turns t on t.id = ev.turn_id where ev.memory_id = ? order by ev.id`).all(memoryId);
  }

  /**
   * @param {{ status?: string|string[], type?: string, scopes?: string[], pinned?: boolean,
   *   limit?: number, offset?: number, order?: 'recent'|'type' }} [filter]
   */
  listMemories(filter = {}) {
    const where = ['deleted = 0'];
    const args = [];
    const statuses = [].concat(filter.status || ['active', 'candidate']);
    where.push(`status in (${statuses.map(() => '?').join(',')})`); args.push(...statuses);
    if (filter.type) { where.push('type = ?'); args.push(filter.type); }
    if (filter.scopes && filter.scopes.length) {
      where.push(`scope in (${filter.scopes.map(() => '?').join(',')})`); args.push(...filter.scopes);
    }
    if (filter.pinned != null) { where.push('pinned = ?'); args.push(filter.pinned ? 1 : 0); }
    const order = filter.order === 'type' ? 'type, pinned desc, mentioned_at desc' : 'pinned desc, mentioned_at desc';
    return this.db.prepare(`select * from memories where ${where.join(' and ')} order by ${order} limit ? offset ?`)
      .all(...args, Math.min(Number(filter.limit) || 200, 1000), Number(filter.offset) || 0);
  }

  /**
   * Yaad ka text ya khasoosiyat badalna (user ka edit). Text badle to hash,
   * talash ki shaklein aur index sab saath badalte hain.
   */
  updateMemory(id, patch, { actor = 'user', cause = null } = {}) {
    const m = this.getMemory(id);
    if (!m) return { ok: false, reason: 'not_found' };
    const text = patch.text != null ? redactSecrets(clean(patch.text)).text : m.text_original;
    if (!text) return { ok: false, reason: 'empty' };
    if (patch.text != null && text.length > LIMITS.memoryMaxChars) return { ok: false, reason: 'too_long' };
    const textEn = patch.textEn != null ? clip(clean(patch.textEn), LIMITS.memoryMaxChars) : m.text_en;
    const triggers = patch.triggers != null ? this._normTriggers(patch.triggers) : parseJson(m.triggers, []);
    const type = MEMORY_TYPES.includes(patch.type) ? patch.type : m.type;
    const sensitivity = patch.sensitivity === 'sensitive' || patch.sensitivity === 'normal' ? patch.sensitivity : m.sensitivity;
    const scope = patch.scope != null ? (clean(patch.scope) || 'user') : m.scope;
    const norm = this._searchText(text, textEn, triggers, id);
    this.q(`update memories set text_original = ?, text_en = ?, fold = ?, skel = ?, type = ?, triggers = ?,
      sensitivity = ?, scope = ?, content_hash = ?, updated_at = ?,
      origin = case when ? then 'manual' else origin end,
      status = case when ? and status = 'candidate' then 'active' else status end,
      confidence = case when ? then 1.0 else confidence end
      where id = ?`)
      .run(text, textEn, norm.fold, norm.skel, type, JSON.stringify(triggers), sensitivity, scope, contentHash(text), this.now(),
        actor === 'user' ? 1 : 0, actor === 'user' ? 1 : 0, actor === 'user' ? 1 : 0, id);
    this.audit(actor, 'update', 'memory', id, { text: m.text_original, type: m.type, triggers: m.triggers },
      { text, type, triggers }, cause);
    return { ok: true, id };
  }

  /** @returns {{ ok: boolean, reason?: string }} hadd par `ok:false` aur wajah. */
  setPinned(id, pinned, { actor = 'user' } = {}) {
    const m = this.getMemory(id);
    if (!m) return { ok: false, reason: 'not_found' };
    if (!!m.pinned === !!pinned) return { ok: true };
    if (pinned) {
      if (m.status !== 'active') return { ok: false, reason: 'not_active' };
      const full = this._pinRoom(m.text_original.length);
      if (full) return { ok: false, reason: full };
    }
    this.q('update memories set pinned = ?, updated_at = ? where id = ?').run(pinned ? 1 : 0, this.now(), id);
    this.audit(actor, pinned ? 'pin' : 'unpin', 'memory', id, null, null, null);
    return { ok: true };
  }

  /**
   * Baat BADAL gayi (ghalat nahi thi): purani mansookh, nayi us ki jagah.
   * History mein dono rehti hain — "pehle kahan rehta tha" ka jawab purani deti hai.
   */
  supersede(oldId, input, { actor = 'system', cause = null } = {}) {
    const old = this.getMemory(oldId);
    if (!old) return this.addMemory({ ...input, actor, cause });
    const made = this.addMemory({
      scope: old.scope, type: old.type, sensitivity: old.sensitivity, triggers: parseJson(old.triggers, []),
      ...input, actor, cause, pinned: false,
    });
    if (!made.ok || made.id === oldId) return made;
    const now = this.now();
    this.db.transaction(() => {
      this.q(`update memories set status = 'superseded', valid_to = ?, superseded_by = ?, pinned = 0, updated_at = ? where id = ?`)
        .run(now, made.id, now, oldId);
      // Pin purani se nayi par chala jata hai — user ne BAAT ko pin kiya tha, us ke lafzon ko nahi.
      if (old.pinned) this.q('update memories set pinned = 1 where id = ?').run(made.id);
      this.audit(actor, 'supersede', 'memory', oldId, { text: old.text_original }, { by: made.id }, cause);
    })();
    return made;
  }

  /** Purani yaad ka bharosa ghatana (takrao mila magar faisla nahi ho saka). */
  weaken(id, amount = 0.2, { actor = 'system', cause = null } = {}) {
    const m = this.getMemory(id);
    if (!m) return false;
    const next = Math.max(0.1, m.confidence - amount);
    this.q('update memories set confidence = ?, updated_at = ? where id = ?').run(next, this.now(), id);
    this.audit(actor, 'weaken', 'memory', id, { confidence: m.confidence }, { confidence: next }, cause);
    return true;
  }

  /**
   * Bhool jana.
   *   soft (default): `retracted` — talash aur profile se bahar, history mein hai.
   *   hard: yaad, us ke saboot, aur asal record mein us ka jumla — sab mit-ta hai.
   * Dono mein tombstone lagta hai taake agla nichor ye baat dobara na seekh le.
   */
  forgetMemory(id, { hard = false, actor = 'user', cause = null } = {}) {
    const m = this.getMemory(id);
    if (!m) return { ok: false, reason: 'not_found' };
    const now = this.now();
    this.db.transaction(() => {
      this.q('insert or ignore into tombstones(content_hash, created_at, note) values (?, ?, ?)').run(m.content_hash, now, hard ? 'hard' : 'soft');
      if (!hard) {
        // Jis jumle se ye yaad bani thi wo bhi chhup jata hai — warna baat kachi
        // baat-cheet ki talash se wapas aa jati.
        this.hideTurns(this.getEvidence(id).map((ev) => ev.turn_id).filter(Boolean));
        this.q(`update memories set status = 'retracted', pinned = 0, valid_to = ?, updated_at = ? where id = ?`).run(now, now, id);
        this.audit(actor, 'forget', 'memory', id, { text: m.text_original }, { status: 'retracted' }, cause);
        return;
      }
      for (const ev of this.getEvidence(id)) {
        if (!ev.turn_id || !ev.quote) continue;
        this._redactInTurn(ev.turn_id, ev.quote);
      }
      this.q('delete from memories where id = ?').run(id);
      // Hard forget ka hisaab bhi rehta hai — magar us mein mita hua text NAHI.
      this.audit(actor, 'hard_forget', 'memory', id, null, null, cause);
    })();
    return { ok: true, hard };
  }

  /** "Bhool jao" wale jumle: record mein rehte hain, magar koi unhein dhoondh ya dikha nahi sakta. */
  hideTurns(turnIds) {
    for (const id of turnIds) this.q('update turns set flags = flags | ? where id = ?').run(TURN_FLAGS.forgotten, id);
    return turnIds.length;
  }

  /** Asal record mein se ek jumla nikalna (sirf hard forget ke liye). */
  _redactInTurn(turnId, quote) {
    const turn = this.getTurn(turnId);
    if (!turn) return false;
    const mark = '[removed by user]';
    let text = turn.text;
    const at = text.toLowerCase().indexOf(String(quote).toLowerCase());
    // Quote hoo-ba-hoo na mile (model ne lafz thore badle) to poora turn hat jata
    // hai: user ne "poori tarah mita do" kaha hai, adhoora mitana us se bura hai.
    text = at >= 0 ? `${text.slice(0, at)}${mark}${text.slice(at + String(quote).length)}` : mark;
    const norm = normalizeForSearch(text);
    this.q('update turns set text = ?, fold = ?, skel = ?, flags = flags | ? where id = ?')
      .run(text, norm.fold, norm.skel, TURN_FLAGS.redacted, turnId);
    return true;
  }

  isTombstoned(hash) { return !!this.q('select 1 from tombstones where content_hash = ?').get(hash); }

  /** Yaad nikli (aur kaam aayi ya nahi). `useful_count` sirf kaam aane par barhta hai. */
  markUsed(ids, mind, useful = false) {
    const now = this.now();
    const run = this.db.transaction(() => {
      for (const id of ids) {
        this.q('insert into access_log(memory_id, ts, mind, useful) values (?, ?, ?, ?)').run(id, now, mind, useful ? 1 : 0);
        if (useful) this.q('update memories set useful_count = useful_count + 1, last_used_at = ? where id = ?').run(now, id);
      }
    });
    run();
  }

  /** Activation ke liye waqt: bani, dobara zikr hui, aur har dafa jab kaam aayi. */
  activationTimes(memoryId, limit = 12) {
    return this.q('select ts from access_log where memory_id = ? and useful = 1 order by ts desc limit ?').all(memoryId, limit).map((r) => Number(r.ts));
  }

  // ── intentions (waade aur follow-ups) ─────────────────────────────────────

  addIntention(input) {
    const what = clip(redactSecrets(clean(input.what)).text, 300);
    if (!what) return { ok: false, reason: 'empty' };
    const hash = contentHash(what);
    const dup = this.q(`select id from intentions where content_hash = ? and status = 'open' limit 1`).get(hash);
    if (dup) return { ok: true, id: Number(dup.id), duplicateOf: Number(dup.id) };
    const now = this.now();
    const info = this.q(`insert into intentions(uid, what, what_en, who_for, cue_type, cue_text, due_ts, expiry_ts, source_turn,
        source_session, quote, confidence, created_by, character_id, scope, created_at, content_hash)
      values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
      .run(uid('i'), what, clip(clean(input.whatEn), 300), input.whoFor ? clip(clean(input.whoFor), 80) : null,
        ['time', 'event', 'context'].includes(input.cueType) ? input.cueType : 'time',
        input.cueText ? clip(clean(input.cueText), 200) : null,
        input.dueTs || null, input.expiryTs || null, input.sourceTurn || null, input.sourceSession || null,
        clip(clean(input.quote), 400), Number.isFinite(input.confidence) ? input.confidence : 1,
        input.createdBy || 'system', input.characterId || null, clean(input.scope) || 'user', now, hash);
    const id = Number(info.lastInsertRowid);
    this.audit(input.actor || input.createdBy || 'system', 'add', 'intention', id, null, { what, due: input.dueTs || null }, input.cause);
    return { ok: true, id };
  }

  getIntention(id) { return this.q('select * from intentions where id = ?').get(id) || null; }

  /** Chhoti id (uid ke pehle huroof) se — nichor ka model isi se waada pehchanta hai. */
  findIntentionByShortId(short) {
    const s = clean(short).replace(/[^a-z0-9_]/gi, '');
    if (s.length < 6) return null;
    const rows = this.q('select * from intentions where uid like ? limit 2').all(s.startsWith('i_') ? `${s}%` : `i_${s}%`);
    return rows.length === 1 ? rows[0] : null;
  }

  /** Is baat-cheet mein `createdBy` ke likhe waade — har halat ke (khule, poore, hataye hue). */
  intentionsFromSession(sessionKey, createdBy) {
    return this.q('select * from intentions where source_session = ? and created_by = ? order by id').all(sessionKey, createdBy);
  }

  listIntentions({ status = 'open', limit = 100 } = {}) {
    return this.q(`select * from intentions where status = ? order by coalesce(due_ts, created_at) limit ?`).all(status, limit);
  }

  /**
   * Wo waade jo ab saamne aane chahiyen: jin ka waqt aa gaya (ya `aheadMs` ke
   * andar aane wala hai). Bina waqt wale waade yahan nahi aate — wo sirf
   * context ke "khule waade" mein dikhte hain.
   */
  dueIntentions({ now = this.now(), aheadMs = 0, scopes = ['user'] } = {}) {
    const marks = scopes.map(() => '?').join(',');
    return this.db.prepare(`select * from intentions where status = 'open' and due_ts is not null and due_ts <= ?
      and (expiry_ts is null or expiry_ts > ?) and scope in (${marks}) order by due_ts`).all(now + aheadMs, now, ...scopes);
  }

  resolveIntention(id, status = 'done', { actor = 'user', cause = null } = {}) {
    if (!['done', 'dismissed', 'expired', 'open'].includes(status)) return { ok: false, reason: 'bad_status' };
    const row = this.q('select * from intentions where id = ?').get(id);
    if (!row) return { ok: false, reason: 'not_found' };
    this.q('update intentions set status = ?, resolved_at = ? where id = ?').run(status, status === 'open' ? null : this.now(), id);
    this.audit(actor, status, 'intention', id, { status: row.status }, { status }, cause);
    return { ok: true };
  }

  markIntentionSurfaced(id) {
    this.q('update intentions set last_surfaced_at = ?, surfaced_count = surfaced_count + 1 where id = ?').run(this.now(), id);
  }

  /** Jin waadon ki muddat guzar gayi wo khud `expired` ho jate hain. */
  expireIntentions(now = this.now()) {
    return this.q(`update intentions set status = 'expired', resolved_at = ? where status = 'open' and expiry_ts is not null and expiry_ts <= ?`)
      .run(now, now).changes;
  }

  // ── character ki taraf ka kacha record ────────────────────────────────────

  /**
   * Character ne kya socha / kaha / chup raha / us par kya jawab aaya.
   * Sirf append. Ye kabhi user ka fact nahi banta aur nichor ka source nahi.
   */
  addCharacterEvent(input) {
    const characterId = clean(input && input.characterId);
    const kind = clean(input && input.kind);
    if (!characterId || !kind || this.isPaused()) return { ok: false };
    const text = clip(redactSecrets(String(input.text || '').trim()).text, 2000);
    const info = this.q(`insert or ignore into character_events(ts, character_id, kind, text, meta, screen_derived, session_id, source_ref)
      values (?, ?, ?, ?, ?, ?, ?, ?)`)
      .run(Number(input.ts) > 0 ? Number(input.ts) : this.now(), characterId.slice(0, 80), kind.slice(0, 40), text,
        clip(JSON.stringify(input.meta || {}), 2000), input.screenDerived ? 1 : 0,
        input.sessionId || null, input.sourceRef ? clean(input.sourceRef).slice(0, 200) : null);
    return { ok: true, added: info.changes > 0 };
  }

  listCharacterEvents({ characterId, since = 0, limit = 200, kinds = null } = {}) {
    if (kinds && kinds.length) {
      const marks = kinds.map(() => '?').join(',');
      return this.db.prepare(`select * from character_events where character_id = ? and ts >= ? and kind in (${marks})
        order by ts desc limit ?`).all(characterId, since, ...kinds, limit);
    }
    return this.q('select * from character_events where character_id = ? and ts >= ? order by ts desc limit ?').all(characterId, since, limit);
  }

  // ── episodes ──────────────────────────────────────────────────────────────

  addEpisode(input) {
    const title = clip(clean(input.title), 160) || 'Conversation';
    const summary = clip(clean(input.summaryEn), 1200);
    const norm = normalizeForSearch(`${title} ${summary}`);
    const info = this.q(`insert into episodes(uid, session_id, channel, character_id, first_turn, last_turn, start_ts, end_ts,
        title, summary_en, fold, skel, mood, salience, digest_version, created_at)
      values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
      .run(uid('ep'), input.sessionKey, input.channel, input.characterId || null, input.firstTurn, input.lastTurn,
        input.startTs, input.endTs, title, summary, norm.fold, norm.skel, input.mood ? clip(clean(input.mood), 40) : null,
        Number.isFinite(input.salience) ? Math.max(0, Math.min(1, input.salience)) : 0, input.digestVersion || null, this.now());
    return Number(info.lastInsertRowid);
  }

  getEpisode(id) { return this.q('select * from episodes where id = ?').get(id) || null; }

  recentEpisodes({ limit = 5, characterId = null, channel = null } = {}) {
    const where = [];
    const args = [];
    if (characterId) { where.push('character_id = ?'); args.push(characterId); }
    if (channel) { where.push('channel = ?'); args.push(channel); }
    return this.db.prepare(`select * from episodes ${where.length ? `where ${where.join(' and ')}` : ''}
      order by end_ts desc limit ?`).all(...args, limit);
  }

  // ── ginti aur export ──────────────────────────────────────────────────────

  stats() {
    const n = (sql) => Number(this.q(sql).get().n);
    return {
      turns: n('select count(*) as n from turns'),
      sessions: n('select count(*) as n from sessions'),
      memories: n(`select count(*) as n from memories where deleted = 0 and status = 'active'`),
      candidates: n(`select count(*) as n from memories where deleted = 0 and status = 'candidate'`),
      pinned: n(`select count(*) as n from memories where deleted = 0 and status = 'active' and pinned = 1`),
      episodes: n('select count(*) as n from episodes'),
      openIntentions: n(`select count(*) as n from intentions where status = 'open'`),
      characterEvents: n('select count(*) as n from character_events'),
      firstTurnAt: Number(this.q('select min(ts) as n from turns').get().n) || null,
      lastTurnAt: Number(this.q('select max(ts) as n from turns').get().n) || null,
      paused: this.isPaused(),
    };
  }

  /** User ki apni yaadash, parhne laiq shakl mein (JSON export ke liye). */
  exportAll() {
    return {
      exported_at: new Date(this.now()).toISOString(),
      stats: this.stats(),
      memories: this.q(`select uid, type, text_original, text_en, origin, status, scope, sensitivity, pinned, triggers,
        confidence, evidence_count, event_start, mentioned_at, valid_to from memories where deleted = 0 order by id`).all(),
      intentions: this.q('select uid, what, who_for, due_ts, status, created_at from intentions order by id').all(),
      episodes: this.q('select uid, session_id, title, summary_en, start_ts, end_ts from episodes order by id').all(),
      sessions: this.q('select id, channel, character_id, started_at, last_turn_at, turn_count from sessions order by started_at').all(),
      turns: this.q('select session_id, ts, speaker, character_id, text from turns order by id').all(),
    };
  }
}

module.exports = { MemoryStore, sessionKey, scopesFor, parseJson, wordOverlap };
