// Stonic ki yaadash — EK folder, Claude ki OFFICIAL memory ka tareeqa.
//
// ════════════════════════════════════════════════════════════════════════════
//  YE FILE KYA HAI
// ════════════════════════════════════════════════════════════════════════════
//
//   <appData>/Stonic AI/Memory/<account>/
//       MEMORY.md          fehrist — har yaad ki EK line. AI KHUD likhta hai
//                          (Claude Code ki tarah); code sirf parhta hai, aur
//                          sirf tab banata hai jab ye file hai hi nahi (CLI ka
//                          "backfill", official.js).
//       <koi bhi naam>.md  ek yaad = ek file, upar frontmatter (name /
//                          description / metadata.type).
//       .old-memory-backup\  .migration.json   — chhupi, AI ki pohanch se bahar.
//
//  Kaun is folder ko kaise chhoota hai:
//    - New voice (Claude Agent SDK): SDK ki apni auto memory — Read / Write /
//      Edit / Grep seedhe isi folder par (autoMemoryDirectory). Ye file us
//      raaste mein nahi aati; sirf watcher (service.js) tabdeeli dekhta hai.
//    - Classic voice (Gemini Live): Anthropic ka memory tool (memory_20250818)
//      — `command()` — aur Claude Code jaisa Grep — `grep()`. Jawab hoobahoo
//      official (official.js), aur CLI jo likhai ke baad karta hai (note par
//      mohar, fehrist ki yaad-dihani, purani yaad ka note) wohi yahan.
//    - Memory page aur muntaqli — hamari apni tahein; wo
//      fehrist mein apni line khud jorte / hatate hain (AI nahi likhta).
//
//  Code ki apni teen hifazatein (official docs ki "security considerations"):
//    1. Raasta: sirf /memories ke andar; "..", junction/symlink, chhupi files
//       aur Windows ke mana naam rad.
//    2. Har likhai poori ya bilkul nahi (chhupi temp file + rename), ek qataar mein.
//    3. File ki hadd (bohat bari file rad, kat-ti nahi).
//
//  Electron is file mein nahi — folder bahar se aata hai (`node --test`).

'use strict';

const fs = require('fs');
const path = require('path');
const { EventEmitter } = require('events');
const official = require('./official');

const { INDEX_FILE, VIRTUAL_ROOT, memoryTool: T } = official;

const LIMITS = Object.freeze({
  /** Ek file ki hadd (docs: "cap how large a file can grow"). IPC 64 KB tak hi laata hai. */
  fileChars: 60_000,
  /** Poore folder mein files — hifazati fuse. */
  files: 5_000,
  /** Raaste mein zyada se zyada itne hisse. */
  depth: 6,
  segmentChars: 120,
  nameChars: 120,
  descriptionChars: 400,
  /** Grep: lambi line ki jagah ginti (ripgrep --max-columns 500). */
  grepColumns: 500,
});

// Windows ke mana naam / huroof. Chhupi (".") cheezein bhi rad: muntaqli ka
// backup aur nishan wahin hain aur AI ko kabhi nahi chhoone.
const RESERVED_NAME = /^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$/i;
const BAD_CHARS = /[<>:"|?*\\]/;
const COMMANDS = ['view', 'create', 'str_replace', 'insert', 'delete', 'rename'];

class MemoryError extends Error {}

// ── Chhote madadgar ─────────────────────────────────────────────────────────

const oneLine = (value, max) => {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
};

const toLf = (text) => String(text ?? '').replace(/\r\n?/g, '\n');

const isControl = (code) => code < 32 || (code >= 127 && code <= 159);

function occurrences(haystack, needle) {
  const at = [];
  if (!needle) return at;
  for (let i = haystack.indexOf(needle); i !== -1; i = haystack.indexOf(needle, i + 1)) at.push(i);
  return at;
}

const lineOf = (text, index) => (text.slice(0, index).match(/\n/g) || []).length;

/** YAML ki qeemat: "JSON quotes" ya 'single quotes' utaar kar. */
function unquote(value) {
  const s = String(value ?? '').trim();
  if (s.length >= 2 && s.startsWith('"') && s.endsWith('"')) {
    try { return String(JSON.parse(s)); } catch { /* neeche saada tareeqa */ }
  }
  return s.replace(/^(["'])(.*)\1$/, '$2');
}

/** Frontmatter ki lines ginti (`---` se `---` tak), na ho to 0. */
function frontmatterLines(text) {
  const m = /^---[ \t]*\n[\s\S]*?\n---[ \t]*(?:\n|$)/.exec(text);
  return m ? (m[0].match(/\n/g) || []).length : 0;
}

/**
 * Note ka frontmatter: upar ki `key: value` aur `metadata:` ke andar ki.
 * Na ho to bhi kaam chalta hai (Memory page ke liye pehli asal line).
 *
 * @param {string} raw
 */
function parseNote(raw) {
  const text = toLf(raw).replace(/^﻿/, '');
  const match = /^---[ \t]*\n([\s\S]*?)\n---[ \t]*(?:\n|$)/.exec(text);
  const fields = {};
  const metadata = {};
  let body = text;
  if (match) {
    body = text.slice(match[0].length);
    let inMeta = false;
    for (const line of match[1].split('\n')) {
      const top = /^([A-Za-z][\w-]*)\s*:\s*(.*)$/.exec(line);
      if (top) {
        inMeta = top[1] === 'metadata' && top[2].trim() === '';
        if (!inMeta) fields[top[1].toLowerCase()] = unquote(top[2]);
        continue;
      }
      const nested = inMeta ? /^\s+["']?([\w-]+)["']?\s*:\s*(.*)$/.exec(line) : null;
      if (nested) metadata[nested[1]] = unquote(nested[2]);
    }
  }
  let description = fields.description || '';
  if (!description) {
    description = body.split('\n').map((l) => l.replace(/^[#>*\-\s]+/, '').trim()).find(Boolean) || '';
  }
  return {
    name: oneLine(fields.name || '', LIMITS.nameChars),
    description: oneLine(description, LIMITS.descriptionChars),
    type: oneLine(metadata.type || fields.type || '', 40),
    modified: metadata.modified || '',
    body,
    hasHeader: Boolean(match && fields.description),
    fields,
    metadata,
  };
}

/** Bijli jaye ya antivirus file pakar le — file phir bhi poori ya purani, aadhi kabhi nahi. */
function writeAtomic(file, text) {
  // Temp file chhupi (".") — AI ki fehrist / listing mein kabhi nahi aati.
  const tmp = path.join(path.dirname(file), `.${path.basename(file)}.tmp-${process.pid}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`);
  fs.writeFileSync(tmp, text, 'utf8');
  for (let attempt = 0; ; attempt += 1) {
    try {
      fs.renameSync(tmp, file);
      return;
    } catch (err) {
      const busy = err && ['EPERM', 'EBUSY', 'EACCES'].includes(err.code);
      if (!busy || attempt >= 4) {
        try { fs.unlinkSync(tmp); } catch { /* pehle hi gayi */ }
        throw err;
      }
      // Antivirus / indexer ne lamhe bhar ko pakri hai — thora ruk kar dobara.
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 30 * (attempt + 1));
    }
  }
}

/** Simple glob (`*`, `?`, `{a,b}`, `**`) → RegExp. "/" na ho to sirf file ke naam se milta hai. */
function globToRegExp(glob) {
  let re = '';
  for (let i = 0; i < glob.length; i += 1) {
    const ch = glob[i];
    if (ch === '*') {
      if (glob[i + 1] === '*') { re += '.*'; i += 1; if (glob[i + 1] === '/') i += 1; } else re += '[^/]*';
    } else if (ch === '?') re += '[^/]';
    else if (ch === '{') {
      const end = glob.indexOf('}', i);
      if (end === -1) { re += '\\{'; continue; }
      re += `(?:${glob.slice(i + 1, end).split(',').map((p) => p.replace(/[.+^$()|[\]\\]/g, '\\$&').replace(/\*/g, '[^/]*')).join('|')})`;
      i = end;
    } else re += ch.replace(/[.+^$()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${re}$`, 'i');
}

// ── Asal class ──────────────────────────────────────────────────────────────

class FileMemory extends EventEmitter {
  /**
   * @param {{ dir: string, now?: () => number, log?: (level: string, msg: string) => void }} opts
   */
  constructor(opts) {
    super();
    if (!opts || !opts.dir) throw new Error('FileMemory: dir zaroori hai');
    this.dir = path.resolve(opts.dir);
    this.now = opts.now || (() => Date.now());
    this.log = opts.log || (() => {});
    /** rel → { mtimeMs, size, name, description, type, hasHeader } — har dafa poori file dobara na parhni pare. */
    this._cache = new Map();
    this._chain = Promise.resolve();
    this._realRoot = null;
  }

  /** Folder bana do (pehli dafa) aur purani adhoori temp files saaf karo. MEMORY.md NAHI banti. */
  ensure() {
    fs.mkdirSync(this.dir, { recursive: true });
    this._realRoot = fs.realpathSync(this.dir);
    try {
      const cutoff = this.now() - 60_000;
      for (const name of fs.readdirSync(this.dir)) {
        if (!/\.tmp-/.test(name)) continue;
        const file = path.join(this.dir, name);
        try { if (fs.statSync(file).mtimeMs < cutoff) fs.unlinkSync(file); } catch { /* koi aur dekh raha hai */ }
      }
    } catch { /* safai zaroori nahi */ }
    return this;
  }

  get indexPath() {
    return path.join(this.dir, INDEX_FILE);
  }

  // ── Raaste ────────────────────────────────────────────────────────────────

  /**
   * Model ka raasta ("/memories/ammi.md") → folder ke andar ki asal jagah.
   * Official handler ki tarah: har raasta `/memories` se shuru, bahar jane wala rad.
   *
   * @param {unknown} input
   */
  resolve(input) {
    const raw = String(input ?? '').trim();
    let decoded = raw;
    try { decoded = decodeURIComponent(raw); } catch { /* "%" wala naam — jaisa hai */ }
    const unified = decoded.replace(/\\/g, '/');
    if (!unified.startsWith(VIRTUAL_ROOT) || (unified.length > VIRTUAL_ROOT.length && unified[VIRTUAL_ROOT.length] !== '/')) {
      throw new MemoryError(T.mustStartWithRoot(raw));
    }
    const parts = unified.slice(VIRTUAL_ROOT.length).split('/').filter(Boolean);
    if (parts.length > LIMITS.depth) throw new MemoryError(`Path ${raw} is nested too deep (at most ${LIMITS.depth} levels inside ${VIRTUAL_ROOT}).`);
    for (const seg of parts) {
      if (seg === '.' || seg === '..') throw new MemoryError(T.escapes(raw));
      const bad = seg.startsWith('.') || BAD_CHARS.test(seg) || /[. ]$/.test(seg) || RESERVED_NAME.test(seg)
        || seg.length > LIMITS.segmentChars || [...seg].some((ch) => isControl(ch.codePointAt(0)));
      if (bad) throw new MemoryError(`Path ${raw} is not a usable memory file name. Use letters, numbers and dashes, like ${VIRTUAL_ROOT}/sister-ayesha.md.`);
    }
    const rel = parts.join('/');
    const abs = parts.length ? path.join(this.dir, ...parts) : this.dir;
    if (abs !== this.dir && !abs.startsWith(this.dir + path.sep)) throw new MemoryError(T.escapes(raw));
    this._assertRealInside(abs, raw);
    return {
      rel,
      abs,
      isRoot: parts.length === 0,
      isIndex: parts.length === 1 && parts[0].toLowerCase() === INDEX_FILE.toLowerCase(),
      virtual: rel ? `${VIRTUAL_ROOT}/${rel}` : VIRTUAL_ROOT,
    };
  }

  /** Folder ke andar koi junction/symlink bahar le jaye — wo bhi rad. */
  _assertRealInside(abs, raw) {
    if (!this._realRoot) this.ensure();
    let probe = abs;
    while (!fs.existsSync(probe)) {
      const parent = path.dirname(probe);
      if (parent === probe) return;
      probe = parent;
    }
    const real = fs.realpathSync(probe);
    const root = this._realRoot;
    const win = process.platform === 'win32';
    const same = win ? real.toLowerCase() === root.toLowerCase() : real === root;
    const under = win ? real.toLowerCase().startsWith(root.toLowerCase() + path.sep) : real.startsWith(root + path.sep);
    if (!same && !under) throw new MemoryError(T.escapes(raw));
  }

  // ── Notes ki fehrist (Memory page, Grep, backfill) ───────────────────────

  /** Sab notes (MEMORY.md ke siwa), naye pehle. Badli hui file hi dobara parhi jati hai. */
  scan() {
    if (!this._realRoot) this.ensure();
    const rels = [];
    this._walk(this.dir, '', 0, rels, (name) => /\.md$/i.test(name));
    const seen = new Set();
    const notes = [];
    for (const rel of rels) {
      if (!rel.includes('/') && rel.toLowerCase() === INDEX_FILE.toLowerCase()) continue;
      seen.add(rel);
      const abs = path.join(this.dir, ...rel.split('/'));
      let stat;
      try { stat = fs.statSync(abs); } catch { continue; }
      let meta = this._cache.get(rel);
      if (!meta || meta.mtimeMs !== stat.mtimeMs || meta.size !== stat.size) {
        let raw = '';
        try { raw = fs.readFileSync(abs, 'utf8'); } catch { continue; }
        const parsed = parseNote(raw);
        meta = {
          mtimeMs: stat.mtimeMs,
          size: stat.size,
          name: parsed.name,
          description: parsed.description,
          type: parsed.type,
          hasHeader: parsed.hasHeader,
        };
        this._cache.set(rel, meta);
      }
      notes.push({ rel, path: `${VIRTUAL_ROOT}/${rel}`, ...meta });
    }
    for (const rel of [...this._cache.keys()]) if (!seen.has(rel)) this._cache.delete(rel);
    notes.sort((a, b) => b.mtimeMs - a.mtimeMs || a.rel.localeCompare(b.rel));
    return notes;
  }

  /** Chhupi cheezein, symlink aur node_modules chhor kar har file (`accept` ke mutabiq). */
  _walk(dirAbs, relPrefix, depth, out, accept = () => true) {
    let entries;
    try { entries = fs.readdirSync(dirAbs, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      if (entry.name.startsWith('.') || entry.isSymbolicLink() || entry.name === 'node_modules') continue;
      const rel = relPrefix ? `${relPrefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        if (depth + 1 < LIMITS.depth) this._walk(path.join(dirAbs, entry.name), rel, depth + 1, out, accept);
        continue;
      }
      if (entry.isFile() && accept(entry.name)) out.push(rel);
    }
  }

  // ── Fehrist (MEMORY.md) ───────────────────────────────────────────────────

  /** Disk wali MEMORY.md jaisi hai ('' = nahi hai). */
  readIndex() {
    try { return fs.readFileSync(this.indexPath, 'utf8'); } catch { return ''; }
  }

  /**
   * CLI ki tarah: MEMORY.md na ho magar notes hon to notes se fehrist bana do
   * (backfill — official.js). Pehle se ho to kuch nahi.
   * @returns {boolean} nayi bani?
   */
  ensureIndex() {
    if (fs.existsSync(this.indexPath)) return false;
    const notes = this.scan();
    if (notes.length === 0) return false;
    const { content } = official.backfillIndex(notes.map((n) => ({ rel: n.rel, description: n.description })));
    if (!content) return false;
    try {
      // "wx": beech mein kisi aur (SDK) ne bana di ho to us ki rehne do.
      fs.writeFileSync(this.indexPath, content, { encoding: 'utf8', flag: 'wx' });
    } catch (err) {
      if (err && err.code !== 'EEXIST') this.log('warn', `[FileMemory] MEMORY.md nahi bani: ${err.message}`);
      return false;
    }
    this.emit('changed', { op: 'index', path: `${VIRTUAL_ROOT}/${INDEX_FILE}` });
    return true;
  }

  /** Prompt mein jane wali fehrist — CLI ki 200 lines / 25,000 huroof hadd ke saath. */
  promptIndex() {
    return official.loadIndex(this.readIndex()).content;
  }

  /**
   * Hamari tahon (muntaqli, Memory page) ke liye: in notes ki line
   * fehrist ke aakhir mein jor do — jo pehle se kahin likhe hon wo chhor kar.
   * Line wohi shakl jo CLI ke backfill ki hai.
   * @param {Array<{ rel: string, description?: string }>} notes
   */
  addPointers(notes) {
    const current = this.readIndex();
    const lines = [];
    for (const note of notes || []) {
      if (this._indexMentions(current, note.rel) || lines.some((l) => l.includes(`](${note.rel})`))) continue;
      const line = official.backfillLine(note);
      if (line) lines.push(line);
    }
    if (lines.length === 0) return 0;
    const base = current && !current.endsWith('\n') ? `${current}\n` : current;
    writeAtomic(this.indexPath, `${base}${lines.join('\n')}\n`);
    return lines.length;
  }

  /** Fehrist se wo lines hata do jo is note ki taraf ishara karti hain. */
  removePointers(rel) {
    const current = this.readIndex();
    if (!current) return 0;
    const kept = [];
    let removed = 0;
    for (const line of current.split('\n')) {
      if (this._lineLinks(line, rel)) { removed += 1; continue; }
      kept.push(line);
    }
    if (removed > 0) writeAtomic(this.indexPath, kept.join('\n'));
    return removed;
  }

  _lineLinks(line, rel) {
    const target = String(rel || '');
    if (!target) return false;
    const links = [...line.matchAll(/\]\(\s*<?([^)>]+?)>?\s*\)/g)].map((m) => m[1].replace(/^\.?\//, '').replace(/^memories\//i, ''));
    const lower = target.toLowerCase();
    return links.some((link) => link.toLowerCase() === lower);
  }

  _indexMentions(indexText, rel) {
    return String(indexText || '').split('\n').some((line) => this._lineLinks(line, rel));
  }

  // ── Darwaza: Anthropic memory tool (memory_20250818) ─────────────────────

  /**
   * @param {{ command?: string, path?: string, file_text?: string, old_str?: string, new_str?: string,
   *   insert_line?: number, insert_text?: string, old_path?: string, new_path?: string,
   *   view_range?: number[] }} input
   * @param {{ sessionId?: string }} [ctx]  CLI ki tarah note par `originSessionId`
   * @returns {Promise<{ ok: boolean, text: string, changed?: string }>}
   */
  command(input, ctx = {}) {
    const args = input && typeof input === 'object' ? input : {};
    const cmd = String(args.command || '').trim();
    if (!COMMANDS.includes(cmd)) return Promise.resolve({ ok: false, text: T.unknownCommand(cmd || '(none)') });
    const run = () => {
      try {
        return this[`_${cmd}`](args, ctx);
      } catch (err) {
        if (err instanceof MemoryError) return { ok: false, text: err.message };
        this.log('error', `[FileMemory] ${cmd} nakaam: ${err && err.message}`);
        return { ok: false, text: `Error: the memory could not do that right now (${err && err.code ? err.code : 'error'}). Try again in a moment.` };
      }
    };
    if (cmd === 'view') return Promise.resolve(run());
    return this._serial(run);
  }

  _serial(fn) {
    const job = this._chain.then(fn, fn);
    this._chain = job.catch(() => {});
    return job;
  }

  _stat(abs) {
    try { return fs.statSync(abs); } catch { return null; }
  }

  _view(args) {
    const target = this.resolve(args.path);
    const stat = this._stat(target.abs);
    if (!stat) throw new MemoryError(T.notFound(target.virtual));

    if (stat.isDirectory()) {
      const lines = [`${official.formatListSize(stat.size)}\t${target.virtual}`];
      const collect = (dirAbs, relPrefix, depth) => {
        if (depth > 2) return;
        let entries;
        try { entries = fs.readdirSync(dirAbs, { withFileTypes: true }); } catch { return; }
        entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
        for (const entry of entries) {
          if (entry.name.startsWith('.') || entry.name === 'node_modules' || entry.isSymbolicLink()) continue;
          const rel = relPrefix ? `${relPrefix}/${entry.name}` : entry.name;
          const itemStat = this._stat(path.join(dirAbs, entry.name));
          if (!itemStat) continue;
          if (itemStat.isDirectory()) {
            lines.push(`${official.formatListSize(itemStat.size)}\t${target.virtual}/${rel}/`);
            if (depth < 2) collect(path.join(dirAbs, entry.name), rel, depth + 1);
          } else if (itemStat.isFile()) {
            lines.push(`${official.formatListSize(itemStat.size)}\t${target.virtual}/${rel}`);
          }
        }
      };
      collect(target.abs, '', 1);
      return { ok: true, text: `${T.dirHeader(target.virtual)}\n${lines.join('\n')}` };
    }

    const content = fs.readFileSync(target.abs, 'utf8');
    const lines = content.split('\n');
    if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
    if (lines.length > 999_999) throw new MemoryError(T.tooManyLines(target.virtual));
    let shown = lines;
    let start = 1;
    if (Array.isArray(args.view_range) && args.view_range.length === 2) {
      const [a, b] = args.view_range.map((n) => Math.trunc(Number(n)));
      if (Number.isFinite(a) && Number.isFinite(b)) {
        const from = Math.max(1, a) - 1;
        shown = lines.slice(from, b === -1 ? lines.length : b);
        start = from + 1;
      }
    }
    let body = '';
    let cut = 0;
    for (let i = 0; i < shown.length; i += 1) {
      const line = official.numberedLine(i + start, shown[i]);
      if (body.length + line.length + 1 > T.viewChars && body) { cut = i + start; break; }
      body += `${body ? '\n' : ''}${line}`;
    }
    if (cut) body += `\n[Truncated at line ${cut - 1} of ${lines.length} (16,000 characters) — use view_range to read the rest.]`;
    const age = target.isIndex ? '' : official.ageNote(stat.mtimeMs, this.now());
    return { ok: true, text: `${age}${T.fileHeader(target.virtual)}\n${body}` };
  }

  /** Likhne layak file ka raasta (folder ya root nahi). */
  _fileTarget(pathArg) {
    const target = this.resolve(pathArg);
    if (target.isRoot) throw new MemoryError(T.fileMissing(target.virtual));
    return target;
  }

  _checkSize(text, virtual) {
    if (text.length > LIMITS.fileChars) {
      throw new MemoryError(`Error: ${virtual} would be ${text.length} characters; the limit is ${LIMITS.fileChars}. Keep each memory to one fact and move detail into separate files.`);
    }
  }

  /** CLI jo likhai par karta hai: note par mohar (MEMORY.md par nahi). */
  _prepare(target, text, ctx) {
    return target.isIndex ? text : official.stampNote(text, { sessionId: ctx && ctx.sessionId, now: this.now() });
  }

  /** CLI jo MEMORY.md ki likhai ke baad kehta hai (yaad-dihani / ghalti), jawab ke saath. */
  _withIndexNotice(target, text) {
    if (!target.isIndex) return text;
    const notice = official.indexWriteNotice(this.readIndex());
    return notice ? `${text}\n\n${notice.text}` : text;
  }

  _done(target, op, text) {
    this.emit('changed', { op, path: target.virtual });
    return { ok: true, text: this._withIndexNotice(target, text), changed: target.virtual };
  }

  _create(args, ctx) {
    const target = this._fileTarget(args.path);
    if (fs.existsSync(target.abs)) throw new MemoryError(T.exists(target.virtual));
    const text = typeof args.file_text === 'string' ? args.file_text : '';
    this._checkSize(text, target.virtual);
    if (this.scan().length >= LIMITS.files) throw new MemoryError('Error: the memory directory is full. Merge or delete files that are no longer relevant first.');
    fs.mkdirSync(path.dirname(target.abs), { recursive: true });
    writeAtomic(target.abs, this._prepare(target, text, ctx));
    return this._done(target, 'create', T.created(target.virtual));
  }

  _readExisting(target, missing) {
    const stat = this._stat(target.abs);
    if (!stat || !stat.isFile()) throw new MemoryError(missing(target.virtual));
    return fs.readFileSync(target.abs, 'utf8');
  }

  _str_replace(args, ctx) {
    const target = this._fileTarget(args.path);
    const content = this._readExisting(target, T.fileMissing);
    const oldStr = typeof args.old_str === 'string' ? args.old_str : '';
    const newStr = typeof args.new_str === 'string' ? args.new_str : '';
    const hits = occurrences(content, oldStr);
    if (hits.length === 0) throw new MemoryError(T.noMatch(oldStr, target.virtual));
    if (hits.length > 1) throw new MemoryError(T.manyMatches(oldStr, hits.map((i) => lineOf(content, i) + 1)));
    const next = content.slice(0, hits[0]) + newStr + content.slice(hits[0] + oldStr.length);
    this._checkSize(next, target.virtual);
    const written = this._prepare(target, next, ctx);
    writeAtomic(target.abs, written);
    // Snippet reference handler ki tarah (badli line ke 2 upar / 2 neeche) —
    // mohar se frontmatter ki lines barhi hon to line number usi hisaab se.
    let changedLine = lineOf(next, hits[0]);
    if (changedLine >= frontmatterLines(next)) changedLine += frontmatterLines(written) - frontmatterLines(next);
    const finalLines = written.split('\n');
    const from = Math.max(0, changedLine - 2);
    const to = Math.min(finalLines.length, changedLine + 3);
    const snippet = [];
    for (let n = from + 1; n <= to; n += 1) snippet.push(official.numberedLine(n, finalLines[n - 1]));
    return this._done(target, 'update', `${T.edited} ${T.snippetHeader}\n${snippet.join('\n')}`);
  }

  _insert(args, ctx) {
    const target = this._fileTarget(args.path);
    const content = this._readExisting(target, T.pathMissing);
    const lines = content.split('\n');
    if (lines[lines.length - 1] === '') lines.pop();
    const at = Number(args.insert_line);
    if (!Number.isInteger(at) || at < 0 || at > lines.length) throw new MemoryError(T.badInsertLine(args.insert_line, lines.length));
    const insertText = typeof args.insert_text === 'string' ? args.insert_text : '';
    lines.splice(at, 0, insertText.replace(/\n+$/, ''));
    let next = lines.join('\n');
    if (!next.endsWith('\n')) next += '\n';
    this._checkSize(next, target.virtual);
    writeAtomic(target.abs, this._prepare(target, next, ctx));
    return this._done(target, 'update', T.inserted(target.virtual));
  }

  _delete(args) {
    const target = this.resolve(args.path);
    if (target.isRoot) throw new MemoryError(T.cannotDeleteRoot);
    const stat = this._stat(target.abs);
    if (!stat) throw new MemoryError(T.pathMissing(target.virtual));
    if (stat.isDirectory()) fs.rmSync(target.abs, { recursive: true, force: true });
    else fs.unlinkSync(target.abs);
    this.emit('changed', { op: 'delete', path: target.virtual });
    return { ok: true, text: T.deleted(target.virtual), changed: target.virtual };
  }

  _rename(args) {
    const from = this.resolve(args.old_path);
    const to = this.resolve(args.new_path);
    if (from.isRoot) throw new MemoryError(T.cannotRenameRoot);
    if (to.isRoot) throw new MemoryError(T.destinationExists(to.virtual));
    if (!this._stat(from.abs)) throw new MemoryError(T.pathMissing(from.virtual));
    if (this._stat(to.abs)) throw new MemoryError(T.destinationExists(to.virtual));
    fs.mkdirSync(path.dirname(to.abs), { recursive: true });
    fs.renameSync(from.abs, to.abs);
    this.emit('changed', { op: 'rename', path: to.virtual, from: from.virtual });
    return { ok: true, text: this._withIndexNotice(to, T.renamed(from.virtual, to.virtual)), changed: to.virtual };
  }

  // ── Grep (Claude Code ke Grep tool jaisa, sirf is folder mein) ────────────

  /**
   * @param {{ pattern?: string, path?: string, glob?: string, output_mode?: string,
   *   head_limit?: number, offset?: number, context?: number, case_insensitive?: boolean }} input
   * @returns {{ ok: boolean, text: string }}
   */
  grep(input = {}) {
    try {
      return { ok: true, text: this._grep(input || {}) };
    } catch (err) {
      if (err instanceof MemoryError) return { ok: false, text: err.message };
      this.log('error', `[FileMemory] grep nakaam: ${err && err.message}`);
      return { ok: false, text: 'Error: the memory search did not work right now. Try again in a moment.' };
    }
  }

  _grep(input) {
    let source = typeof input.pattern === 'string' ? input.pattern : '';
    if (!source) throw new MemoryError('Error: pattern is required.');
    let flags = 'u';
    if (/^\(\?i\)/.test(source)) { source = source.slice(4); flags += 'i'; }
    if (input.case_insensitive === true || input['-i'] === true) flags += flags.includes('i') ? '' : 'i';
    let regex;
    try {
      regex = new RegExp(source, flags);
    } catch {
      // ripgrep ki kuch likhai JS ke Unicode mode mein nahi chalti — saade mode mein dobara.
      try { regex = new RegExp(source, flags.replace('u', '')); } catch (err) {
        throw new MemoryError(`Error: invalid regular expression: ${oneLine(err.message, 200)}`);
      }
    }
    const mode = ['content', 'count', 'files_with_matches'].includes(input.output_mode) ? input.output_mode : 'files_with_matches';
    const limitRaw = input.head_limit === undefined || input.head_limit === null ? official.grepResult.defaultLimit : Number(input.head_limit);
    const limit = Number.isInteger(limitRaw) && limitRaw >= 0 ? limitRaw : official.grepResult.defaultLimit;
    const offset = Number.isInteger(Number(input.offset)) && Number(input.offset) > 0 ? Number(input.offset) : 0;
    const context = Math.max(0, Math.min(20, Math.trunc(Number(input.context ?? input['-C'] ?? 0)) || 0));

    const base = this.resolve(input.path || VIRTUAL_ROOT);
    const baseStat = this._stat(base.abs);
    if (!baseStat) throw new MemoryError(`Path does not exist: ${base.virtual}.`);
    let rels = [];
    if (baseStat.isDirectory()) this._walk(base.abs, base.rel, base.rel ? base.rel.split('/').length : 0, rels);
    else rels = [base.rel];
    if (typeof input.glob === 'string' && input.glob.trim()) {
      const globs = input.glob.split(/\s+/).flatMap((g) => (g.includes('{') ? [g] : g.split(','))).filter(Boolean).map(globToRegExp);
      rels = rels.filter((rel) => globs.some((g) => g.test(rel) || g.test(rel.split('/').pop())));
    }

    const files = [];
    for (const rel of rels) {
      const abs = path.join(this.dir, ...rel.split('/'));
      let text = '';
      try { text = toLf(fs.readFileSync(abs, 'utf8')); } catch { continue; }
      const lines = text.split('\n');
      if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
      const matched = [];
      lines.forEach((line, i) => { regex.lastIndex = 0; if (regex.test(line)) matched.push(i); });
      if (matched.length === 0) continue;
      let mtimeMs = 0;
      try { mtimeMs = fs.statSync(abs).mtimeMs; } catch { /* */ }
      files.push({ rel, virtual: `${VIRTUAL_ROOT}/${rel}`, lines, matched, mtimeMs });
    }
    // Claude Code ka Grep: files naye pehle.
    files.sort((a, b) => b.mtimeMs - a.mtimeMs || a.rel.localeCompare(b.rel));

    const page = (items) => {
      const sliced = limit === 0 ? items.slice(offset) : items.slice(offset, offset + limit);
      const limited = limit !== 0 && items.length - offset > limit;
      const note = limited || offset ? [limited ? `limit: ${limit}` : '', offset ? `offset: ${offset}` : ''].filter(Boolean).join(', ') : '';
      return { sliced, note };
    };

    const G = official.grepResult;
    if (mode === 'files_with_matches') {
      if (files.length === 0) return G.noFiles;
      const { sliced, note } = page(files.map((f) => f.virtual));
      return sliced.length ? G.files(sliced, note ? `[Showing results with pagination = ${note}]` : '') : G.noFiles;
    }
    if (mode === 'count') {
      const rows = files.map((f) => `${f.virtual}:${f.matched.length}`);
      const total = files.reduce((sum, f) => sum + f.matched.length, 0);
      const { sliced, note } = page(rows);
      if (files.length === 0) return G.noMatches;
      return G.count(sliced.join('\n'), total, files.length, note);
    }
    const out = [];
    for (const f of files) {
      const show = new Set();
      for (const i of f.matched) for (let j = Math.max(0, i - context); j <= Math.min(f.lines.length - 1, i + context); j += 1) show.add(j);
      let prev = -2;
      for (const j of [...show].sort((a, b) => a - b)) {
        if (context > 0 && prev >= 0 && j > prev + 1) out.push('--');
        const line = f.lines[j];
        const shown = line.length > LIMITS.grepColumns ? `[Omitted long line with ${f.matched.includes(j) ? 1 : 0} matches]` : line;
        out.push(`${f.virtual}${f.matched.includes(j) ? ':' : '-'}${j + 1}${f.matched.includes(j) ? ':' : '-'}${shown}`);
        prev = j;
      }
    }
    if (out.length === 0) return G.noMatches;
    const { sliced, note } = page(out);
    return G.content(sliced.join('\n') || G.noMatches, note);
  }

  // ── Hamari tahein: muntaqli, Memory page ─────────────────────────────────

  /**
   * Ek saath kai notes (sirf muntaqli). Har note wohi hifazati jaanch se
   * guzarta hai; `mtimeMs` diya ho to file ka waqt wohi rakha jata hai. Aakhir
   * mein fehrist: na ho to CLI wala backfill, ho to naye notes ki lines.
   *
   * @param {Array<{ rel: string, text: string, mtimeMs?: number }>} notes
   * @returns {Promise<{ written: number, failed: Array<{ rel: string, error: string }> }>}
   */
  bulkWrite(notes) {
    return this._serial(() => {
      let written = 0;
      const failed = [];
      const done = [];
      for (const note of notes) {
        try {
          const target = this._fileTarget(`${VIRTUAL_ROOT}/${note.rel}`);
          if (target.isIndex) throw new MemoryError('MEMORY.md is the index');
          const text = toLf(note.text);
          this._checkSize(text, target.virtual);
          fs.mkdirSync(path.dirname(target.abs), { recursive: true });
          writeAtomic(target.abs, text);
          if (Number.isFinite(note.mtimeMs) && note.mtimeMs > 0) {
            const at = new Date(note.mtimeMs);
            try { fs.utimesSync(target.abs, at, at); } catch { /* waqt na badla to bhi note salamat */ }
          }
          written += 1;
          done.push({ rel: target.rel, description: parseNote(text).description });
        } catch (err) {
          failed.push({ rel: note.rel, error: err.message });
        }
      }
      if (written > 0) {
        try {
          if (!this.ensureIndex()) this.addPointers(done);
        } catch (err) {
          this.log('warn', `[FileMemory] muntaqli ke baad fehrist nahi bani: ${err.message}`);
        }
        this.emit('changed', { op: 'import', count: written });
      }
      return { written, failed };
    });
  }

  list() {
    return this.scan().map((n) => ({
      path: n.path,
      name: n.name || n.rel.replace(/\.md$/i, ''),
      description: n.description,
      type: n.type,
      updatedAt: n.mtimeMs,
      size: n.size,
    }));
  }

  /** Memory page: note ka poora matn (MEMORY.md bhi). */
  read(pathArg) {
    try {
      const target = this._fileTarget(pathArg);
      const stat = this._stat(target.abs);
      if (!stat || !stat.isFile()) return { ok: false, text: T.notFound(target.virtual) };
      return { ok: true, text: fs.readFileSync(target.abs, 'utf8') };
    } catch (err) {
      return { ok: false, text: err.message };
    }
  }

  /**
   * Memory page se user ne note badla (poora likh kar). CLI ki tarah mohar;
   * naya note ho to fehrist mein us ki line.
   */
  writeNote(pathArg, text, ctx = {}) {
    return this._serial(() => {
      try {
        const target = this._fileTarget(pathArg);
        const body = toLf(text);
        this._checkSize(body, target.virtual);
        const existed = fs.existsSync(target.abs);
        fs.mkdirSync(path.dirname(target.abs), { recursive: true });
        writeAtomic(target.abs, this._prepare(target, body, ctx));
        if (!existed && !target.isIndex) this.addPointers([{ rel: target.rel, description: parseNote(body).description }]);
        this.emit('changed', { op: existed ? 'update' : 'create', path: target.virtual });
        return { ok: true, text: `Saved ${target.virtual}.`, changed: target.virtual };
      } catch (err) {
        return { ok: false, text: err.message };
      }
    });
  }

  /** Memory page se user ne note mitaya — fehrist se us ki line bhi. */
  deleteNote(pathArg) {
    return this._serial(() => {
      try {
        const target = this._fileTarget(pathArg);
        if (target.isIndex) return { ok: false, text: 'The index cannot be deleted from here.' };
        const stat = this._stat(target.abs);
        if (!stat || !stat.isFile()) return { ok: false, text: T.pathMissing(target.virtual) };
        fs.unlinkSync(target.abs);
        this.removePointers(target.rel);
        this.emit('changed', { op: 'delete', path: target.virtual });
        return { ok: true, text: T.deleted(target.virtual), changed: target.virtual };
      } catch (err) {
        return { ok: false, text: err.message };
      }
    });
  }

  stats() {
    const notes = this.scan();
    return { notes: notes.length, bytes: notes.reduce((sum, n) => sum + n.size, 0) };
  }
}

module.exports = {
  FileMemory,
  MemoryError,
  parseNote,
  writeAtomic,
  globToRegExp,
  LIMITS,
  INDEX_FILE,
  VIRTUAL_ROOT,
  COMMANDS,
};
