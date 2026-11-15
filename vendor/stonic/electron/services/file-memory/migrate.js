// Purani yaadein naye folder mein — EK dafa, har account ke liye.
//
// ════════════════════════════════════════════════════════════════════════════
//  KYA, KAHAN SE
// ════════════════════════════════════════════════════════════════════════════
//
//   1. Memory Core   %APPDATA%\stonic_gen3\memory\<account>\memory.db
//                    chalti yaadein (har ek apna note), khule waade, purani
//                    baat-cheet ke khulase (hafte ka ek note)
//   2. Hermes        %LOCALAPPDATA%\hermes\memories\MEMORY.md + USER.md
//                    (purane versions ki legacy Hermes yaadash; ab Stonic
//                    Hermes nahi chalata — ye sirf parhi jati hai)
//   3. Purana profile Documents\Stonic Data\MEMORY.md (update_user_profile)
//   4. memories.json %APPDATA%\stonic_gen3\memories.json (sab se purana)
//
//  Wohi baat do jagah ho (Hermes ki baat Memory Core mein pehle se import thi)
//  to note ek hi banta hai.
//
// ── TEEN WAADE ──────────────────────────────────────────────────────────────
//
//  1. ASAL FILES KO HAATH NAHI. Pehle sab ki copy `.old-memory-backup\<waqt>\`
//     mein; database usi copy ki ek aur temp copy se parha jata hai.
//  2. KUCH GUM NAHI. Har chalti yaad apna note banti hai — milaya ya chhota
//     nahi jata. Kachi baat-cheet (har jumla) naye folder mein nahi aati, magar
//     backup wali database mein poori mehfooz hai.
//  3. DOBARA CHALE TO DOHRA NAHI. Naam tay-shuda tareeqe se bante hain, is liye
//     beech mein ruki muntaqli dobara chal kar wohi files dobara likhti hai.
//     Kaam poora hone par `.migration.json` — phir kabhi nahi chalti.

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { parseNote } = require('./store');

const MIGRATION_VERSION = 1;
const MARKER_FILE = '.migration.json';
const BACKUP_DIR = '.old-memory-backup';
const ORIGIN = 'old-stonic-memory';
const MAX_ATTEMPTS = 3;
const PART_CHARS = 18_000;
const DESCRIPTION_CHARS = 240;

// ── Chhote madadgar ─────────────────────────────────────────────────────────

const clean = (v) => String(v ?? '').replace(/\s+/g, ' ').trim();

const oneLine = (v, max = DESCRIPTION_CHARS) => {
  const text = clean(v);
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
};

const pad = (n) => String(n).padStart(2, '0');
const dateOf = (ms) => {
  if (!Number.isFinite(ms) || ms <= 0) return '';
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
const dateTimeOf = (ms) => {
  if (!Number.isFinite(ms) || ms <= 0) return '';
  const d = new Date(ms);
  return `${dateOf(ms)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** Matlab ki pehchan: chhote bare harf, nishaan aur faasle ka farq nahi. */
const sameKey = (text) => String(text ?? '').normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();

const shortHash = (value) => crypto.createHash('sha1').update(String(value)).digest('hex').slice(0, 6);

const STOP_WORDS = new Set([
  'a', 'an', 'the', 'and', 'or', 'but', 'of', 'to', 'in', 'on', 'for', 'at', 'by', 'with', 'from', 'as', 'into',
  'is', 'are', 'was', 'were', 'be', 'been', 'has', 'have', 'had', 'do', 'does', 'did', 'will', 'would', 'should',
  'that', 'this', 'these', 'those', 'it', 'its', 'he', 'she', 'they', 'them', 'his', 'her', 'their', 'him',
  'user', 'users', 'wants', 'want', 'said', 'says', 'told', 'tells', 'likes', 'which', 'who', 'when', 'also', 'very',
  'ne', 'ki', 'ka', 'ke', 'hai', 'hain', 'ko', 'se', 'aur', 'bhi', 'ye', 'woh', 'wo',
]);
const RESERVED = /^(con|prn|aux|nul|com[0-9]|lpt[0-9])$/;

/** Angrezi / Roman matn se chhota naam ("usman-bought-new-motorcycle"). Urdu script se kuch na bane to khali. */
function slugFrom(text) {
  const ascii = String(text ?? '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase();
  const words = (ascii.match(/[a-z0-9]+/g) || []).filter((w) => w.length > 1 && !STOP_WORDS.has(w));
  let slug = '';
  for (const word of words) {
    const next = slug ? `${slug}-${word}` : word;
    if (next.length > 48) break;
    slug = next;
    if (slug.split('-').length >= 6) break;
  }
  if (slug.length < 4) return '';
  return RESERVED.test(slug) ? `${slug}-note` : slug;
}

const PREFIX_BY_TYPE = {
  identity: 'about-you',
  preference: 'likes',
  instruction: 'how-to-talk',
  project: 'project',
  opinion: 'opinion',
  relationship: 'people',
};

/** "female_girlfriend" → "Girlfriend". */
function characterLabel(id) {
  const raw = String(id || '').replace(/^(female|male)_/, '').replace(/_/g, ' ').trim();
  if (!raw) return '';
  if (raw === 'custom') return 'a custom character';
  return raw.replace(/\b\w/g, (c) => c.toUpperCase());
}

function uniqueRel(base, taken) {
  const stem = base || 'note';
  let rel = `${stem}.md`;
  for (let i = 2; taken.has(rel.toLowerCase()); i += 1) rel = `${stem}-${i}.md`;
  taken.add(rel.toLowerCase());
  return rel;
}

/** Purani yaad ki qism → Claude Code ki chaar qismon (user / feedback / project / reference) mein se ek. */
const OFFICIAL_TYPE = {
  identity: 'user',
  preference: 'user',
  opinion: 'user',
  relationship: 'user',
  instruction: 'feedback',
  project: 'project',
};

/**
 * Official frontmatter (name / description / metadata.type) — description JSON
 * quotes mein, taake ":" wali baat YAML na tore. `origin` metadata mein:
 * dobara muntaqli par pata chale ke ye note muntaqli ka hai.
 */
function header(name, description, type = 'user') {
  return [
    '---',
    `name: ${name}`,
    `description: ${JSON.stringify(oneLine(description))}`,
    'metadata:',
    `  type: ${type}`,
    `  origin: ${ORIGIN}`,
    '---',
    '',
  ].join('\n');
}

// ── Purane zariye parhna ────────────────────────────────────────────────────

/**
 * Memory Core ki database (COPY) se teen cheezein. `openSqlite(file)` ek
 * better-sqlite3 jaisi cheez lautaye (`prepare().all()`, `close()`).
 */
function readLegacyDb(openSqlite, dbFile) {
  const db = openSqlite(dbFile);
  try {
    const has = (table) => Boolean(db.prepare("select name from sqlite_master where type = 'table' and name = ?").get(table));
    const memories = has('memories')
      ? db.prepare(`select uid, type, text_original, text_en, pinned, scope, mentioned_at, recorded_at, updated_at
                    from memories where deleted = 0 and status = 'active' order by id`).all()
      : [];
    const intentions = has('intentions')
      ? db.prepare(`select uid, what, what_en, due_ts, character_id, created_at
                    from intentions where status = 'open' order by id`).all()
      : [];
    const episodes = has('episodes')
      ? db.prepare(`select uid, title, summary_en, character_id, start_ts from episodes order by start_ts, id`).all()
      : [];
    return { memories, intentions, episodes };
  } finally {
    try { db.close(); } catch { /* pehle hi band */ }
  }
}

/** Legacy Hermes / purani MEMORY.md ki "§" se alag baatein → saaf jumle (+ jo tareekh likhi thi). */
function parseEntries(text) {
  return String(text ?? '')
    .split(/\n?§\n?/)
    .map((raw) => {
      let t = raw.trim().replace(/^[-*]\s+/, '');
      let noted = '';
      const d = /^\[(\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4})\]\s*/.exec(t);
      if (d) { noted = d[1]; t = t.slice(d[0].length); }
      const c = /^\[([A-Za-z][A-Za-z/ _-]{1,30})\]\s*/.exec(t);
      if (c) t = t.slice(c[0].length);
      return { text: clean(t), noted };
    })
    .filter((e) => e.text && !/^stonic memory system initiali[sz]ed\.?$/i.test(e.text));
}

function readText(file) {
  try { return file && fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : ''; } catch { return ''; }
}

/** Legacy Hermes, purana profile aur memories.json — sab ki baatein ek list mein, apne zariye ke naam ke saath. */
function readExtras(sources) {
  const out = [];
  const add = (entries, label) => { for (const e of entries) out.push({ ...e, source: label }); };
  if (sources.legacyHermesDir) {
    add(parseEntries(readText(path.join(sources.legacyHermesDir, 'USER.md'))), 'Hermes notes about you');
    add(parseEntries(readText(path.join(sources.legacyHermesDir, 'MEMORY.md'))), 'Hermes notes');
  }
  if (sources.bootstrapMemory) add(parseEntries(readText(sources.bootstrapMemory)), 'Stonic profile notes');
  if (sources.memoriesJson) {
    try {
      const list = JSON.parse(readText(sources.memoriesJson) || '[]');
      if (Array.isArray(list)) {
        add(list
          .map((m) => ({ text: clean(m && (m.content || m.text)), noted: m && m.timestamp ? dateOf(Number(m.timestamp)) : '' }))
          .filter((e) => e.text), 'Stonic saved memories');
      }
    } catch { /* kharab file — chhor do, backup mein hai */ }
  }
  return out;
}

// ── Mansooba: kaun sa note kis naam se ──────────────────────────────────────

/**
 * Pura mansooba — koi file nahi likhi jati, is liye test seedha ho sakta hai.
 *
 * @param {{ legacy: { memories: any[], intentions: any[], episodes: any[] }, extras: any[],
 *           taken: Set<string>, now: number }} input
 * @returns {{ notes: Array<{ rel: string, text: string, mtimeMs: number }>, counts: Record<string, number> }}
 */
function planMigration({ legacy, extras, taken, now }) {
  const notes = [];
  const seen = new Set();
  const counts = { memories: 0, followUps: 0, conversationNotes: 0, extras: 0, duplicatesSkipped: 0 };

  // 1. Chalti yaadein — har ek apna note. Zaroori wali (pin, pehchan, hidayat)
  //    ko aaj ka waqt milta hai taake fehrist mein upar rahein; baqi apni asal tareekh par.
  for (const row of legacy.memories || []) {
    const original = clean(row.text_original);
    const english = clean(row.text_en);
    const main = english || original;
    if (!main) continue;
    const key = sameKey(main);
    if (seen.has(key)) { counts.duplicatesSkipped += 1; continue; }
    seen.add(key);
    if (original) seen.add(sameKey(original));
    const base = slugFrom(english) || slugFrom(original) || `${PREFIX_BY_TYPE[row.type] || 'note'}-${shortHash(row.uid || main)}`;
    const rel = uniqueRel(base, taken);
    const meta = [`Kind: ${row.type || 'note'}`];
    if (row.pinned) meta.push('marked important');
    if (typeof row.scope === 'string' && row.scope.startsWith('character:')) {
      meta.push(`told to ${characterLabel(row.scope.slice('character:'.length)) || 'one character'}`);
    }
    const noted = dateOf(Number(row.mentioned_at) || Number(row.recorded_at));
    const lines = [header(rel.replace(/\.md$/, ''), main, OFFICIAL_TYPE[row.type] || 'user'), original || english];
    if (english && original && english !== original) lines.push('', `In English: ${english}`);
    lines.push('', `From Stonic's earlier memory${noted ? ` — first noted ${noted}` : ''}. ${meta.join(' · ')}.`);
    const important = Boolean(row.pinned) || row.type === 'identity' || row.type === 'instruction';
    const when = Number(row.updated_at) || Number(row.recorded_at) || now;
    notes.push({ rel, text: lines.join('\n'), mtimeMs: important ? now : Math.min(when, now) });
    counts.memories += 1;
  }

  // 2. Khule waade — har ek apna note, waqt ke saath.
  for (const row of legacy.intentions || []) {
    const what = clean(row.what_en) || clean(row.what);
    if (!what) continue;
    const due = Number(row.due_ts) > 0 ? dateTimeOf(Number(row.due_ts)) : '';
    const base = `follow-up-${slugFrom(clean(row.what_en)) || slugFrom(clean(row.what)) || shortHash(row.uid || what)}`;
    const rel = uniqueRel(base.slice(0, 60).replace(/-+$/, ''), taken);
    const lines = [
      header(rel.replace(/\.md$/, ''), `Follow-up: ${what}${due ? ` — due ${due}` : ''}`, 'project'),
      clean(row.what) || what,
    ];
    if (clean(row.what_en) && clean(row.what) && clean(row.what_en) !== clean(row.what)) lines.push('', `In English: ${clean(row.what_en)}`);
    const by = characterLabel(row.character_id);
    lines.push('', `Status: open. ${due ? `Due: ${due}.` : 'No time was set.'}${by ? ` Promised by ${by}.` : ''} Noted ${dateOf(Number(row.created_at)) || 'earlier'}.`);
    notes.push({ rel, text: lines.join('\n'), mtimeMs: now });
    counts.followUps += 1;
  }

  // 3. Purani baat-cheet ke khulase — hafte ka ek note (bara ho to hisson mein).
  const weeks = new Map();
  for (const row of legacy.episodes || []) {
    const start = Number(row.start_ts);
    if (!Number.isFinite(start) || start <= 0) continue;
    const d = new Date(start);
    const monday = new Date(d.getFullYear(), d.getMonth(), d.getDate() - ((d.getDay() + 6) % 7));
    const key = dateOf(monday.getTime());
    if (!weeks.has(key)) weeks.set(key, { monday, rows: [] });
    weeks.get(key).rows.push(row);
  }
  for (const [key, week] of [...weeks.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
    const sunday = new Date(week.monday.getFullYear(), week.monday.getMonth(), week.monday.getDate() + 6);
    const range = week.monday.getMonth() === sunday.getMonth()
      ? `${week.monday.getDate()}–${sunday.getDate()} ${MONTHS[sunday.getMonth()]} ${sunday.getFullYear()}`
      : `${week.monday.getDate()} ${MONTHS[week.monday.getMonth()]} – ${sunday.getDate()} ${MONTHS[sunday.getMonth()]} ${sunday.getFullYear()}`;
    const parts = [];
    let current = [];
    let size = 0;
    for (const row of week.rows) {
      const who = characterLabel(row.character_id);
      const block = `## ${dateTimeOf(Number(row.start_ts))} — ${clean(row.title) || 'Conversation'}${who ? ` (with ${who})` : ''}\n${clean(row.summary_en) || '(no summary)'}\n`;
      if (size + block.length > PART_CHARS && current.length) { parts.push(current); current = []; size = 0; }
      current.push({ block, row });
      size += block.length;
    }
    if (current.length) parts.push(current);
    parts.forEach((part, i) => {
      const titles = part.map((p) => clean(p.row.title)).filter(Boolean);
      const label = parts.length > 1 ? ` (part ${i + 1} of ${parts.length})` : '';
      const description = `Summaries of past conversations, ${range}${label}: ${titles.join('; ')}`;
      const rel = uniqueRel(`past-conversations-${key}${parts.length > 1 ? `-part-${i + 1}` : ''}`, taken);
      const lastTs = Math.max(...part.map((p) => Number(p.row.start_ts) || 0));
      const text = `${header(rel.replace(/\.md$/, ''), description, 'project')}${part.map((p) => p.block).join('\n')}`;
      notes.push({ rel, text, mtimeMs: Math.min(lastTs || now, now) });
      counts.conversationNotes += 1;
    });
  }

  // 4. Legacy Hermes / purana profile / memories.json — jo Memory Core mein pehle se nahi.
  for (const entry of extras || []) {
    const key = sameKey(entry.text);
    if (!key || seen.has(key)) { counts.duplicatesSkipped += 1; continue; }
    seen.add(key);
    const base = slugFrom(entry.text) || `note-${shortHash(entry.text)}`;
    const rel = uniqueRel(base, taken);
    const text = [
      header(rel.replace(/\.md$/, ''), entry.text),
      entry.text,
      '',
      `From ${entry.source}${entry.noted ? ` (noted ${entry.noted})` : ''}.`,
    ].join('\n');
    notes.push({ rel, text, mtimeMs: now - 1000 });
    counts.extras += 1;
  }

  return { notes, counts };
}

// ── Backup ──────────────────────────────────────────────────────────────────

const stampOf = (ms) => {
  const d = new Date(ms);
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
};

function copyIfExists(from, to) {
  try {
    if (from && fs.existsSync(from) && fs.statSync(from).isFile()) {
      fs.copyFileSync(from, to);
      return true;
    }
  } catch { /* ek file na aaye to baqi phir bhi */ }
  return false;
}

/** Sab purani files ki copy — muntaqli se PEHLE. Lautata hai: folder + kya copy hua. */
function makeBackup({ accountDir, sources, now }) {
  const dir = path.join(accountDir, BACKUP_DIR, stampOf(now));
  fs.mkdirSync(dir, { recursive: true });
  const copied = [];
  if (sources.legacyDb && copyIfExists(sources.legacyDb, path.join(dir, 'memory.db'))) {
    copied.push('memory.db');
    for (const suffix of ['-wal', '-shm']) copyIfExists(`${sources.legacyDb}${suffix}`, path.join(dir, `memory.db${suffix}`));
  }
  if (sources.legacyHermesDir) {
    if (copyIfExists(path.join(sources.legacyHermesDir, 'MEMORY.md'), path.join(dir, 'hermes-MEMORY.md'))) copied.push('hermes-MEMORY.md');
    if (copyIfExists(path.join(sources.legacyHermesDir, 'USER.md'), path.join(dir, 'hermes-USER.md'))) copied.push('hermes-USER.md');
  }
  if (copyIfExists(sources.bootstrapMemory, path.join(dir, 'stonic-data-MEMORY.md'))) copied.push('stonic-data-MEMORY.md');
  if (copyIfExists(sources.memoriesJson, path.join(dir, 'memories.json'))) copied.push('memories.json');
  fs.writeFileSync(path.join(dir, 'README.txt'), [
    `Copy of Stonic's earlier memory, made on ${dateTimeOf(now)} when memory moved to this folder.`,
    'Nothing here is used anymore; it is kept only as a safety copy.',
    '',
    'memory.db holds every earlier conversation word for word (table "turns").',
    `Copied: ${copied.length ? copied.join(', ') : 'nothing — there was no earlier memory'}.`,
    '',
  ].join('\r\n'), 'utf8');
  return { dir, copied };
}

/** Database ko us ki COPY ki bhi temp copy se kholna — backup bilkul ankahi rehti hai. */
function readLegacyFromBackup(openSqlite, backupDir) {
  const source = path.join(backupDir, 'memory.db');
  if (!fs.existsSync(source)) return { memories: [], intentions: [], episodes: [] };
  if (typeof openSqlite !== 'function') throw new Error('sqlite_unavailable');
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'stonic-memory-move-'));
  try {
    const db = path.join(work, 'memory.db');
    fs.copyFileSync(source, db);
    for (const suffix of ['-wal', '-shm']) copyIfExists(`${source}${suffix}`, `${db}${suffix}`);
    return readLegacyDb(openSqlite, db);
  } finally {
    try { fs.rmSync(work, { recursive: true, force: true }); } catch { /* temp hai, Windows khud saaf karega */ }
  }
}

// ── Asal kaam ───────────────────────────────────────────────────────────────

function readMarker(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}

function writeMarker(file, data) {
  try { fs.writeFileSync(file, `${JSON.stringify(data, null, 2)}\n`, 'utf8'); } catch { /* agli dafa dobara */ }
}

/**
 * @param {{
 *   store: import('./store').FileMemory,
 *   accountDir: string,
 *   sources: { legacyDb?: string|null, legacyHermesDir?: string|null, bootstrapMemory?: string|null, memoriesJson?: string|null },
 *   legacyHermesDir: purane versions ki Hermes `memories` folder (MEMORY.md / USER.md) — sirf parhna.
 *   openSqlite?: (file: string) => any,
 *   now?: number,
 *   log?: (level: string, msg: string) => void,
 * }} opts
 */
async function runMigration(opts) {
  const { store, accountDir, sources = {}, openSqlite } = opts;
  const now = opts.now || Date.now();
  const log = opts.log || (() => {});
  const markerFile = path.join(accountDir, MARKER_FILE);
  const previous = readMarker(markerFile);
  if (previous && previous.done) return { ok: true, skipped: true, marker: previous };
  const attempts = (previous && Number(previous.attempts) ? Number(previous.attempts) : 0) + 1;

  // 1. Backup — sirf pehli koshish par (dobara koshish purani copy hi istemal karti hai).
  let backupDir = previous && previous.backup && fs.existsSync(previous.backup) ? previous.backup : '';
  let copied = previous && Array.isArray(previous.copied) ? previous.copied : [];
  if (!backupDir) {
    const made = makeBackup({ accountDir, sources, now });
    backupDir = made.dir;
    copied = made.copied;
  }

  // 2. Purani database. Na khule to agli dafa dobara (3 dafa tak) — tab tak
  //    baqi zariye bhi nahi likhe jate, taake sab ek saath aur ek hi dafa aayein.
  let legacy = { memories: [], intentions: [], episodes: [] };
  let legacyError = '';
  try {
    legacy = readLegacyFromBackup(openSqlite, backupDir);
  } catch (err) {
    legacyError = String(err && err.message ? err.message : err).slice(0, 200);
    // Driver hi load na hua (file kharab nahi, PC ka masla): kabhi haar nahi
    // maanni — warna is user ki purani yaadein hamesha ke liye peeche reh jatin.
    const driverProblem = /sqlite_unavailable|cannot find module|compiled against|NODE_MODULE_VERSION|dlopen|\.node\b/i.test(legacyError);
    if (driverProblem) {
      writeMarker(markerFile, { version: MIGRATION_VERSION, done: false, attempts, backup: backupDir, copied, error: legacyError });
      log('warn', `[FileMemory] purani database ka driver nahi chala (${legacyError}) — agli dafa dobara`);
      return { ok: false, retry: true, error: legacyError };
    }
    if (attempts < MAX_ATTEMPTS) {
      writeMarker(markerFile, { version: MIGRATION_VERSION, done: false, attempts, backup: backupDir, copied, error: legacyError });
      log('warn', `[FileMemory] purani database nahi khuli (${legacyError}) — agli dafa dobara (${attempts}/${MAX_ATTEMPTS})`);
      return { ok: false, retry: true, error: legacyError };
    }
    log('error', `[FileMemory] purani database ${MAX_ATTEMPTS} dafa nahi khuli — baqi zariye muntaqil, database backup mein mehfooz`);
  }

  // 3. Mansooba. Folder mein pehle se mojood naam (jo muntaqli ke nahi) chhor kar.
  const taken = new Set();
  for (const note of store.scan()) {
    let origin = '';
    try {
      const parsed = parseNote(fs.readFileSync(path.join(accountDir, ...note.rel.split('/')), 'utf8'));
      origin = parsed.metadata.origin || parsed.fields.origin || '';
    } catch { /* parh na saka = mehfooz taraf: naam lia hua */ }
    if (origin !== ORIGIN) taken.add(note.rel.toLowerCase());
  }
  const plan = planMigration({ legacy, extras: readExtras(sources), taken, now });

  // 4. Likhna — ek saath, fehrist ek dafa.
  const result = plan.notes.length ? await store.bulkWrite(plan.notes) : { written: 0, failed: [] };
  const marker = {
    version: MIGRATION_VERSION,
    done: true,
    at: new Date(now).toISOString(),
    attempts,
    backup: backupDir,
    copied,
    counts: { ...plan.counts, notesWritten: result.written, notesFailed: result.failed.length },
  };
  if (legacyError) marker.legacyError = legacyError;
  if (result.failed.length) marker.failed = result.failed.slice(0, 20);
  writeMarker(markerFile, marker);
  log('info', `[FileMemory] muntaqli mukammal: ${result.written} notes (${plan.counts.memories} yaadein, ${plan.counts.followUps} waade, ${plan.counts.conversationNotes} khulase, ${plan.counts.extras} doosri files)${result.failed.length ? `, ${result.failed.length} nakaam` : ''}`);
  return { ok: true, marker };
}

module.exports = {
  runMigration,
  planMigration,
  readLegacyDb,
  readExtras,
  parseEntries,
  slugFrom,
  makeBackup,
  MARKER_FILE,
  BACKUP_DIR,
  ORIGIN,
  MIGRATION_VERSION,
};
