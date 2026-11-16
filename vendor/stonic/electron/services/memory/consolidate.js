// "Neend": jab PC farigh ho to yaadash ki dekh-bhaal.
//
// ════════════════════════════════════════════════════════════════════════════
//  IS VERSION MEIN NEEND SIRF CODE HAI — KOI AI NAHI
// ════════════════════════════════════════════════════════════════════════════
//
//  Research mein sab se khatarnak cheez yehi nikli: LLM se poori yaadash "saaf"
//  karwana. Ek test mein model ne ek hi qadam mein 18,000 tokens ki yaadash ko
//  122 tokens bana diya. Is liye yahan koi model kuch dobara nahi likhta.
//
//  Ye kaam sab QAIDON par hain, aur har ek be-zarar hai (kuch mit-ta nahi):
//
//    - waade jin ki muddat guzar gayi -> `expired`
//    - andaze jinhein kaafi alag sessions ka saboot mil chuka -> pakke
//    - backup (aur kabhi kabhi backup khol kar dekhna)
//    - index ki safai, purane mukammal kaamon ki chhatai
//
//  Pakka/kacha ka faisla `store.reinforce` likhte waqt hi kar deta hai; yahan
//  sirf wo andaze uthaye jate hain jo kisi wajah se reh gaye.

const { DIGEST, DAY } = require('./config');
const { backupDue, listBackups, verifyBackup, quickCheck } = require('./db');

/**
 * @param {import('./store').MemoryStore} store
 * @param {import('./jobs').JobQueue} jobs
 * @param {string} baseDir  yaadash ka folder — backup isi ke andar bante hain
 * @returns {{ expired: number, promoted: number, backupWanted: boolean, backupCheck: object|null,
 *   prunedJobs: number, healthy: boolean }}
 */
function runMaintenance(store, jobs, baseDir, now = store.now()) {
  const out = { expired: 0, promoted: 0, backupWanted: false, backupCheck: null, prunedJobs: 0, healthy: true };

  out.expired = store.expireIntentions(now);

  const ready = store.q(`select id from memories where deleted = 0 and status = 'candidate' and session_count >= ?`)
    .all(DIGEST.promoteAfterSessions);
  for (const row of ready) {
    store.q(`update memories set status = 'active', confidence = max(confidence, 0.9), updated_at = ? where id = ?`).run(now, row.id);
    store.audit('system', 'promote', 'memory', row.id, { status: 'candidate' }, { status: 'active' }, 'maintenance');
    out.promoted += 1;
  }

  out.healthy = quickCheck(store.db);

  // Kharab file ka backup nahi banta — warna rotation achhe backup ko kharab se
  // badal deti. Kharabi par purane backup jaise hain waise rehte hain. Backup
  // khud yahan nahi banta (wo async hai) — service `backupWanted` dekh kar banati hai.
  out.backupWanted = !!(out.healthy && baseDir && backupDue(baseDir, now));

  // Mahine mein ek dafa sab se naye backup ko waqai khol kar dekhna: "backup
  // hai" aur "backup khulta hai" do alag baatein hain.
  const lastCheck = Number(store.getMeta('last_backup_check_at', '0')) || 0;
  if (baseDir && now - lastCheck >= 30 * DAY) {
    const latest = listBackups(baseDir)[0];
    if (latest) {
      out.backupCheck = verifyBackup(latest.file);
      store.setMeta('last_backup_check_at', now);
      store.setMeta('last_backup_check_ok', out.backupCheck.ok ? '1' : '0');
    }
  }

  out.prunedJobs = jobs ? jobs.prune(now - 45 * DAY) : 0;

  try {
    for (const t of ['turns_uni', 'turns_tri', 'turns_skel', 'memories_uni', 'memories_tri', 'memories_skel', 'episodes_uni', 'episodes_skel']) {
      store.db.exec(`insert into ${t}(${t}) values ('optimize')`);
    }
    store.db.pragma('wal_checkpoint(PASSIVE)');
  } catch { /* safai na ho saki to agli dafa */ }

  store.setMeta('last_maintenance_at', now);
  return out;
}

module.exports = { runMaintenance };
