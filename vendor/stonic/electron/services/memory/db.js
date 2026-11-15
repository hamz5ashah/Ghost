// Memory Core ki file: kholna, mehfooz rakhna, backup.
//
// ════════════════════════════════════════════════════════════════════════════
//  YE FILE ELECTRON KE BAGHAIR HAI — JAAN BUJH KAR
// ════════════════════════════════════════════════════════════════════════════
//
//  Yahan `electron` ka koi require nahi. Folder ka raasta bahar se aata hai
//  (service.js `app.getPath('userData')` deta hai), is liye poora store test
//  mein ek temp folder par chalta hai.
//
// ── FILE KAHAN HAI, AUR UPDATE PAR KYA HOTA HAI ─────────────────────────────
//
//  <userData>/memory/<account>/memory.db   (+ memory.db-wal, memory.db-shm)
//  <userData>/memory/<account>/backups/    (rozana / hafta-war / mahana copies)
//
//  userData install folder se BAHAR hai, is liye app ka naya version aane par
//  ye file wahin rehti hai. Naya version khulte hi `migrate()` dhaanche ko
//  apne version tak le aata hai — purana data waise ka waisa.
//
//  Har Stonic account ka apna folder hai: ek hi PC par doosra shakhs sign in
//  kare to usay pehle wale ki yaadein nahi milti. Wohi account dobara sign in
//  kare (ya app dobara install ho) to usi folder par wapas aata hai.
//
// ── YE FILE ENCRYPTED NAHI HAI (4 Oct 2026 ka faisla) ───────────────────────
//
//  Encryption ki chaabi Windows se bandhi hoti; Windows dobara install hoti to
//  saari yaadash hamesha ke liye band. "10 saal baad bhi na bhoole" us se
//  zyada ahem hai. Passwords/OTP/card number `secrets.js` likhne se pehle hi
//  nikal deta hai. Baad mein encryption lagani ho to sirf `loadDriver()` aur
//  `openMemoryDb()` badalte hain — baqi code ko database ka driver nazar hi
//  nahi aata.
//
// ── DRIVER: better-sqlite3 12.x (13 nahi) ───────────────────────────────────
//
//  13.x Electron 34 (Node 20) mein load hote hi crash karta hai (0xC0000005,
//  4 Oct 2026 ko napa). 12.11.1 ka Electron-v132 binary theek chalta hai.
//  Electron upgrade ke baghair version mat barhao.

const fs = require('fs');
const path = require('path');
const { FILES, BACKUP, DAY } = require('./config');
const { migrate, MIGRATIONS } = require('./schema');

let cachedDriver = null;
let driverError = null;

/**
 * Native driver ko SIRF zaroorat par load karta hai. Nakaami par `null` —
 * ghalat ABI ya gumshuda binary se app nahi girni chahiye; yaadash band
 * rehti hai aur app pehle jaisi chalti hai.
 */
function loadDriver() {
  if (cachedDriver || driverError) return cachedDriver;
  try {
    // eslint-disable-next-line global-require
    cachedDriver = require('better-sqlite3');
  } catch (err) {
    driverError = err;
    cachedDriver = null;
  }
  return cachedDriver;
}

const getDriverError = () => driverError;

// baseDir khud yaadash ka folder hai (service <userData>/memory/<account> deta hai).
const memoryDir = (baseDir) => baseDir;
const dbPath = (baseDir) => path.join(memoryDir(baseDir), FILES.dbName);
const backupDir = (baseDir) => path.join(memoryDir(baseDir), FILES.backupDirName);

const pad = (n) => String(n).padStart(2, '0');
const stamp = (t) => {
  const d = new Date(t);
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
};

/**
 * Database kholta hai aur dhaancha maujooda version tak laata hai.
 *
 * @param {string} baseDir  userData (ya test ka temp folder)
 * @param {{ now?: number }} [opts]
 * @returns {{ db: object, file: string, migrated: {from:number,to:number,applied:number}, preMigrationBackup: string|null }}
 * @throws driver na mile ya file khul na sake to — bulane wala (service.js) pakarta hai.
 */
function openMemoryDb(baseDir, opts = {}) {
  const Database = loadDriver();
  if (!Database) {
    throw new Error(`Memory database driver load nahi hua: ${driverError ? driverError.message : 'unknown'}`);
  }
  const now = opts.now || Date.now();
  fs.mkdirSync(memoryDir(baseDir), { recursive: true });
  fs.mkdirSync(backupDir(baseDir), { recursive: true });
  const file = dbPath(baseDir);
  const existed = fs.existsSync(file);

  const db = new Database(file);
  // WAL: parhne wale likhne wale ko nahi rokte, aur crash par file salamat.
  db.pragma('journal_mode = WAL');
  // NORMAL: WAL ke saath crash-safe (bijli jaye to aakhri chand likhaiyan ja
  // sakti hain, file kharab nahi hoti) aur FULL se kai guna tez.
  db.pragma('synchronous = NORMAL');
  db.pragma('foreign_keys = ON');
  db.pragma('busy_timeout = 5000');

  // Dhaancha badalne se PEHLE maujooda file ki copy. Migration transaction mein
  // hai, magar ye copy us surat ke liye hai jab naya version khud ghalat ho.
  let preMigrationBackup = null;
  const current = Number(db.pragma('user_version', { simple: true })) || 0;
  if (existed && current > 0 && current < MIGRATIONS.length) {
    preMigrationBackup = path.join(backupDir(baseDir), `pre-migrate-v${current}-${stamp(now)}.db`);
    db.exec(`vacuum into '${preMigrationBackup.replace(/'/g, "''")}'`);
  }

  const migrated = migrate(db);
  return { db, file, migrated, preMigrationBackup };
}

/** Tez sehat ki jaanch. `true` = theek. */
function quickCheck(db) {
  try {
    return db.pragma('quick_check', { simple: true }) === 'ok';
  } catch {
    return false;
  }
}

function closeMemoryDb(db) {
  if (!db || !db.open) return;
  try {
    // WAL ko asal file mein samet do, taake band file akeli mukammal ho.
    db.pragma('wal_checkpoint(TRUNCATE)');
  } catch { /* band hote waqt ki ghalti app ko nahi rokti */ }
  try { db.close(); } catch { /* pehle hi band */ }
}

// ── Backup ──────────────────────────────────────────────────────────────────
//
// `.db` file ko us ki `-wal` ke baghair copy karna data kho sakta hai, is liye
// yahan file copy nahi hoti — `VACUUM INTO` ek mukammal, akeli file banata
// hai (WAL samet), chalte database se, bina usay roke.

const BACKUP_NAME = /^memory-(\d{8})-(\d{6})\.db$/;

function listBackups(baseDir) {
  let names = [];
  try { names = fs.readdirSync(backupDir(baseDir)); } catch { return []; }
  return names
    .map((name) => {
      const m = BACKUP_NAME.exec(name);
      if (!m) return null;
      const [, d, t] = m;
      const at = new Date(
        Number(d.slice(0, 4)), Number(d.slice(4, 6)) - 1, Number(d.slice(6, 8)),
        Number(t.slice(0, 2)), Number(t.slice(2, 4)), Number(t.slice(4, 6)),
      ).getTime();
      const file = path.join(backupDir(baseDir), name);
      let size = 0;
      try { size = fs.statSync(file).size; } catch { /* abhi abhi hati */ }
      return { name, file, at, size };
    })
    .filter(Boolean)
    .sort((a, b) => b.at - a.at);
}

/**
 * Ek backup banata hai aur purane chhaant-ta hai.
 *
 * `db.backup()` SQLite ka apna online-backup hai: file ko tukron mein copy
 * karta hai aur beech beech mein event loop ko saans lene deta hai. Ye is liye
 * zaroori hai ke yahi main process renderer ka bundle bhi chalata hai — bari
 * file par `VACUUM INTO` sab ko chand second rok deta.
 *
 * `mirrorDir` ho to sab se naye backup ki ek copy wahan bhi jati hai (doosri
 * jagah: userData ka folder hi chala jaye to bhi yaadash bache).
 *
 * @returns {Promise<{ file: string, size: number, pruned: string[], mirrored: string|null }>}
 */
async function backupNow(db, baseDir, now = Date.now(), mirrorDir = null) {
  fs.mkdirSync(backupDir(baseDir), { recursive: true });
  const file = path.join(backupDir(baseDir), `memory-${stamp(now)}.db`);
  if (fs.existsSync(file)) fs.unlinkSync(file);
  await db.backup(file);
  const size = fs.statSync(file).size;
  const pruned = pruneBackups(baseDir);
  const mirrored = mirrorDir ? mirrorBackup(file, mirrorDir) : null;
  return { file, size, pruned, mirrored };
}

/**
 * Backup ki copy doosri jagah. Wahan sirf aakhri `keep` copies rehti hain.
 * Nakaami (drive nikal li, folder band) backup ko fail nahi karti — `null`.
 */
function mirrorBackup(file, mirrorDir, keep = 2) {
  try {
    fs.mkdirSync(mirrorDir, { recursive: true });
    const dest = path.join(mirrorDir, path.basename(file));
    fs.copyFileSync(file, dest);
    const old = fs.readdirSync(mirrorDir).filter((n) => BACKUP_NAME.test(n)).sort().reverse().slice(keep);
    for (const name of old) {
      try { fs.unlinkSync(path.join(mirrorDir, name)); } catch { /* kisi ne khol rakhi hai */ }
    }
    return dest;
  } catch {
    return null;
  }
}

/**
 * Kaun se backup rakhne hain: aakhri N din ka ek ek, phir har hafte ka ek,
 * phir har mahine ka ek. Baqi hat jate hain.
 *
 * Sab se naya backup HAMESHA rehta hai, chahe ginti kuch bhi kahe.
 */
function pruneBackups(baseDir) {
  const all = listBackups(baseDir);
  if (all.length <= 1) return [];
  const keep = new Set([all[0].name]);

  const pick = (keyOf, count) => {
    const seen = new Set();
    for (const b of all) {
      const key = keyOf(b.at);
      if (seen.has(key)) continue;
      seen.add(key);
      keep.add(b.name);
      if (seen.size >= count) break;
    }
  };
  const dayKey = (t) => { const d = new Date(t); return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; };
  const weekKey = (t) => Math.floor(t / (7 * DAY));
  const monthKey = (t) => { const d = new Date(t); return `${d.getFullYear()}-${d.getMonth()}`; };
  pick(dayKey, BACKUP.keepDaily);
  pick(weekKey, BACKUP.keepWeekly);
  pick(monthKey, BACKUP.keepMonthly);

  const pruned = [];
  for (const b of all) {
    if (keep.has(b.name)) continue;
    // Bohat purani pre-migrate copies yahan nahi aatin (naam alag hai) — wo
    // jaan bujh kar rehti hain: dhaanche ki tabdeeli se pehle ki aakhri haalat.
    try { fs.unlinkSync(b.file); pruned.push(b.name); } catch { /* kisi ne khol rakhi hai */ }
  }
  return pruned;
}

/**
 * Backup ko waqai khol kar dekhta hai — "backup hai" aur "backup khulta hai"
 * do alag baatein hain. Read-only kholta hai, asal file ko nahi chherta.
 *
 * @returns {{ ok: boolean, turns?: number, memories?: number, version?: number, error?: string }}
 */
function verifyBackup(file) {
  const Database = loadDriver();
  if (!Database) return { ok: false, error: 'driver' };
  let copy = null;
  try {
    copy = new Database(file, { readonly: true, fileMustExist: true });
    const healthy = copy.pragma('quick_check', { simple: true }) === 'ok';
    const version = Number(copy.pragma('user_version', { simple: true })) || 0;
    const turns = copy.prepare('select count(*) as n from turns').get().n;
    const memories = copy.prepare('select count(*) as n from memories').get().n;
    return { ok: healthy, turns, memories, version };
  } catch (err) {
    return { ok: false, error: err.message };
  } finally {
    try { if (copy) copy.close(); } catch { /* pehle hi band */ }
  }
}

/** Kya ab backup ka waqt hai? (aakhri backup `minIntervalMs` se purana) */
function backupDue(baseDir, now = Date.now()) {
  const latest = listBackups(baseDir)[0];
  return !latest || now - latest.at >= BACKUP.minIntervalMs;
}

module.exports = {
  loadDriver,
  getDriverError,
  memoryDir,
  dbPath,
  backupDir,
  openMemoryDb,
  closeMemoryDb,
  quickCheck,
  backupNow,
  mirrorBackup,
  pruneBackups,
  listBackups,
  verifyBackup,
  backupDue,
};
