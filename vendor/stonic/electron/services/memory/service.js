// Memory Core ki service — baqi app ke liye wahid chehra.
//
// ════════════════════════════════════════════════════════════════════════════
//  YE FILE KYA HAI
// ════════════════════════════════════════════════════════════════════════════
//
//  IPC (renderer) aur main process dono isi class se baat karte hain. Ye store, talash, state card, nichor
//  aur qataar ko jorti hai, aur do waade nibhati hai:
//
//   1. APP KABHI YAADASH KI WAJAH SE NAHI GIRTI. Driver load na ho, file
//      kharab ho, kuch bhi ho — `init()` throw nahi karta, service `ready:false`
//      ho jati hai aur har method ek saaf "band hai" jawab deta hai. Bulane
//      wale (voice, Awareness) us par purane raaste par chale jate hain.
//
//   2. BOLTE WAQT KOI BHAARI KAAM NAHI. Likhna aur talash chand millisecond
//      ke hain. Nichor (LLM) sirf qataar se chalta hai, baat khatam hone ke
//      baad, aur voice chalti ho to rukta hai.
//
//  Ye file bhi Electron ke baghair hai: folder, ghari aur LLM bahar se aate
//  hain. Electron se jorne ka kaam memory-handlers.js (IPC) karta hai.

const fs = require('fs');
const path = require('path');
const { EventEmitter } = require('events');
const {
  openMemoryDb, closeMemoryDb, quickCheck, backupNow, listBackups, getDriverError, memoryDir,
} = require('./db');
const { MemoryStore, sessionKey, scopesFor, parseJson, wordOverlap } = require('./store');
const { MemorySearch } = require('./search');
const { MemoryContext } = require('./context');
const { MemoryDigest } = require('./digest');
const { RecallAgent } = require('./recall-agent');
const { JobQueue } = require('./jobs');
const { runMaintenance } = require('./consolidate');
const { queryTokens } = require('./normalize');
const { parseWhen, dueText } = require('./timefmt');
const {
  DIGEST, LIMITS, INTENTIONS, AGENT, HOUR, DAY, MIN, MINDS, TURN_FLAGS,
} = require('./config');

const OFF = (reason) => ({ ok: false, enabled: false, reason });
const MAINTENANCE_EVERY_MS = 6 * HOUR;


class MemoryService extends EventEmitter {
  /**
   * @param {{
   *   rootDir?: string, ownerKey?: string, baseDir?: string,
   *   mirrorRoot?: string|null, enabled?: boolean,
   *   now?: () => number, log?: (level: string, msg: string) => void
   * }} opts
   *   rootDir + ownerKey: asal app — file `<rootDir>/<ownerKey>/memory.db` par.
   *   baseDir: tests — seedha folder.
   *   mirrorRoot: backup ki doosri jagah (`<mirrorRoot>/<ownerKey>/`); null = nahi.
   */
  constructor(opts) {
    super();
    this.rootDir = opts.rootDir || null;
    // ownerProvider: is waqt kaun sa account sign in hai ('' = koi nahi). Har
    // call par poocha jata hai, taake account badalte hi file bhi badal jaye.
    this.ownerProvider = typeof opts.ownerProvider === 'function' ? opts.ownerProvider : null;
    this.ownerKey = this.ownerProvider ? '' : (opts.ownerKey || 'local');
    this.mirrorRoot = opts.mirrorRoot || null;
    this.enabled = opts.enabled !== false;
    this.baseDir = opts.baseDir || path.join(this.rootDir, this.ownerKey || '_none');
    this._tried = false;
    this.now = opts.now || (() => Date.now());
    this.log = opts.log || (() => {});
    this.ready = false;
    this.reason = 'not_started';
    this.db = null;
    this.store = null;
    this.search = null;
    this.context = null;
    this.jobs = null;
    this.digest = null;
    this.agent = null;
    this.llm = null;
    /** Voice session chal rahi ho to nichor intezar karta hai. */
    this.busy = false;
    this._rememberCount = new Map();
    this._followUpCount = new Map();
    this._askCount = new Map();
    this._ticking = false;
    this._lastMaintenance = 0;
  }

  // ── zindagi ───────────────────────────────────────────────────────────────

  /**
   * File kholta hai. KABHI throw nahi karta.
   * @param {{ enabled?: boolean }} [opts] enabled:false = support ka kill switch
   */
  init(opts = {}) {
    if (this.ready) return { ok: true };
    this._tried = true;
    if (opts.enabled === false || !this.enabled) { this.reason = 'disabled'; return OFF('disabled'); }
    // Haath ka kill switch: `<userData>/memory/DISABLED` naam ki file ho to
    // yaadash band (support ke liye — app baqi sab pehle jaisi chalti hai).
    try {
      const marker = path.join(this.rootDir || this.baseDir, 'DISABLED');
      if (fs.existsSync(marker)) { this.reason = 'disabled'; return OFF('disabled'); }
    } catch { /* folder abhi nahi bana */ }
    try {
      const opened = openMemoryDb(this.baseDir, { now: this.now() });
      this.db = opened.db;
      if (!quickCheck(this.db)) {
        // Kharab file par likhna kharabi barhata hai. Band rakho; backup mojood hain.
        closeMemoryDb(this.db);
        this.db = null;
        this.reason = 'corrupt';
        this.log('error', '[Memory] database ki sehat ki jaanch fail — yaadash band. Backups: memory/backups');
        return OFF('corrupt');
      }
      this.store = new MemoryStore(this.db, { now: this.now });
      this.search = new MemorySearch(this.store);
      this.context = new MemoryContext(this.store, this.search);
      this.jobs = new JobQueue(this.store);
      this.digest = new MemoryDigest(this.store, this.search, { llm: (req) => this._callLlm(req) });
      this.agent = new RecallAgent(this.store, this.search);
      this.ready = true;
      this.reason = '';
      if (opened.migrated.applied) {
        this.log('info', `[Memory] dhaancha v${opened.migrated.from} -> v${opened.migrated.to}${opened.preMigrationBackup ? ' (pehle copy bani)' : ''}`);
      }
      return { ok: true };
    } catch (err) {
      const driver = getDriverError();
      this.reason = driver ? 'driver' : 'open_failed';
      this.log('error', `[Memory] shuru nahi ho saka (${this.reason}): ${err.message}`);
      return OFF(this.reason);
    }
  }

  /**
   * Pehle istemal par file kholna. IPC ya bridge ki pehli call boot ke "paint
   * ke baad" wale darwaze se pehle aa sakti hai; us waqt renderer chal chuka
   * hota hai, is liye yahan kholna mehfooz hai. Ek hi dafa koshish hoti hai —
   * nakaami par har call dobara file nahi kholti.
   */
  ensure() {
    if (this.ownerProvider && !this._stopped) {
      let key = '';
      try { key = this.ownerProvider() || ''; } catch { key = ''; }
      if (key !== this.ownerKey) this.setOwner(key);
      // Sign in ke baghair yaadash band: kisi ki bhi nahi hoti, to kahin likhi bhi nahi jati.
      if (!key) { this.reason = 'signed_out'; return false; }
    }
    if (this.ready) return true;
    if (this._tried) return false;
    return this.init().ok;
  }

  /**
   * Account badla (sign out / doosra shakhs sign in): us account ki apni file.
   * Wohi account ho to kuch nahi hota.
   */
  setOwner(ownerKey) {
    const next = ownerKey || '';
    if (!this.rootDir || next === this.ownerKey) return false;
    if (this.db) { try { closeMemoryDb(this.db); } catch { /* band ho hi raha hai */ } }
    this.db = null;
    this.ready = false;
    this.ownerKey = next;
    this.baseDir = path.join(this.rootDir, next || '_none');
    this._tried = false;
    this.reason = 'not_started';
    this._rememberCount.clear();
    this._followUpCount.clear();
    this._askCount.clear();
    this._lastMaintenance = 0;
    return true;
  }

  /** Synchronous, kabhi throw nahi karta, do dafa bulana be-zarar (quit ke har raaste se bulaya jata hai). */
  shutdown() {
    this.ready = false;
    this.reason = 'stopped';
    this._tried = true;
    this._stopped = true;
    try { if (this.db) closeMemoryDb(this.db); } catch { /* band ho hi raha hai */ }
    this.db = null;
  }

  _mirrorDir() {
    return this.mirrorRoot ? path.join(this.mirrorRoot, this.ownerKey) : null;
  }

  /** Nichor ke liye LLM. `null` = nichor band (turns phir bhi likhe jate hain). */
  setLlm(fn) { this.llm = typeof fn === 'function' ? fn : null; }

  setBusy(busy) { this.busy = !!busy; }

  async _callLlm(req) {
    if (!this.llm) { const e = new Error('no llm'); e.code = 'no_llm'; throw e; }
    return this.llm(req);
  }

  _changed(kind) { this.emit('changed', { kind, at: this.now() }); }

  status() {
    if (!this.ensure()) return { ok: true, enabled: false, ready: false, reason: this.reason };
    const monthStart = this._monthStart();
    const backups = listBackups(this.baseDir);
    return {
      ok: true,
      enabled: true,
      ready: true,
      paused: this.store.isPaused(),
      stats: this.store.stats(),
      pinned: this.store.pinnedUsage(),
      digest: {
        enabled: this.store.getMeta('digest_enabled', '1') === '1',
        hasModel: !!this.llm,
        spentThisMonthUsd: Number(this.jobs.spentSince(monthStart).toFixed(4)),
        monthlyCapUsd: Number(this.store.getMeta('digest_cap_usd', String(DIGEST.monthlyCapUsd))),
        queue: this.jobs.counts(),
        lastError: this.store.getMeta('digest_last_error', ''),
      },
      backup: {
        count: backups.length,
        lastAt: backups[0] ? backups[0].at : null,
        lastCheckOk: this.store.getMeta('last_backup_check_ok', ''),
        mirrorDir: this._mirrorDir(),
      },
      folder: memoryDir(this.baseDir),
    };
  }

  _monthStart() {
    const d = new Date(this.now());
    return new Date(d.getFullYear(), d.getMonth(), 1).getTime();
  }

  // ── likhna: baat-cheet ────────────────────────────────────────────────────

  /**
   * Baat-cheet ke jumle. Voice (renderer) yahin likhta hai.
   * Likhne ke baad us session ka nichor qataar mein lagta hai — khamoshi ke baad chalega.
   */
  appendTurns(input) {
    if (!this.ensure()) return OFF(this.reason);
    const res = this.store.appendTurns(input);
    if (res.ok && (res.added || res.updated)) {
      this._scheduleDigest(res.sessionKey);
      this._changed('turns');
    }
    return res;
  }

  /** Baat-cheet khatam hui (voice band). */
  sessionEnded(input) {
    if (!this.ensure()) return OFF(this.reason);
    const key = this.store.endSession(input.channel, input.sessionId);
    this._scheduleDigest(key);
    this._rememberCount.delete(key);
    this._followUpCount.delete(key);
    this._askCount.delete(key);
    return { ok: true, sessionKey: key };
  }

  _scheduleDigest(key) {
    const session = this.store.getSession(key);
    if (!session || session.channel === 'awareness') return;
    // Key mein nichor ka nishan (digested_upto) hai: jab tak nichor na nikle wohi
    // kaam rehta hai (aur us ka waqt aage khisakta rehta hai); nikal jaye to
    // naye turns ka naya kaam banta hai.
    this.jobs.enqueue('digest', { sessionKey: key }, `digest:${key}:${session.digested_upto}:${DIGEST.promptVersion}`,
      { runAfter: this.now() + DIGEST.quietMs });
  }

  // ── likhna: "yaad rakhna" ─────────────────────────────────────────────────

  /**
   * User ne kaha "yaad rakhna" (ya model ne koi pakki baat pakri). Foran likha
   * jata hai aur tasdeeq wapas jati hai — is raaste mein koi LLM nahi aur koi
   * khamosh nakaami nahi.
   *
   * @param {{ text: string, source?: 'voice'|'user', characterId?: string|null,
   *   channel?: string, sessionId?: string, pin?: boolean, about?: string, triggers?: string[],
   *   type?: string, scope?: string, sensitivity?: string }} input
   * @returns {{ ok: boolean, id?: number, pinned?: boolean, duplicate?: boolean, reason?: string, message: string }}
   */
  remember(input) {
    if (!this.ensure()) return { ...OFF(this.reason), message: 'Memory is off.' };
    if (this.store.isPaused()) return { ok: false, reason: 'paused', message: 'Memory is paused, so this was not saved.' };
    const text = String((input && input.text) || '').replace(/\s+/g, ' ').trim();
    if (!text) return { ok: false, reason: 'empty', message: 'Nothing to remember.' };
    const source = input.source || 'voice';
    const key = input.channel && input.sessionId ? sessionKey(input.channel, input.sessionId) : null;

    if (source !== 'user' && key) {
      const used = this._rememberCount.get(key) || 0;
      if (used >= LIMITS.rememberPerSession) {
        return { ok: false, reason: 'session_limit', message: 'Too many things saved in this conversation already; this one was not saved.' };
      }
      this._rememberCount.set(key, used + 1);
    }

    // Saboot: isi baat-cheet mein user ka wo taaza jumla jis se ye baat nikli.
    // Mile to yaad "user ne khud kaha" hai; na mile to "assistant ne note kiya".
    const evidence = key ? this._evidenceFor(text, key) : [];
    const origin = source === 'user' ? 'manual' : (evidence.length ? 'stated' : 'assistant_tool');
    const about = input.about ? String(input.about).trim() : '';

    const base = {
      type: input.type, text, origin,
      scope: input.scope || 'user',
      sensitivity: input.sensitivity,
      status: 'active',
      triggers: input.triggers || (input.pin && about ? [about] : []),
      evidence,
      entities: about ? [{ name: about, type: 'person' }] : [],
      sourceSession: key,
      createdBy: source,
      actor: source,
      userRequested: true,
      cause: 'remember',
    };
    let made = this.store.addMemory({ ...base, pinned: !!input.pin });
    let pinFull = false;
    if (!made.ok && /^pin_limit/.test(made.reason || '')) {
      // Pin ki jagah nahi: baat phir bhi likhi jati hai (bina pin), aur user ko bataya jata hai.
      pinFull = true;
      made = this.store.addMemory({ ...base, pinned: false });
    }
    if (!made.ok) {
      let why = 'It could not be saved.';
      if (made.reason === 'secret_only') why = 'That looks like a password or code, which is never stored.';
      // Lambi yaad kaati nahi jati. Model ko wajah aur hal dono milte hain, taake wo
      // usi baat ko do yaadon mein likh kar dobara bula sake.
      if (made.reason === 'too_long') {
        why = `Not saved: that is ${text.length} characters and one memory holds at most ${LIMITS.memoryMaxChars}. Save it as two or more memories, one topic each.`;
      }
      return { ok: false, reason: made.reason, message: why };
    }
    this._changed('memories');
    const m = this.store.getMemory(made.id);
    return {
      ok: true,
      id: made.id,
      uid: m ? m.uid : undefined,
      pinned: !!(m && m.pinned),
      duplicate: !!made.duplicateOf,
      message: pinFull
        ? 'Saved, but the always-remember list is full, so it is saved as a normal memory. They can free a slot on the Memory page.'
        : (made.duplicateOf ? 'Already remembered; noted again.' : 'Saved to memory.'),
    };
  }

  _evidenceFor(text, key) {
    const want = queryTokens(text).map((t) => t.fold);
    if (!want.length) return [];
    const recent = this.store.q(`select id, text, fold from turns where session_id = ? and speaker = 'user' and (flags & ?) = 0
      order by id desc limit 8`).all(key, TURN_FLAGS.automated | TURN_FLAGS.recalled);
    let best = null;
    let bestHits = 0;
    for (const t of recent) {
      const words = new Set(t.fold.split(' '));
      const hits = want.filter((w) => words.has(w)).length;
      if (hits > bestHits) { best = t; bestHits = hits; }
    }
    // Kam se kam do lafz ya aadhe lafz milen — ek aam lafz ka ittefaq saboot nahi.
    if (!best || bestHits < Math.min(2, want.length) || bestHits / want.length < 0.34) return [];
    return [{ turnId: best.id, quote: best.text.slice(0, 300) }];
  }

  /**
   * "Ye bhool jao." Talash se sab se mutalliq yaad dhoondh kar retract karta
   * hai. Sirf PAKKA match (FOUND) — kamzor match par kuch nahi mit-ta, user se
   * poocha jata hai.
   */
  forgetByQuery(input) {
    if (!this.ensure()) return { ...OFF(this.reason), message: 'Memory is off.' };
    const found = this.search.recall({
      query: input.query, queryEn: input.queryEn, mind: input.mind || 'voice',
      characterId: input.characterId, kinds: ['memory'], k: 3, purpose: 'answer',
    });
    const top = found.items[0];
    const second = found.items[1];
    // "Cigarette wali baat bhool jao": sawal mein bharti ke lafz hote hain, is liye
    // yahan talash ka sakht FOUND zaroori nahi — magar match SAAF hona chahiye:
    // pakka lafz mila ho, aur doosra umeedwar us ke qareeb na ho. Shak ho to kuch
    // nahi mit-ta aur model user se poochta hai.
    const clear = top && (found.status === 'FOUND'
      || (top.strong && top.coverage >= 0.5 && (!second || top.score >= 1.3 * second.score)));
    if (!clear) {
      return { ok: false, reason: 'not_found', message: 'No clear match in memory. Ask them which memory they mean, or tell them they can remove it on the Memory page.' };
    }
    this._hideSourceTurns(top.id);
    const res = this.store.forgetMemory(top.id, { hard: false, actor: input.mind || 'voice', cause: 'forget_by_query' });
    if (res.ok) this._changed('memories');
    return { ok: res.ok, id: top.id, forgotten: top.text, message: res.ok ? `Forgotten: "${top.text}". It will not come back.` : 'Could not forget it.' };
  }

  // ── likhna: waada (follow-up) ─────────────────────────────────────────────

  /**
   * Voice ka `follow_up` tool: waada usi waqt likhna, ya poora ho chuka waada
   * band karna. Nichor bhi waade nikalta hai, magar baat khatam hone ke 15
   * minute baad — "10 minute baad yaad dilana" tab tak guzar chuka hota hai.
   * Nichor baad mein wohi waada dobara nahi banata (digest.alreadyNoted).
   *
   * `remember` ki tarah: koi LLM nahi, koi khamosh nakaami nahi — `message`
   * model ko batata hai ke waqai kya hua, aur waqt kaun sa laga.
   *
   * @param {{ what: string, when?: string, done?: boolean, source?: 'voice',
   *   characterId?: string|null, channel?: string, sessionId?: string }} input
   *   when: timefmt.parseWhen ki shaklein ("+10m", "tomorrow 17:00", ...). Khali = bina waqt ke.
   * @returns {{ ok: boolean, id?: number, dueTs?: number|null, duplicate?: boolean, closed?: string,
   *   reason?: string, message: string }}
   */
  followUp(input) {
    if (!this.ensure()) return { ...OFF(this.reason), message: 'Memory is off.' };
    if (this.store.isPaused()) return { ok: false, reason: 'paused', message: 'Memory is paused, so nothing was saved or changed.' };
    const what = String((input && input.what) || '').replace(/\s+/g, ' ').trim();
    if (!what) return { ok: false, reason: 'empty', message: 'Nothing to follow up on.' };
    const source = input.source || 'voice';
    if (input.done) return this._closeFollowUp(what, source);

    const now = this.now();
    const whenRaw = String(input.when || '').trim();
    const dueTs = whenRaw ? parseWhen(whenRaw, now) : null;
    if (whenRaw && dueTs == null) {
      return {
        ok: false,
        reason: 'bad_when',
        message: `Not saved: "${whenRaw.slice(0, 40)}" is not a time you can use (unreadable, or already past). Write it as "+10m", "+2h", "today 17:00", "tomorrow 09:00", "fri 15:00" or "2026-10-12 10:00".`,
      };
    }

    const key = input.channel && input.sessionId ? sessionKey(input.channel, input.sessionId) : null;
    if (key) {
      const used = this._followUpCount.get(key) || 0;
      if (used >= LIMITS.followUpsPerSession) {
        return { ok: false, reason: 'session_limit', message: 'Too many follow-ups were saved in this conversation already; this one was not saved.' };
      }
      this._followUpCount.set(key, used + 1);
    }

    // Saboot: user ka wo taaza jumla jis mein ye kaha gaya (mile to). Nichor
    // isi jumle se pehchanta hai ke ye waada pehle hi likha ja chuka hai.
    const [evidence] = key ? this._evidenceFor(what, key) : [];
    const made = this.store.addIntention({
      what,
      cueType: 'time',
      dueTs,
      expiryTs: dueTs ? dueTs + INTENTIONS.expiryAfterDueMs : now + INTENTIONS.expiryUndatedMs,
      sourceTurn: evidence ? evidence.turnId : null,
      sourceSession: key,
      quote: evidence ? evidence.quote : '',
      createdBy: source,
      characterId: input.characterId || null,
      actor: source,
      cause: 'follow_up',
    });
    if (!made.ok) {
      return { ok: false, reason: made.reason, message: 'It could not be saved.' };
    }
    if (made.duplicateOf) return { ok: true, id: made.id, duplicate: true, dueTs, message: 'Already on the follow-up list; nothing new was added.' };
    this._changed('intentions');
    return {
      ok: true,
      id: made.id,
      dueTs,
      message: dueTs ? `Saved. It is due ${dueText(dueTs, now)}.` : 'Saved to follow-ups, with no time set.',
    };
  }

  /**
   * "Wo kaam ho gaya / ab zaroorat nahi." Khule waadon mein se sab se milta
   * julta band hota hai — sirf SAAF match par. Do waade barabar milen (ya koi
   * theek se na mile) to kuch band nahi hota aur model user se poochta hai.
   */
  _closeFollowUp(what, actor) {
    const scored = this.store.listIntentions({ status: 'open', limit: 200 })
      .map((row) => ({ row, score: Math.max(wordOverlap(what, row.what), wordOverlap(what, row.what_en)) }))
      .sort((a, b) => b.score - a.score);
    const [top, second] = scored;
    if (!top || top.score < INTENTIONS.sameOverlap || (second && second.score >= top.score)) {
      return { ok: false, reason: 'not_found', message: 'No clear match among the open follow-ups, so nothing was closed. Ask them which one they mean, or tell them they can close it on the Memory page.' };
    }
    const res = this.store.resolveIntention(Number(top.row.id), 'done', { actor, cause: 'follow_up' });
    if (res.ok) this._changed('intentions');
    return {
      ok: res.ok,
      id: Number(top.row.id),
      closed: top.row.what,
      message: res.ok ? `Closed: "${top.row.what}". You will not bring it up again.` : 'Could not close it.',
    };
  }

  // ── parhna ────────────────────────────────────────────────────────────────

  /**
   * Talash. Jawab mein tayyar text bhi hota hai (tool result / prefetch ke liye).
   */
  recall(input) {
    if (!this.ensure()) return { ok: false, enabled: false, status: 'NONE', items: [], text: '' };
    const mind = MINDS.includes(input.mind) ? input.mind : 'voice';
    const result = this.search.recall({ ...input, mind });
    const ids = result.items.filter((i) => i.kind === 'memory').map((i) => i.id);
    if (ids.length) this.store.markUsed(ids, mind, false);
    return {
      ok: true,
      status: result.status,
      items: result.items,
      text: this.context.formatRecall(result, { mind, now: this.now(), forPrompt: !!input.forPrompt }),
      tookMs: result.tookMs,
    };
  }

  // ── Recall Agent (voice ka `recall`) ──────────────────────────────────────
  //
  // Do qadam, kyunke model tak ka raasta renderer ke paas hai (recall-agent.js):
  //   recallPack   -> saari yaadash ka prompt   (renderer isay model ko bhejta hai)
  //   recallFinish -> model ke jawab ki jaanch, aur voice ke liye tayyar text
  // `agent:false` = model ko mat bulao (pause, hadd, yaadash khali): bulane wala
  // lafzi talash (`recall`) ka jawab deta hai, jo PC par chalti aur muft hai.

  /**
   * @param {{ question: string, keywords?: string, characterId?: string|null, channel?: string, sessionId?: string,
   *   recent?: Array<{ speaker: 'user'|'assistant', text: string }> }} input
   * @returns {{ ok: boolean, agent?: boolean, reason?: string, id?: string, system?: string, user?: string,
   *   maxOutputTokens?: number, timeoutMs?: number, stats?: object }}
   */
  recallPack(input) {
    if (!this.ensure()) return OFF(this.reason);
    if (!String((input && input.question) || '').trim()) return { ok: false, reason: 'empty' };
    if (this.store.isPaused()) return { ok: true, agent: false, reason: 'paused' };

    const key = input.channel && input.sessionId ? sessionKey(input.channel, input.sessionId) : null;
    if (key) {
      const used = this._askCount.get(key) || 0;
      if (used >= AGENT.asksPerSession) return { ok: true, agent: false, reason: 'session_limit' };
      this._askCount.set(key, used + 1);
    }

    const pack = this.agent.pack({ ...input, now: this.now() });
    if (pack.empty) {
      this.agent.cancel(pack.id);
      return { ok: true, agent: false, reason: 'empty_memory', stats: pack.stats };
    }
    return {
      ok: true, agent: true, id: pack.id, system: pack.system, user: pack.user,
      maxOutputTokens: pack.maxOutputTokens, timeoutMs: AGENT.timeoutMs, stats: pack.stats,
    };
  }

  /**
   * @param {{ id: string, text?: string, failed?: boolean }} input
   *   failed: model nahi chala — pack ka intezar khatam karo.
   * @returns {{ ok: boolean, reason?: string, status?: 'FOUND'|'PARTIAL'|'UNSURE'|'NONE', text?: string, sources?: string[] }}
   */
  recallFinish(input) {
    if (!this.ensure()) return OFF(this.reason);
    const id = String((input && input.id) || '');
    if (input && input.failed) return { ok: this.agent.cancel(id) };
    const res = this.agent.finish(id, input && input.text);
    // Jo yaadein jawab mein waqai kaam aayin: un ki "kaam aayi" wali ginti barhti hai.
    if (res.ok && res.memoryIds && res.memoryIds.length) this.store.markUsed(res.memoryIds, 'voice', true);
    return res;
  }

  /**
   * Recall Agent kitna bhara hai (Memory page → Settings ka gauge). Sirf ginti.
   * @param {{ characterId?: string|null }} [input]
   */
  recallUsage(input = {}) {
    if (!this.ensure()) return OFF(this.reason);
    return { ok: true, paused: this.store.isPaused(), ...this.agent.usage(input) };
  }

  /** State card — connect par voice ko, soch se pehle Awareness ko. */
  contextBlock(input) {
    if (!this.ensure()) return { ok: false, enabled: false, text: '' };
    const mind = MINDS.includes(input.mind) ? input.mind : 'voice';
    const exclude = input.channel && input.sessionId ? sessionKey(input.channel, input.sessionId) : null;
    const card = this.context.stateCard({ ...input, mind, excludeSession: exclude, now: this.now() });
    return { ok: true, ...card };
  }

  /** Awareness ke liye: jin waadon ka waqt aa gaya. `surface:true` par ginti barhti hai. */
  dueIntentions(input = {}) {
    if (!this.ensure()) return { ok: false, enabled: false, text: '', items: [] };
    if (this.store.isPaused()) return { ok: true, text: '', items: [] };
    const block = this.context.dueBlock({ characterId: input.characterId, now: this.now(), aheadMs: input.aheadMs || 0 });
    if (input.surface) block.ids.forEach((id) => this.store.markIntentionSurfaced(id));
    const items = block.ids.map((id) => this.store.getIntention(id)).filter(Boolean);
    return { ok: true, text: block.text, items };
  }

  // ── Yaadash page (UI) ─────────────────────────────────────────────────────

  listMemories(filter = {}) {
    if (!this.ensure()) return { ...OFF(this.reason), items: [] };
    const rows = this.store.listMemories(filter).map((m) => ({ ...m, triggers: parseJson(m.triggers, []) }));
    return { ok: true, items: rows };
  }

  memoryDetail(id) {
    if (!this.ensure()) return OFF(this.reason);
    const m = this.store.getMemory(id);
    if (!m) return { ok: false, reason: 'not_found' };
    return {
      ok: true,
      memory: { ...m, triggers: parseJson(m.triggers, []) },
      evidence: this.store.getEvidence(id),
      history: this.store.listAudit({ targetKind: 'memory', targetId: id, limit: 30 }),
    };
  }

  updateMemory(id, patch) {
    if (!this.ensure()) return OFF(this.reason);
    const res = this.store.updateMemory(id, patch || {}, { actor: 'user', cause: 'edit' });
    if (res.ok) this._changed('memories');
    return res;
  }

  setPinned(id, pinned) {
    if (!this.ensure()) return OFF(this.reason);
    const res = this.store.setPinned(id, pinned, { actor: 'user' });
    if (res.ok) this._changed('memories');
    return res;
  }

  /**
   * Jis yaad ke saath saboot ka jumla nahi jura (model ne khud note ki thi), us
   * ka asal jumla usi baat-cheet mein dhoondh kar chhupana. Sirf PAKKA match:
   * be-taalluq jumla chhup jaye to user ki baat-cheet mein suraakh ho jata.
   */
  _hideSourceTurns(memoryId) {
    const m = this.store.getMemory(memoryId);
    if (!m || !m.source_session || this.store.getEvidence(memoryId).some((ev) => ev.turn_id)) return 0;
    const found = this.search.recall({ query: m.text_original, queryEn: m.text_en, mind: 'hermes', kinds: ['turn'], k: 5 });
    const ids = found.items
      .filter((it) => it.sessionKey === m.source_session && it.speaker === 'user' && it.strong && it.coverage >= 0.6)
      .map((it) => it.id);
    return this.store.hideTurns(ids);
  }

  forgetMemory(id, { hard = false } = {}) {
    if (!this.ensure()) return OFF(this.reason);
    if (!hard) this._hideSourceTurns(id);
    const res = this.store.forgetMemory(id, { hard, actor: 'user', cause: 'memory_page' });
    if (res.ok) this._changed('memories');
    return res;
  }

  listIntentions(filter = {}) {
    if (!this.ensure()) return { ...OFF(this.reason), items: [] };
    return { ok: true, items: this.store.listIntentions(filter) };
  }

  addIntention(input) {
    if (!this.ensure()) return OFF(this.reason);
    const res = this.store.addIntention({ ...input, createdBy: input.createdBy || 'user', actor: 'user' });
    if (res.ok) this._changed('intentions');
    return res;
  }

  resolveIntention(id, status) {
    if (!this.ensure()) return OFF(this.reason);
    const res = this.store.resolveIntention(id, status, { actor: 'user' });
    if (res.ok) this._changed('intentions');
    return res;
  }

  listSessions(filter = {}) {
    if (!this.ensure()) return { ...OFF(this.reason), items: [] };
    return { ok: true, items: this.store.listSessions(filter) };
  }

  sessionTurns(key, opts = {}) {
    if (!this.ensure()) return { ...OFF(this.reason), items: [] };
    return {
      ok: true,
      session: this.store.getSession(key),
      items: this.store.getTurns(key, { limit: Math.min(Number(opts.limit) || 500, 2000) }),
      episodes: this.store.q('select * from episodes where session_id = ? order by start_ts').all(key),
    };
  }

  setPaused(paused) {
    if (!this.ensure()) return OFF(this.reason);
    this.store.setPaused(!!paused, 'user');
    this._changed('settings');
    return { ok: true, paused: !!paused };
  }

  setDigestEnabled(enabled) {
    if (!this.ensure()) return OFF(this.reason);
    this.store.setMeta('digest_enabled', enabled ? '1' : '0');
    this._changed('settings');
    return { ok: true };
  }

  // ── character ki taraf ka record ──────────────────────────────────────────

  characterEvent(input) {
    if (!this.ensure()) return OFF(this.reason);
    return this.store.addCharacterEvent(input);
  }

  characterEvents(filter) {
    if (!this.ensure()) return { ...OFF(this.reason), items: [] };
    return { ok: true, items: this.store.listCharacterEvents(filter || {}) };
  }

  // ── export aur backup ─────────────────────────────────────────────────────

  /** Poori yaadash ek parhne laiq JSON file mein. User apni yaadash ka malik hai. */
  exportTo(file) {
    if (!this.ensure()) return OFF(this.reason);
    try {
      fs.writeFileSync(file, JSON.stringify(this.store.exportAll(), null, 2), 'utf8');
      this.store.audit('user', 'export', 'store', null, null, null, null);
      return { ok: true, file };
    } catch (err) {
      return { ok: false, reason: err.message };
    }
  }

  async backup() {
    if (!this.ensure()) return OFF(this.reason);
    try {
      const made = await backupNow(this.db, this.baseDir, this.now(), this._mirrorDir());
      if (this.ready) this.store.setMeta('last_backup_at', this.now());
      return { ok: true, ...made };
    } catch (err) {
      return { ok: false, reason: err.message };
    }
  }

  // ── background ────────────────────────────────────────────────────────────

  /**
   * Ghari ki har dharkan par (aur session khatam hone par) bulaya jata hai.
   * Ek waqt mein ek hi kaam; voice chalti ho to nichor rukta hai.
   * @returns {Promise<{ ran: string|null, result?: object }>}
   */
  async tick() {
    if (!this.ensure() || this._ticking) return { ran: null };
    this._ticking = true;
    try {
      const now = this.now();
      if (now - this._lastMaintenance >= MAINTENANCE_EVERY_MS && !this.busy) {
        this._lastMaintenance = now;
        const m = runMaintenance(this.store, this.jobs, this.baseDir, now);
        if (!m.healthy) this.log('error', '[Memory] sehat ki jaanch fail hui — backup nahi banaya.');
        if (m.promoted || m.expired) this._changed('memories');
        if (m.backupWanted) {
          const b = await this.backup();
          if (!b.ok) this.log('warn', `[Memory] backup nahi ban saka: ${b.reason}`);
        }
        if (!this.ensure()) return { ran: 'maintenance' };
      }
      if (this.store.isPaused() || this.busy) return { ran: null };
      if (this.store.getMeta('digest_enabled', '1') !== '1' || !this.llm) return { ran: null };

      // Paise ki hadd — kaam uthane se PEHLE.
      const cap = Number(this.store.getMeta('digest_cap_usd', String(DIGEST.monthlyCapUsd)));
      if (this.jobs.spentSince(this._monthStart()) >= cap) return { ran: null, result: { capped: 'month' } };
      const dayStart = new Date(now); dayStart.setHours(0, 0, 0, 0);
      if (this.jobs.ranSince(dayStart.getTime(), 'digest') >= DIGEST.dailyCallCap) return { ran: null, result: { capped: 'day' } };

      const job = this.jobs.claim(['digest']);
      if (!job) return { ran: null };
      const res = await this.digest.digestSession(job.payload.sessionKey, now);
      if (res.ok) {
        this.jobs.complete(job.id, { costUsd: res.costUsd });
        this.store.setMeta('digest_last_error', '');
        this.store.setMeta('last_digest_at', this.now());
        if (res.memories || res.updates || res.intentions || res.episodes) this._changed('memories');
        if (!res.skipped) {
          this.log('info', `[Memory] nichor: ${job.payload.sessionKey} -> ${res.memories} yaadein, ${res.updates} tabdeeliyan, ${res.intentions} waade, ${res.rejected.length} rad`);
        }
      } else if (/^llm:(blocked|no_key|no_llm|network|auth|timeout|aborted)/.test(res.reason || '') || res.reason === 'paused') {
        // Plan ki hadd / key nahi / internet nahi / window band: ye baat-cheet ki
        // ghalti nahi. Koshish gine baghair baad mein dobara — plan ya key ka
        // masla ho to ek ghanta, warna das minute.
        this.jobs.defer(job.id, /^llm:(blocked|no_key)/.test(res.reason || '') ? HOUR : 10 * MIN);
        this.store.setMeta('digest_last_error', res.reason);
      } else {
        this.store.bumpDigestAttempt(job.payload.sessionKey);
        const state = this.jobs.fail(job.id, res.reason, { costUsd: res.costUsd });
        this.store.setMeta('digest_last_error', res.reason || 'error');
        this.log('warn', `[Memory] nichor nakaam (${res.reason}) — ${state === 'dead' ? 'chhor diya' : 'dobara koshish hogi'}`);
      }
      return { ran: 'digest', result: res };
    } catch (err) {
      this.log('error', `[Memory] tick: ${err.message}`);
      return { ran: null };
    } finally {
      this._ticking = false;
    }
  }

  /** App khulne par: jin sessions ka nichor reh gaya tha (crash, band) unhein qataar mein. */
  sweep() {
    if (!this.ensure()) return 0;
    let n = 0;
    for (const s of this.store.sessionsNeedingDigest(this.now(), 0)) {
      if (s.channel === 'awareness') continue;
      const r = this.jobs.enqueue('digest', { sessionKey: s.id }, `digest:${s.id}:${s.digested_upto}:${DIGEST.promptVersion}`,
        { runAfter: Math.max(this.now() + 2 * MIN, s.last_turn_at + DIGEST.quietMs) });
      if (r.created) n += 1;
    }
    return n;
  }
}

module.exports = { MemoryService, scopesFor, DAY };
