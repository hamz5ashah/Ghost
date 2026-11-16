// Background kaam ki qataar — usi database ki `jobs` table mein.
//
// ════════════════════════════════════════════════════════════════════════════
//  QATAAR DATABASE MEIN KYUN, MEMORY (RAM) MEIN KYUN NAHI
// ════════════════════════════════════════════════════════════════════════════
//
//  Nichor baat khatam hone ke 15 minute baad chalta hai. Us dauran user app
//  band kar de, PC so jaye ya bijli chali jaye — RAM ki qataar ke saath wo kaam
//  hamesha ke liye gaya. Yahan kaam file mein likha hai: app dobara khule to
//  wahin se uthta hai.
//
// ── TEEN HIFAZATEIN ─────────────────────────────────────────────────────────
//
//   idempotency_key -> wohi kaam do dafa qataar mein nahi lagta (aur ho chuka
//                      ho to dobara nahi chalta). Key mein prompt ka version
//                      shamil hota hai, is liye naya prompt aane par wohi
//                      baat-cheet dobara nichori ja sakti hai.
//   lease           -> kaam uthane wala ek muddat ke liye "malik" hai. Wo crash
//                      ho jaye to muddat guzarne par kaam dobara mil jata hai.
//   attempts        -> baar baar fail hone wala kaam `dead` ho jata hai, taake
//                      har launch par wohi kaam paisa na jalaye.

const { JOBS, DIGEST } = require('./config');

class JobQueue {
  /**
   * @param {import('./store').MemoryStore} store
   */
  constructor(store) {
    this.store = store;
    this.db = store.db;
  }

  /**
   * @returns {{ id: number, created: boolean, state: string }}
   */
  enqueue(kind, payload, idempotencyKey, { runAfter = 0 } = {}) {
    const now = this.store.now();
    const existing = this.store.q('select id, state from jobs where idempotency_key = ?').get(idempotencyKey);
    if (existing) {
      // Qataar mein pehle se hai aur naya waqt DER ka hai (baat-cheet dobara chal
      // pari) to intezar barha do — adhoori baat ka nichor na ho.
      if (existing.state === 'queued' && runAfter) {
        this.store.q('update jobs set run_after = max(run_after, ?) where id = ?').run(runAfter, existing.id);
      }
      return { id: Number(existing.id), created: false, state: existing.state };
    }
    const info = this.store.q(`insert into jobs(kind, payload, idempotency_key, state, run_after, created_at)
      values (?, ?, ?, 'queued', ?, ?)`).run(kind, JSON.stringify(payload || {}), idempotencyKey, runAfter, now);
    return { id: Number(info.lastInsertRowid), created: true, state: 'queued' };
  }

  /**
   * Agla tayyar kaam uthata hai (ya jis ka malik crash ho gaya).
   * @returns {{ id: number, kind: string, payload: object, attempts: number }|null}
   */
  claim(kinds = null) {
    const now = this.store.now();
    const filter = kinds && kinds.length ? `and kind in (${kinds.map(() => '?').join(',')})` : '';
    const args = kinds && kinds.length ? kinds : [];
    const pick = this.db.transaction(() => {
      const row = this.db.prepare(`select * from jobs
        where ((state = 'queued' and run_after <= ?) or (state = 'running' and leased_until <= ?)) ${filter}
        order by run_after, id limit 1`).get(now, now, ...args);
      if (!row) return null;
      this.store.q(`update jobs set state = 'running', leased_until = ?, attempts = attempts + 1 where id = ?`)
        .run(now + JOBS.leaseMs, row.id);
      return row;
    });
    const row = pick();
    if (!row) return null;
    let payload = {};
    try { payload = JSON.parse(row.payload) || {}; } catch { /* kharab payload = khali */ }
    return { id: Number(row.id), kind: row.kind, payload, attempts: Number(row.attempts) + 1 };
  }

  complete(id, { costUsd = 0 } = {}) {
    this.store.q(`update jobs set state = 'done', finished_at = ?, cost_usd = cost_usd + ?, error = null, leased_until = 0 where id = ?`)
      .run(this.store.now(), Number(costUsd) || 0, id);
  }

  /**
   * Kaam fail hua. Koshishein baqi hon to der se dobara (har dafa dugni der),
   * warna `dead`. `costUsd`: fail hone wali call ka paisa bhi ginti mein aata hai.
   * @returns {'queued'|'dead'}
   */
  fail(id, error, { maxAttempts = DIGEST.maxAttempts, costUsd = 0, retryInMs = null } = {}) {
    const row = this.store.q('select attempts from jobs where id = ?').get(id);
    if (!row) return 'dead';
    const attempts = Number(row.attempts);
    const dead = attempts >= maxAttempts;
    const wait = retryInMs != null ? retryInMs : JOBS.retryBaseMs * (2 ** Math.max(0, attempts - 1));
    this.store.q(`update jobs set state = ?, run_after = ?, leased_until = 0, error = ?, cost_usd = cost_usd + ?, finished_at = ? where id = ?`)
      .run(dead ? 'dead' : 'queued', this.store.now() + wait, String(error || '').slice(0, 500), Number(costUsd) || 0,
        dead ? this.store.now() : null, id);
    return dead ? 'dead' : 'queued';
  }

  /** Kaam ko bina koshish gine wapas qataar mein (maslan cap lag gaya, ya voice chal rahi hai). */
  defer(id, waitMs) {
    this.store.q(`update jobs set state = 'queued', run_after = ?, leased_until = 0, attempts = max(0, attempts - 1) where id = ?`)
      .run(this.store.now() + waitMs, id);
  }

  /** `since` ke baad kitna kharcha hua (USD) — cap ke liye. */
  spentSince(since) {
    return Number(this.store.q('select coalesce(sum(cost_usd), 0) as s from jobs where created_at >= ?').get(since).s) || 0;
  }

  /** `since` ke baad kitni LLM calls wale kaam chale (done + dead + chalte hue). */
  ranSince(since, kind) {
    return Number(this.store.q(`select count(*) as n from jobs where kind = ? and attempts > 0 and created_at >= ?`).get(kind, since).n) || 0;
  }

  counts() {
    const out = { queued: 0, running: 0, done: 0, dead: 0 };
    for (const r of this.store.q('select state, count(*) as n from jobs group by state').all()) out[r.state] = Number(r.n);
    return out;
  }

  /** Purane mukammal kaam hatana — warna table saalon mein be-wajah barhti hai. */
  prune(olderThan) {
    return this.store.q(`delete from jobs where state in ('done', 'dead') and finished_at is not null and finished_at < ?`).run(olderThan).changes;
  }
}

module.exports = { JobQueue };
