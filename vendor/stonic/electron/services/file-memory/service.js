// Yaadash ki service — baqi app ke liye wahid chehra.
//
// ════════════════════════════════════════════════════════════════════════════
//  JAGAH (lifetime — kabhi nahi badalni)
// ════════════════════════════════════════════════════════════════════════════
//
//   %APPDATA%\Stonic AI\Memory\<account>\
//
//  - App ke version / generation (stonic_gen3, gen4 …) se AZAAD: userData ka
//    folder har generation ka alag hota hai, yaadash ka nahi.
//  - Install folder se bahar — update, repair, reinstall kuch nahi chhoota.
//    Installer ka `deleteAppDataOnUninstall` abhi `false` hai; isay `true` kiya
//    to uninstall par electron-builder `%APPDATA%\Stonic AI` bhi mita deta hai
//    (yaani poori yaadash). Wo flag badalne se pehle ye yaad rakhein.
//  - Documents mein NAHI: Documents aksar OneDrive par hota hai, aur zaati
//    yaadash chupke se cloud par nahi jani chahiye.
//  - Har account ka apna folder (`u-<id>`): ek PC par do log hon to ek doosre
//    ki yaad nahi milti. Sign out = yaadash band (koi folder nahi khulta).
//
//  Engine Claude ki official memory hai (official.js / store.js). Is file ka
//  kaam sirf upar ki tahein: kaun sa account, muntaqli, har AI ko us ka hissa
//  (rules.js), aur folder par nazar — New voice ka SDK files seedha likhta hai,
//  to Memory page ko us ki khabar watcher se milti hai.

'use strict';

const fs = require('fs');
const path = require('path');
const { EventEmitter } = require('events');
const { FileMemory } = require('./store');
const { buildClassicCard, buildAgentRules, buildIndexOnly } = require('./rules');
const official = require('./official');
const { runMigration, MARKER_FILE } = require('./migrate');

/** Muntaqli ka intezar is se zyada nahi — der ho to baat bina purani yaadon ke shuru (wo peeche aa jati hain). */
const READY_WAIT_MS = 4000;
/** Folder ki kai tabdeeliyan (SDK ek note + fehrist likhta hai) ek hi khabar ban kar. */
const WATCH_DEBOUNCE_MS = 300;

const OFF = (reason) => ({ ok: false, enabled: false, reason });

/** Yaadash ka root — har generation ke liye wohi. */
function memoryRootFor(appDataDir) {
  return path.join(appDataDir, 'Stonic AI', 'Memory');
}

class FileMemoryService extends EventEmitter {
  /**
   * @param {{
   *   rootDir: string,
   *   ownerProvider: () => string,
   *   enabled?: boolean,
   *   watch?: boolean,
   *   legacy?: { memoryRoot?: string, legacyHermesDir?: () => string|null, bootstrapMemory?: string, memoriesJson?: string },
   *   legacy.legacyHermesDir: purane versions ka Hermes data folder (legacy Hermes memory files,
   *   old versions) — sirf ek dafa ki muntaqli ke liye parha jata hai, kabhi likha nahi jata.
   *   openSqlite?: (file: string) => any,
   *   now?: () => number,
   *   log?: (level: string, msg: string) => void,
   * }} opts
   */
  constructor(opts) {
    super();
    this.rootDir = opts.rootDir;
    this.ownerProvider = opts.ownerProvider;
    this.enabled = opts.enabled !== false;
    this.watchFolder = opts.watch !== false;
    this.legacy = opts.legacy || {};
    this.openSqlite = opts.openSqlite || null;
    this.now = opts.now || (() => Date.now());
    this.log = opts.log || (() => {});
    this._store = null;
    this._owner = '';
    this._watcher = null;
    this._watchTimer = null;
    /** owner → muntaqli ka promise (har account ke liye ek dafa). */
    this._migrations = new Map();
  }

  _currentOwner() {
    try { return String(this.ownerProvider() || ''); } catch { return ''; }
  }

  /** Is waqt ke account ki yaadash. Sign out / band = null. */
  current() {
    if (!this.enabled) return null;
    const owner = this._currentOwner();
    if (!owner) {
      this._stopWatching();
      return null;
    }
    if (this._store && this._owner === owner) return this._store;
    const store = new FileMemory({ dir: path.join(this.rootDir, owner), now: this.now, log: this.log });
    try {
      store.ensure();
    } catch (err) {
      this.log('error', `[FileMemory] folder nahi bana (${err.message})`);
      return null;
    }
    store.on('changed', (info) => this.emit('changed', { ...info }));
    this._stopWatching();
    this._store = store;
    this._owner = owner;
    this._startWatching(store.dir);
    return store;
  }

  // ── Folder par nazar (New voice ka SDK seedha likhta hai) ─────────────────

  _startWatching(dir) {
    if (!this.watchFolder) return;
    try {
      this._watcher = fs.watch(dir, { recursive: true, persistent: false }, (_type, filename) => {
        const name = String(filename || '');
        // Chhupi cheezein (temp files, muntaqli ka backup / nishan) khabar nahi.
        if (name && name.split(/[\\/]/).some((part) => part.startsWith('.'))) return;
        clearTimeout(this._watchTimer);
        this._watchTimer = setTimeout(() => this.emit('changed', { op: 'external' }), WATCH_DEBOUNCE_MS);
        if (this._watchTimer.unref) this._watchTimer.unref();
      });
      this._watcher.on('error', (err) => {
        this.log('warn', `[FileMemory] folder ki nigrani band: ${err && err.message}`);
        this._stopWatching();
      });
    } catch (err) {
      this.log('warn', `[FileMemory] folder ki nigrani shuru nahi hui: ${err && err.message}`);
      this._watcher = null;
    }
  }

  _stopWatching() {
    clearTimeout(this._watchTimer);
    this._watchTimer = null;
    if (this._watcher) {
      try { this._watcher.close(); } catch { /* pehle hi band */ }
      this._watcher = null;
    }
  }

  /** App band ho rahi ho. */
  close() {
    this._stopWatching();
  }

  // ── Muntaqli ──────────────────────────────────────────────────────────────

  /** Is account ki muntaqli (pehli dafa chalegi, baad mein foran poori). */
  ready() {
    const store = this.current();
    if (!store) return Promise.resolve(false);
    const owner = this._owner;
    if (!this._migrations.has(owner)) {
      const job = this._migrate(store, owner).catch((err) => {
        this.log('error', `[FileMemory] muntaqli nakaam: ${err && err.message}`);
        this._migrations.delete(owner); // agli dafa dobara
        return false;
      });
      this._migrations.set(owner, job);
    }
    return this._migrations.get(owner);
  }

  async _migrate(store, owner) {
    const legacy = this.legacy;
    let legacyHermesDir = null;
    try {
      legacyHermesDir = typeof legacy.legacyHermesDir === 'function' ? legacy.legacyHermesDir() : legacy.legacyHermesDir || null;
    } catch { legacyHermesDir = null; }
    const result = await runMigration({
      store,
      accountDir: store.dir,
      sources: {
        legacyDb: legacy.memoryRoot ? path.join(legacy.memoryRoot, owner, 'memory.db') : null,
        legacyHermesDir: legacyHermesDir ? path.join(legacyHermesDir, 'memories') : null,
        bootstrapMemory: legacy.bootstrapMemory || null,
        memoriesJson: legacy.memoriesJson || null,
      },
      openSqlite: this.openSqlite,
      now: this.now(),
      log: this.log,
    });
    if (result && result.retry) this._migrations.delete(owner);
    return Boolean(result && result.ok);
  }

  /** Muntaqli ka intezar — magar baat-cheet ko kabhi zyada der nahi rokna. */
  async whenReady(ms = READY_WAIT_MS) {
    let timer;
    const limit = new Promise((resolve) => { timer = setTimeout(() => resolve('timeout'), ms); });
    try {
      return await Promise.race([this.ready(), limit]);
    } finally {
      clearTimeout(timer);
    }
  }

  status() {
    if (!this.enabled) return OFF('disabled');
    const store = this.current();
    if (!store) return OFF('signed_out');
    let migrated = false;
    try { migrated = Boolean(JSON.parse(fs.readFileSync(path.join(store.dir, MARKER_FILE), 'utf8')).done); } catch { /* abhi nahi */ }
    let stats = { notes: 0, bytes: 0 };
    try { stats = store.stats(); } catch { /* folder abhi parha nahi ja saka */ }
    return { ok: true, enabled: true, dir: store.dir, migrated, ...stats };
  }

  /** Baat-cheet shuru: muntaqli ka intezar, aur CLI ki tarah MEMORY.md na ho to backfill. */
  async _sessionStore() {
    if (!this.enabled) return { error: OFF('disabled') };
    if (!this.current()) return { error: OFF('signed_out') };
    await this.whenReady();
    const store = this.current();
    if (!store) return { error: OFF('signed_out') };
    try { store.ensureIndex(); } catch (err) { this.log('warn', `[FileMemory] backfill nahi hua: ${err.message}`); }
    return { store };
  }

  /**
   * Classic ka prompt hissa: official "# Memory" (memory tool wala) + aaj ki
   * tareekh + MEMORY.md (CLI ki 200 lines / 25,000 huroof hadd).
   * `indexOnly` = sirf fehrist (Awareness — us ke paas likhne ka tool nahi).
   * @param {{ indexOnly?: boolean }} [opts]
   */
  async card(opts = {}) {
    const { store, error } = await this._sessionStore();
    if (error) return error;
    const indexRaw = store.readIndex();
    const text = opts.indexOnly ? buildIndexOnly({ indexRaw }) : buildClassicCard({ indexRaw, now: this.now() });
    const notes = store.scan().length;
    return { ok: true, enabled: true, text, notes, chars: text.length };
  }

  /**
   * New voice (Claude Agent SDK): SDK ki apni auto memory isi folder par
   * (`autoMemoryDirectory`), aur prompt ka "# Memory" hissa (SDK custom prompt
   * par khud nahi bhejta). MEMORY.md SDK khud laata hai.
   */
  async agentSetup() {
    const { store, error } = await this._sessionStore();
    if (error) return error;
    const dir = official.dirForPrompt(store.dir);
    return { ok: true, enabled: true, dir, rules: buildAgentRules({ dir }), notes: store.scan().length };
  }

  /** Classic ka `memory` tool (Anthropic memory_20250818 ke hukm). */
  async command(input, ctx = {}) {
    const { store, error } = await this._toolStore();
    if (error) return error;
    return store.command(input, ctx);
  }

  /** Classic ka Grep (Claude Code ke Grep jaisa, sirf yaadash ke folder mein). */
  async grep(input) {
    const { store, error } = await this._toolStore();
    if (error) return error;
    return store.grep(input);
  }

  async _toolStore() {
    if (!this.enabled) return { error: { ok: false, text: 'Memory is turned off on this PC.' } };
    if (!this.current()) return { error: { ok: false, text: 'Memory is not available until they sign in to Stonic.' } };
    await this.whenReady();
    const store = this.current();
    if (!store) return { error: { ok: false, text: 'Memory is not available until they sign in to Stonic.' } };
    return { store };
  }

  // ── Memory page ───────────────────────────────────────────────────────────

  list() {
    const store = this.current();
    if (!store) return { ...OFF(this.enabled ? 'signed_out' : 'disabled'), notes: [] };
    return { ok: true, enabled: true, dir: store.dir, notes: store.list() };
  }

  read(notePath) {
    const store = this.current();
    if (!store) return { ok: false, text: '' };
    return store.read(notePath);
  }

  async save(notePath, text) {
    const store = this.current();
    if (!store) return { ok: false, text: 'Memory is not available.' };
    return store.writeNote(notePath, text);
  }

  async remove(notePath) {
    const store = this.current();
    if (!store) return { ok: false, text: 'Memory is not available.' };
    return store.deleteNote(notePath);
  }

  folder() {
    const store = this.current();
    return store ? store.dir : '';
  }
}

module.exports = { FileMemoryService, memoryRootFor };
