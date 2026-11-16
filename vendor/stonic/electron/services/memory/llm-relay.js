// Nichor ka LLM call — main process se renderer tak, aur wapas.
//
// ════════════════════════════════════════════════════════════════════════════
//  CALL RENDERER MEIN KYUN CHALTI HAI
// ════════════════════════════════════════════════════════════════════════════
//
//  Nichor ka faisla (qataar, gate, store) main process mein hai, magar model
//  tak ka raasta — Stonic Cloud ka token, us ka refresh, Custom API ki key, aur
//  user ne kaun sa engine chuna — sab renderer ke paas hai (Awareness ka
//  dimaagh bhi wahin se call karta hai: src/lib/awareness/brain.ts).
//
//  Token ko main process mein dobara banana khatarnak hota: Supabase ka refresh
//  token har dafa badalta hai, aur do jagah se refresh poori sign-in tor deta
//  hai. Is liye main sirf DARKHWAST bhejta hai, call renderer karta hai.
//
//      main: memory:llm-request { id, system, user }  ──▶  renderer
//      main: memory:llm-reply   { id, text | error }  ◀──  renderer
//
//  Window band ho ya jawab na aaye to request waqt par fail hoti hai aur
//  nichor qataar mein intezar karta hai — kuch zaya nahi hota.

const DEFAULT_TIMEOUT_MS = 90 * 1000;

class LlmRelay {
  /**
   * @param {{ send: (payload: object) => boolean, timeoutMs?: number }} opts
   *   send: renderer ko darkhwast bhejta hai; `false` = koi sunne wala nahi.
   */
  constructor(opts) {
    this.send = opts.send;
    this.timeoutMs = opts.timeoutMs || DEFAULT_TIMEOUT_MS;
    this.pending = new Map();
    this.seq = 0;
  }

  /** @returns {Promise<{ text: string, costUsd?: number }>} */
  request(req) {
    return new Promise((resolve, reject) => {
      this.seq += 1;
      const id = `llm-${Date.now()}-${this.seq}`;
      const fail = (code) => {
        const entry = this.pending.get(id);
        if (!entry) return;
        clearTimeout(entry.timer);
        this.pending.delete(id);
        reject(Object.assign(new Error(`memory llm: ${code}`), { code }));
      };
      const timer = setTimeout(() => fail('timeout'), this.timeoutMs);
      if (typeof timer.unref === 'function') timer.unref();
      this.pending.set(id, { resolve, timer, fail });
      let delivered = false;
      try { delivered = this.send({ id, system: req.system, user: req.user, maxOutputTokens: req.maxOutputTokens }); } catch { delivered = false; }
      if (!delivered) fail('no_llm');
    });
  }

  /** Renderer ka jawab. Anjaan ya purani id khamoshi se chhor di jati hai. */
  resolve(reply) {
    const entry = reply && this.pending.get(reply.id);
    if (!entry) return false;
    if (reply.error) { entry.fail(String(reply.error).slice(0, 40)); return true; }
    clearTimeout(entry.timer);
    this.pending.delete(reply.id);
    entry.resolve({ text: String(reply.text || ''), costUsd: Number.isFinite(reply.costUsd) ? reply.costUsd : undefined });
    return true;
  }

  /** Renderer chala gaya (reload / band): jo intezar mein the unhein foran fail. */
  cancelAll(code = 'no_llm') {
    for (const entry of [...this.pending.values()]) entry.fail(code);
  }
}

module.exports = { LlmRelay };
