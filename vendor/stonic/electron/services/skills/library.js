'use strict';

/**
 * User ki skills — EK folder, Claude Code ke official Agent Skills format mein.
 *
 *   Documents\Stonic Data\skills\custom\<naam>\SKILL.md    (+ skill ki apni files)
 *
 * Har skill ek folder hai, aur folder ka naam hi skill ka naam: Claude Code plugin
 * skills ka naam folder se leta hai, SKILL.md ke `name` se nahi (naapa, CLI 2.1.318).
 * SKILL.md ke upar YAML frontmatter (`name`, `description`, `when_to_use` …), neeche
 * hidayat. Ye wohi format hai jo Claude Code / Agent SDK khud parhta hai.
 *
 * Dono voice assistants inhein Claude ke official raaste se use karte hain:
 *
 *   New + Classic ka Claude dimagh   stonic-voice (Claude Agent SDK) ko `forEngine()`
 *                                    milta hai: ek plugin folder (app ke data mein) jis
 *                                    mein sirf `.claude-plugin/plugin.json` aur `skills/`
 *                                    — user ke folder ka junction (kuch copy nahi hota).
 *                                    SDK `plugins` + `skills` (sirf chalu) se uthata hai;
 *                                    model ko naam + description dikhte hain aur zaroorat
 *                                    par Skill tool poori SKILL.md kholta hai.
 *   Classic ki awaaz (Gemini Live)   wohi fehrist prompt mein, aur `load_skill` wohi
 *                                    SKILL.md parhta hai (skills-handlers.js).
 *
 * Model ko sirf wo skill di jati hai jo Claude Code waqai load kar sake (`usable`):
 * frontmatter saaf YAML ho, description ho, aur aisi koi setting na ho jo Stonic ke
 * pehre se bahar kaam kare (apne hooks, apna model …). Jo na chal sake us par saaf
 * wajah (`problems`) — chup chaap gayab nahi.
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const yaml = require('js-yaml');

const SKILL_FILE = 'SKILL.md';
const STATE_FILE = '.state.json';
// Plugin ka naam: model ko skill "stonic:weekly-report" dikhti hai. Engine yehi naam
// plugin.json se parhta hai (stonic-voice config.py `_parse_skills`).
const PLUGIN_NAME = 'stonic';
// Agent Skills ka naam: chhote harf, adad, beech mein hyphen; 64 tak.
const NAME_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const MAX_NAME_CHARS = 64;
// Agent Skills: description 1,024 tak. Claude Code fehrist mein description +
// when_to_use ko 1,536 par kaat-ta hai.
const MAX_DESCRIPTION_CHARS = 1024;
const MAX_WHEN_CHARS = 500;
const MAX_BODY_CHARS = 200000;
const MAX_SKILL_BYTES = 512 * 1024;
const MAX_FILES_LISTED = 50;
// Pichle Stonic UI ki likhi SKILL.md mein sirf yehi keys hoti thin (har ek ek line, bina quotes).
const LEGACY_KEYS = new Set(['name', 'description', 'author', 'version', 'tags']);
const OWN_KEYS = new Set(['name', 'description', 'when_to_use']);

// Claude Code ki wo settings jo skill ko Stonic ke pehre / user ki settings se bahar le jati hain.
// Aisi skill model ko nahi di jati (fehrist se hi bahar = kabhi chalti nahi); user ko saaf wajah.
// Skills card mein khol kar save karne se ye settings hat jati hain (`save`).
const BLOCKED_KEYS = [
  ['hooks', 'It runs its own commands (hooks), which Stonic does not allow. Open it and save to remove them.'],
  ['model', 'It picks its own AI model instead of your voice settings. Open it and save to remove that line.'],
  ['effort', 'It sets its own thinking level instead of your voice settings. Open it and save to remove that line.'],
  ['context', 'It asks to run in a separate helper, which Stonic does not do. Open it and save to remove that line.'],
  ['agent', 'It asks for a separate helper agent, which Stonic does not do. Open it and save to remove that line.'],
];
const MANUAL_ONLY_KEY = 'disable-model-invocation';

const FRONTMATTER = /^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/;

function clip(text, limit) {
  const value = String(text || '').replace(/\s+/g, ' ').trim();
  return value.length <= limit ? value : `${value.slice(0, limit - 1).trimEnd()}…`;
}

/** Koi bhi naam → Agent Skills ka naam ("Write Weekly Report" → "write-weekly-report"). */
function slugify(text) {
  return String(text || '')
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, MAX_NAME_CHARS)
    .replace(/-+$/g, '');
}

function isValidName(name) {
  return typeof name === 'string' && name.length > 0 && name.length <= MAX_NAME_CHARS && NAME_RE.test(name);
}

/** Folder ka seedha bachcha (koi `..`, slash ya chhupa naam nahi) — raasta bahar na nikle. */
function isChildName(name) {
  return typeof name === 'string' && name.length > 0 && name.length <= 255
    && path.basename(name) === name && name !== '.' && name !== '..' && !name.startsWith('.')
    && !/[\\/\0]/.test(name);
}

function scalar(value) {
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return '';
}

/** SKILL.md → { block, body, data, error }. `error: 'missing'` = frontmatter hi nahi. */
function splitSkill(raw) {
  const text = String(raw || '').replace(/^﻿/, '');
  const match = text.match(FRONTMATTER);
  if (!match) return { block: null, body: text, data: null, error: 'missing' };
  const body = text.slice(match[0].length);
  try {
    const data = yaml.load(match[1]);
    if (data === null || data === undefined) return { block: match[1], body, data: {}, error: null };
    if (typeof data !== 'object' || Array.isArray(data)) {
      return { block: match[1], body, data: null, error: 'it is not a list of "key: value" settings' };
    }
    return { block: match[1], body, data, error: null };
  } catch (err) {
    return { block: match[1], body, data: null, error: String((err && (err.reason || err.message)) || err) };
  }
}

/**
 * Pichle Stonic UI ki frontmatter, usi ke purane tareeqe se (har line `key: value`).
 * Wo UI description bina quotes ke likhta tha — ":" ya "#" wali description saaf
 * YAML nahi rehti, aur Claude Code aisi skill chup chaap chhor deta hai (naapa,
 * CLI 2.1.318). Sirf tab jab block poora usi shakl ka ho; warna null.
 */
function legacyFields(block) {
  if (typeof block !== 'string') return null;
  const out = {};
  for (const line of block.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const colon = line.indexOf(':');
    if (colon <= 0 || /^\s/.test(line)) return null;
    const key = line.slice(0, colon).trim();
    if (!LEGACY_KEYS.has(key) || Object.prototype.hasOwnProperty.call(out, key)) return null;
    let value = line.slice(colon + 1).trim();
    if (key === 'tags') {
      const inner = value.startsWith('[') && value.endsWith(']') ? value.slice(1, -1) : value;
      out.tags = inner.split(',').map((item) => item.trim().replace(/^['"]|['"]$/g, '')).filter(Boolean);
      continue;
    }
    if (value.length >= 2 && ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'")))) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return Object.prototype.hasOwnProperty.call(out, 'description') ? out : null;
}

/** Saaf SKILL.md: Stonic ki teen keys pehle, baqi user ki keys jaisi thin. */
function renderSkill({ name, description, whenToUse, extra, body }) {
  const data = { name, description };
  if (whenToUse) data.when_to_use = whenToUse;
  for (const [key, value] of Object.entries(extra || {})) {
    if (!OWN_KEYS.has(key) && value !== undefined) data[key] = value;
  }
  const front = yaml.dump(data, { lineWidth: -1, noRefs: true, quotingType: '"' }).trimEnd();
  const text = String(body || '').replace(/\r\n/g, '\n').replace(/^\n+/, '').replace(/\s+$/, '');
  return `---\n${front}\n---\n\n${text}\n`;
}

/** Aadhi likhi file kabhi na parhi jaye (Claude Code / doosri window): pehle temp, phir rename. */
function writeAtomic(file, content) {
  const temp = `${file}.${process.pid}.${crypto.randomBytes(4).toString('hex')}.tmp`;
  fs.writeFileSync(temp, content, 'utf8');
  try {
    fs.renameSync(temp, file);
  } catch (err) {
    try { fs.unlinkSync(temp); } catch { /* temp pehle hi gayab */ }
    throw err;
  }
}

function readText(file) {
  try { return fs.readFileSync(file, 'utf8'); } catch { return null; }
}

function samePath(a, b) {
  const norm = (value) => path.resolve(String(value || '').replace(/^\\\\\?\\/, '')).replace(/[\\/]+$/, '');
  return process.platform === 'win32' ? norm(a).toLowerCase() === norm(b).toLowerCase() : norm(a) === norm(b);
}

class SkillsLibrary {
  /**
   * @param {object} options
   * @param {() => string} options.skillsDir  user ka skills folder (Documents\Stonic Data\skills\custom)
   * @param {string} options.pluginDir        app ka plugin folder (Claude Code `plugins` ka raasta)
   * @param {(file: string) => Promise<void>} [options.trash]  delete = Recycle Bin (Electron shell.trashItem)
   * @param {(level: string, message: string) => void} [options.log]
   */
  constructor({ skillsDir, pluginDir, trash, log } = {}) {
    if (typeof skillsDir !== 'function') throw new TypeError('skillsDir: function chahiye');
    this._skillsDir = skillsDir;
    this.pluginDir = pluginDir;
    this._trash = trash || null;
    this._log = log || (() => {});
  }

  skillsDir() {
    const dir = this._skillsDir();
    fs.mkdirSync(dir, { recursive: true });
    return dir;
  }

  // ── Parhna ──────────────────────────────────────────────────────────────

  /** Har skill (purani shakl wali pehle theek ho kar), naam ki tarteeb se. */
  list() {
    const root = this.skillsDir();
    this._migrate(root);
    let entries = [];
    try {
      entries = fs.readdirSync(root, { withFileTypes: true });
    } catch (err) {
      this._log('warn', `[Skills] Folder parha na ja saka: ${err.message}`);
      return [];
    }
    const skills = [];
    for (const entry of entries) {
      if (!entry.isDirectory() || entry.name.startsWith('.')) continue;
      const skill = this._load(root, entry.name);
      if (skill) skills.push(skill);
    }
    return skills.sort((a, b) => a.name.localeCompare(b.name));
  }

  /** Ek skill poori: fehrist wali maloomat + hidayat + folder ki doosri files. null = nahi mili. */
  read(name) {
    if (!isChildName(name)) return null;
    const root = this.skillsDir();
    const skill = this._load(root, name);
    if (!skill) return null;
    const raw = skill.size > MAX_SKILL_BYTES ? '' : (readText(skill.filePath) || '');
    const parts = splitSkill(raw);
    return {
      ...skill,
      body: parts.body.replace(/^(\r?\n)+/, ''),
      files: this._files(path.join(root, name)),
      folderPath: path.join(root, name),
    };
  }

  _load(root, folder) {
    const folderPath = path.join(root, folder);
    const file = path.join(folderPath, SKILL_FILE);
    let stat;
    try {
      stat = fs.statSync(file);
      if (!stat.isFile()) return null;
    } catch {
      return null;
    }
    const problems = [];
    if (!isValidName(folder)) {
      problems.push('Its name must be lowercase letters, numbers and hyphens (up to 64), like weekly-report. Open it and save to fix the name.');
    }
    let parts = { block: null, body: '', data: null, error: null };
    if (stat.size > MAX_SKILL_BYTES) {
      problems.push('SKILL.md is over 512 KB. Move long reference material into separate files in the skill folder.');
    } else {
      parts = splitSkill(readText(file) || '');
      if (parts.error === 'missing') {
        problems.push('SKILL.md needs a settings block at the top (between two --- lines) with a name and a description. Open it and save to add one.');
      } else if (parts.error) {
        problems.push(`The settings block at the top of SKILL.md could not be read (${clip(parts.error, 140)}). Open it and save to rewrite it.`);
      }
    }
    const data = parts.data || {};
    const description = scalar(data.description);
    const whenToUse = scalar(data.when_to_use);
    if (!parts.error && stat.size <= MAX_SKILL_BYTES) {
      if (!description) problems.push('Add a description, so the assistant knows when to use it.');
      if (!parts.body.trim()) problems.push('Add instructions: the steps the assistant should follow.');
    }
    for (const [key, why] of BLOCKED_KEYS) {
      if (Object.prototype.hasOwnProperty.call(data, key)) problems.push(why);
    }
    if (data[MANUAL_ONLY_KEY] === true) {
      problems.push('It is set to manual only, so the assistant never picks it. Open it and save to turn that off.');
    }
    const enabled = this._enabled(folderPath);
    return {
      name: folder,
      description,
      whenToUse,
      enabled,
      usable: enabled && problems.length === 0,
      problems,
      // Purane renderer code ke liye (folderName = skill ka naam).
      folderName: folder,
      filePath: file,
      modifiedAt: Math.round(stat.mtimeMs),
      size: stat.size,
    };
  }

  _enabled(folderPath) {
    const raw = readText(path.join(folderPath, STATE_FILE));
    if (raw === null) return true;
    try {
      return JSON.parse(raw).enabled !== false;
    } catch {
      return true;
    }
  }

  /** Skill ke folder ki doosri files (model zaroorat par Read karta hai), chhota sa naqsha. */
  _files(folderPath) {
    const out = [];
    const walk = (dir, prefix, depth) => {
      if (depth > 3 || out.length >= MAX_FILES_LISTED) return;
      let entries = [];
      try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
      for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
        if (out.length >= MAX_FILES_LISTED) return;
        if (entry.name.startsWith('.')) continue;
        const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
        if (entry.isDirectory()) walk(path.join(dir, entry.name), rel, depth + 1);
        else if (entry.isFile() && rel !== SKILL_FILE) out.push(rel);
      }
    };
    walk(folderPath, '', 0);
    return out;
  }

  // ── Purani shakl → official shakl (ek dafa, khud) ───────────────────────

  /**
   * Pichle Stonic builds ki skills ko Claude Code ke qabil banana:
   *   1. folder ke bahar padi `.md` file → apna folder + SKILL.md (Claude Code sirf folder parhta hai)
   *   2. ghalat folder ka naam ("My Skill") → saaf naam ("my-skill")
   *   3. purani UI ki frontmatter (bina quotes) → saaf YAML, wohi alfaaz
   * Kuch bhi mitaya nahi jata; jahan naya naam pehle se ho wahan kuch nahi hota.
   */
  _migrate(root) {
    let entries = [];
    try { entries = fs.readdirSync(root, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      if (entry.name.startsWith('.')) continue;
      try {
        if (entry.isFile() && entry.name.toLowerCase().endsWith('.md')) this._adoptLoose(root, entry.name);
      } catch (err) {
        this._log('warn', `[Skills] ${entry.name} ko folder mein na la saka: ${err.message}`);
      }
    }
    try { entries = fs.readdirSync(root, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      if (!entry.isDirectory() || entry.name.startsWith('.')) continue;
      let folder = entry.name;
      try {
        if (!fs.existsSync(path.join(root, folder, SKILL_FILE))) continue;
        if (!isValidName(folder)) folder = this._fixFolderName(root, folder) || folder;
        this._repairLegacy(path.join(root, folder, SKILL_FILE), folder);
      } catch (err) {
        this._log('warn', `[Skills] ${folder} theek na ho saki: ${err.message}`);
      }
    }
  }

  _adoptLoose(root, fileName) {
    const loose = path.join(root, fileName);
    const raw = readText(loose);
    if (raw === null) return;
    const parts = splitSkill(raw);
    const base = fileName.slice(0, -3);
    const named = scalar(parts.data && parts.data.name) || scalar(legacyFields(parts.block)?.name);
    const slug = slugify(named) || slugify(base);
    if (!isValidName(slug) || fs.existsSync(path.join(root, slug))) return;
    if (parts.error === 'missing') return; // frontmatter hi nahi: pehle bhi skill nahi thi
    fs.mkdirSync(path.join(root, slug));
    fs.renameSync(loose, path.join(root, slug, SKILL_FILE));
    // Purani on/off haalat (skills\.states\core-<naam>.json) skill ke saath.
    const oldState = path.join(root, '..', '.states', `core-${base}.json`);
    const state = readText(oldState);
    if (state !== null) {
      try {
        const enabled = JSON.parse(state).enabled !== false;
        fs.writeFileSync(path.join(root, slug, STATE_FILE), JSON.stringify({ enabled }), 'utf8');
        fs.unlinkSync(oldState);
      } catch { /* haalat na mili to skill chalu (default) */ }
    }
    this._log('info', `[Skills] ${fileName} → ${slug}/${SKILL_FILE}`);
  }

  _fixFolderName(root, folder) {
    const parts = splitSkill(readText(path.join(root, folder, SKILL_FILE)) || '');
    const slug = slugify(folder) || slugify(scalar(parts.data && parts.data.name));
    if (!isValidName(slug) || slug === folder) return null;
    const target = path.join(root, slug);
    if (folder.toLowerCase() === slug) {
      // Sirf harfon ka chhota / bara (Windows par wohi folder): do qadam mein.
      const temp = path.join(root, `${slug}.rename-${crypto.randomBytes(3).toString('hex')}`);
      fs.renameSync(path.join(root, folder), temp);
      fs.renameSync(temp, target);
    } else {
      if (fs.existsSync(target)) return null;
      fs.renameSync(path.join(root, folder), target);
    }
    this._log('info', `[Skills] folder "${folder}" → "${slug}"`);
    return slug;
  }

  _repairLegacy(file, folder) {
    const raw = readText(file);
    if (raw === null || raw.length > MAX_SKILL_BYTES) return;
    const parts = splitSkill(raw);
    const legacy = legacyFields(parts.block);
    if (!legacy) return;
    const strict = parts.data || {};
    const nameDiffers = scalar(strict.name) !== folder;
    const broken = Boolean(parts.error) || scalar(strict.description) !== legacy.description;
    if (!broken && !nameDiffers) return;
    const { name: _name, description, ...extra } = legacy;
    writeAtomic(file, renderSkill({ name: folder, description, extra, body: parts.body }));
    this._log('info', `[Skills] ${folder}: frontmatter saaf YAML mein (Claude Code ke qabil)`);
  }

  // ── Likhna ──────────────────────────────────────────────────────────────

  /**
   * Nayi skill ya purani mein tabdeeli. `originalName` = jis skill ko khola tha (naam
   * badla to folder bhi). User ki doosri frontmatter keys (license, metadata …) jaisi thin.
   * @returns {{ok: true, skill: object} | {ok: false, error: string}}
   */
  save({ name, description, whenToUse, body, originalName } = {}) {
    const slug = slugify(name);
    if (!isValidName(slug)) return { ok: false, error: 'Give the skill a name with letters or numbers, like weekly-report.' };
    const desc = String(description || '').trim();
    if (!desc) return { ok: false, error: 'Add a description, so the assistant knows when to use it.' };
    if (desc.length > MAX_DESCRIPTION_CHARS) return { ok: false, error: `Keep the description under ${MAX_DESCRIPTION_CHARS} characters.` };
    const when = String(whenToUse || '').trim();
    if (when.length > MAX_WHEN_CHARS) return { ok: false, error: `Keep "When to use" under ${MAX_WHEN_CHARS} characters.` };
    const text = String(body || '');
    if (!text.trim()) return { ok: false, error: 'Add instructions: the steps the assistant should follow.' };
    if (text.length > MAX_BODY_CHARS) return { ok: false, error: 'The instructions are too long. Move reference material into separate files in the skill folder.' };

    const root = this.skillsDir();
    const target = path.join(root, slug);
    const from = originalName ? String(originalName) : '';
    if (from && !isChildName(from)) return { ok: false, error: 'That skill could not be found.' };
    if (from && !fs.existsSync(path.join(root, from))) return { ok: false, error: 'That skill could not be found. It may have been deleted.' };

    if (from && from !== slug) {
      if (from.toLowerCase() === slug) {
        const temp = path.join(root, `${slug}.rename-${crypto.randomBytes(3).toString('hex')}`);
        fs.renameSync(path.join(root, from), temp);
        fs.renameSync(temp, target);
      } else {
        if (fs.existsSync(target)) return { ok: false, error: 'A skill with that name already exists.' };
        fs.renameSync(path.join(root, from), target);
      }
    } else if (!from && fs.existsSync(target)) {
      return { ok: false, error: 'A skill with that name already exists.' };
    }
    fs.mkdirSync(target, { recursive: true });

    const file = path.join(target, SKILL_FILE);
    const existing = splitSkill(readText(file) || '');
    const extra = { ...(existing.data || legacyFields(existing.block) || {}) };
    // Skills card mein save = Stonic mein chalne wali skill: pehre se bahar wali settings hat jati hain.
    for (const [key] of BLOCKED_KEYS) delete extra[key];
    delete extra[MANUAL_ONLY_KEY];
    writeAtomic(file, renderSkill({ name: slug, description: desc, whenToUse: when, extra, body: text }));
    return { ok: true, skill: this._load(root, slug) };
  }

  /** Skill on / off (model ki fehrist mein ho ya na ho). */
  setEnabled(name, enabled) {
    if (!isChildName(name)) return { ok: false, error: 'That skill could not be found.' };
    const folderPath = path.join(this.skillsDir(), name);
    if (!fs.existsSync(path.join(folderPath, SKILL_FILE))) return { ok: false, error: 'That skill could not be found.' };
    fs.writeFileSync(path.join(folderPath, STATE_FILE), JSON.stringify({ enabled: enabled !== false }), 'utf8');
    return { ok: true };
  }

  /** Skill ka folder Recycle Bin mein (wapas laya ja sake). */
  async remove(name) {
    if (!isChildName(name)) return { ok: false, error: 'That skill could not be found.' };
    const folderPath = path.join(this.skillsDir(), name);
    if (!fs.existsSync(folderPath)) return { ok: false, error: 'That skill could not be found.' };
    if (this._trash) {
      await this._trash(folderPath);
    } else {
      fs.rmSync(folderPath, { recursive: true, force: true });
    }
    return { ok: true };
  }

  // ── Claude ke dimagh ke liye (stonic-voice) ──────────────────────────────

  /**
   * Engine ka `skills` hissa: plugin folder + chalu skills ke naam + nishan + skills
   * ka folder (dimagh user ke kehne par nayi skill wahin likhta hai — Claude Code mein
   * skill "save" karne ka official tareeqa: folder + SKILL.md).
   * Koi chalne wali skill na ho to `names` khali (Skill tool nahi milta, sirf save ka raasta).
   * null = plugin ban na saka — engine pehle jaisa.
   */
  forEngine() {
    const usable = this.list().filter((skill) => skill.usable);
    const plugin = this.ensurePlugin();
    if (!plugin) return null;
    const version = crypto.createHash('sha1')
      .update(usable.map((skill) => `${skill.name}:${skill.modifiedAt}:${skill.size}`).join('|'))
      .digest('hex')
      .slice(0, 16);
    return { plugin, dir: this.skillsDir(), names: usable.map((skill) => skill.name), version };
  }

  /**
   * Claude Code plugin (official layout): `.claude-plugin/plugin.json` + `skills/`
   * — user ke skills folder ka junction. Plugin folder app ka apna hai: is mein
   * kabhi hooks / MCP / agents nahi hote, sirf skills ka raasta.
   * @returns {string|null} plugin folder, ya null (ban na saka)
   */
  ensurePlugin() {
    const root = this.pluginDir;
    if (!root) return null;
    try {
      const target = this.skillsDir();
      fs.mkdirSync(path.join(root, '.claude-plugin'), { recursive: true });
      const manifestPath = path.join(root, '.claude-plugin', 'plugin.json');
      const manifest = `${JSON.stringify({ name: PLUGIN_NAME, description: 'Skills the user saved in Stonic.' }, null, 2)}\n`;
      if (readText(manifestPath) !== manifest) writeAtomic(manifestPath, manifest);
      const link = path.join(root, 'skills');
      let stat = null;
      try { stat = fs.lstatSync(link); } catch { stat = null; }
      if (stat && !stat.isSymbolicLink()) {
        // Asli folder (junction nahi): kisi ne khud banaya — kabhi mitaya nahi jata.
        this._log('warn', `[Skills] ${link} junction nahi, asli folder hai — skills band`);
        return null;
      }
      if (stat) {
        let current = '';
        try { current = fs.readlinkSync(link); } catch { current = ''; }
        if (samePath(current, target)) return root;
        // Sirf junction hatta hai — user ki skills nahi.
        try { fs.unlinkSync(link); } catch { fs.rmdirSync(link); }
      }
      fs.symlinkSync(target, link, process.platform === 'win32' ? 'junction' : 'dir');
      return root;
    } catch (err) {
      this._log('warn', `[Skills] plugin folder ban na saka: ${err.message}`);
      return null;
    }
  }
}

module.exports = {
  SkillsLibrary,
  slugify,
  isValidName,
  splitSkill,
  legacyFields,
  renderSkill,
  PLUGIN_NAME,
  SKILL_FILE,
  MAX_DESCRIPTION_CHARS,
  MAX_WHEN_CHARS,
  MAX_NAME_CHARS,
};
