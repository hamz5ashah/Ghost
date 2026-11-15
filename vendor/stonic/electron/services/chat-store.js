'use strict';

// ═══════════════════════════════════════════════════════════════
//  Chat history file (userData/chats.json)
//
//  The renderer saves the whole chat list whenever it changes. That list grows
//  without bound (MBs after a few weeks), so the old handler — a synchronous,
//  pretty-printed `writeFileSync` straight over the file — froze the main
//  process on every save and, if the app died mid-write, left a truncated file
//  that the next launch read as "no chats" and then overwrote for good.
//
//  This store:
//    - writes asynchronously and atomically (temp file + rename), so a crash
//      leaves either the old file or the new one, never half of one;
//    - coalesces: while a write is running only the NEWEST snapshot waits, so
//      bursts never queue up multi-MB writes;
//    - accepts the snapshot already serialized by the renderer (a string is
//      far cheaper to send over IPC than the object graph) or as an array;
//    - can flush synchronously at quit;
//    - never throws away an unreadable file: it is moved aside first.
// ═══════════════════════════════════════════════════════════════

const fs = require('fs');

const RENAME_RETRY_CODES = new Set(['EPERM', 'EACCES', 'EBUSY']);
const RENAME_RETRIES = 4;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** True when `text` looks like a serialized JSON array (cheap check, no parse). */
function looksLikeJsonArray(text) {
  if (typeof text !== 'string') return false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text.charCodeAt(i);
    // skip whitespace / BOM
    if (ch === 0x20 || ch === 0x0a || ch === 0x0d || ch === 0x09 || ch === 0xfeff) continue;
    return ch === 0x5b; // '['
  }
  return false;
}

/**
 * Parse chat file text into an array. A file holding the list as one JSON
 * string (double-encoded) is unwrapped. Returns null when it is not a list.
 */
function parseChatsText(text) {
  let value = JSON.parse(text);
  if (typeof value === 'string') value = JSON.parse(value);
  return Array.isArray(value) ? value : null;
}

function createChatStore({ getFilePath, logger = console } = {}) {
  if (typeof getFilePath !== 'function') throw new Error('createChatStore: getFilePath is required');

  let pending = null;   // newest serialized snapshot not yet on disk
  let pendingGen = 0;   // its sequence number
  let nextGen = 0;
  // flushSync wrote everything up to this sequence number; an async write of
  // an older snapshot that finishes later must not replace it.
  let syncFlushedGen = 0;
  let writing = null;   // promise of the drain loop, while it runs

  async function renameWithRetry(from, to) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        await fs.promises.rename(from, to);
        return;
      } catch (err) {
        // Windows: antivirus / the search indexer can hold the target for a moment.
        if (attempt >= RENAME_RETRIES || !RENAME_RETRY_CODES.has(err && err.code)) throw err;
        await sleep(40 * (attempt + 1));
      }
    }
  }

  async function writeAtomic(file, text, gen) {
    const tmp = `${file}.tmp`;
    await fs.promises.writeFile(tmp, text, 'utf8');
    if (gen <= syncFlushedGen) {
      // A newer snapshot was written synchronously meanwhile (quit path).
      try { await fs.promises.rm(tmp, { force: true }); } catch { /* best effort */ }
      return;
    }
    try {
      await renameWithRetry(tmp, file);
    } catch (err) {
      // Rename kept failing: a direct write still beats losing the save.
      logger.warn(`[Chats] Atomic replace failed (${err.code || err.message}); writing in place.`);
      await fs.promises.writeFile(file, text, 'utf8');
      try { await fs.promises.rm(tmp, { force: true }); } catch { /* best effort */ }
    }
  }

  function drain() {
    if (writing) return writing;
    writing = (async () => {
      try {
        while (pending !== null) {
          const text = pending;
          const gen = pendingGen;
          pending = null;
          try {
            await writeAtomic(getFilePath(), text, gen);
          } catch (err) {
            logger.warn('[Chats] Could not save chats:', err && err.message);
          }
        }
      } finally {
        writing = null;
      }
    })();
    return writing;
  }

  /**
   * Queue a snapshot for saving. `chats` is the array or its JSON text.
   * Resolves true once the newest snapshot is on disk (false if rejected).
   */
  function save(chats) {
    let text;
    if (typeof chats === 'string') {
      if (!looksLikeJsonArray(chats)) {
        logger.warn('[Chats] Refusing to save: payload is not a JSON list.');
        return Promise.resolve(false);
      }
      text = chats;
    } else if (Array.isArray(chats)) {
      try {
        text = JSON.stringify(chats);
      } catch (err) {
        logger.warn('[Chats] Could not serialize chats:', err && err.message);
        return Promise.resolve(false);
      }
    } else {
      logger.warn('[Chats] Refusing to save: payload is not a list.');
      return Promise.resolve(false);
    }
    nextGen += 1;
    pending = text;
    pendingGen = nextGen;
    return drain().then(() => true, () => false);
  }

  /** Write whatever is still waiting, synchronously. For quit / session end. */
  function flushSync() {
    if (pending === null) return false;
    const text = pending;
    syncFlushedGen = pendingGen;
    pending = null;
    const file = getFilePath();
    const tmp = `${file}.quit.tmp`;
    try {
      fs.writeFileSync(tmp, text, 'utf8');
      try {
        fs.renameSync(tmp, file);
      } catch {
        fs.writeFileSync(file, text, 'utf8');
        try { fs.rmSync(tmp, { force: true }); } catch { /* best effort */ }
      }
      return true;
    } catch (err) {
      logger.warn('[Chats] Final save failed:', err && err.message);
      return false;
    }
  }

  /**
   * Read the saved chats. Returns an array, or null when there is no file.
   * An unreadable file is moved aside (never deleted) and an interrupted
   * write's temp file is tried before giving up.
   */
  async function load() {
    const file = getFilePath();
    let text;
    try {
      text = await fs.promises.readFile(file, 'utf8');
    } catch (err) {
      if (err && err.code === 'ENOENT') return null;
      throw err;
    }
    try {
      const chats = parseChatsText(text);
      if (chats) return chats;
      throw new Error('chat file does not contain a list');
    } catch (err) {
      const aside = `${file}.corrupt-${Date.now()}`;
      logger.warn(`[Chats] chats.json unreadable (${err.message}); keeping it as ${aside}.`);
      try { await fs.promises.rename(file, aside); } catch { /* leave it where it is */ }
      for (const candidate of [`${file}.tmp`, `${file}.quit.tmp`]) {
        try {
          const recovered = parseChatsText(await fs.promises.readFile(candidate, 'utf8'));
          if (recovered) {
            logger.warn(`[Chats] Recovered ${recovered.length} chats from ${candidate}.`);
            return recovered;
          }
        } catch { /* not there or not valid */ }
      }
      return [];
    }
  }

  return {
    save,
    load,
    flushSync,
    /** Resolves when no write is running (tests / shutdown diagnostics). */
    idle: () => (writing ? writing.then(() => undefined) : Promise.resolve()),
  };
}

module.exports = { createChatStore, looksLikeJsonArray, parseChatsText };
