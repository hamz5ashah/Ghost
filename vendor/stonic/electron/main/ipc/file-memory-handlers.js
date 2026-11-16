const { BrowserWindow, shell } = require('electron');

// Nayi yaadash (ek folder, Claude Code wala tareeqa) ke IPC handlers.
//
// Classic voice, New voice (Beta) aur Memory page — teeno renderer se isi
// darwaze se aate hain. Sab aakhir mein ek hi FileMemoryService tak pohnchte
// hain, is liye likhne wala hamesha ek rehta hai.
//
// Har jawab ek object hai aur koi handler throw nahi karta: yaadash band ho ya
// user sign in na ho to `{ ok:false, enabled:false, reason }`.

function broadcast(channel, payload) {
  for (const win of BrowserWindow.getAllWindows()) {
    if (win.isDestroyed()) continue;
    try { win.webContents.send(channel, payload); } catch { /* window band ho rahi hai */ }
  }
}

const str = (value, max = 4096) => (typeof value === 'string' ? value.slice(0, max) : '');

function registerFileMemoryHandlers({ getFileMemoryService }) {
  const { ipcMain } = require('electron');
  let wired = null;

  const service = () => {
    const svc = typeof getFileMemoryService === 'function' ? getFileMemoryService() : null;
    if (svc && wired !== svc) {
      wired = svc;
      svc.on('changed', (info) => broadcast('file-memory:changed', info || {}));
    }
    return svc;
  };

  const call = (fn) => async (_event, payload) => {
    const svc = service();
    if (!svc) return { ok: false, enabled: false, reason: 'not_initialised' };
    try {
      return await fn(svc, payload && typeof payload === 'object' ? payload : {});
    } catch (err) {
      return { ok: false, reason: 'error', text: String(err && err.message ? err.message : err).slice(0, 200) };
    }
  };

  ipcMain.handle('file-memory:status', call((svc) => svc.status()));

  // Classic ka prompt hissa: official "# Memory" + aaj ki tareekh + MEMORY.md.
  // `indexOnly` = sirf fehrist (Awareness).
  ipcMain.handle('file-memory:card', call((svc, p) => svc.card({ indexOnly: p.indexOnly === true })));

  // New voice (Claude Agent SDK): account ka folder + "# Memory" hissa. SDK
  // isi folder par apni auto memory chalata hai (voice-beta-manager folder
  // ko yahin se dobara milata hai — renderer koi aur raasta nahi de sakta).
  ipcMain.handle('file-memory:agent', call((svc) => svc.agentSetup()));

  // Classic ka `memory` tool — Anthropic memory_20250818 ke hukm.
  ipcMain.handle('file-memory:tool', call((svc, p) => svc.command({
    command: str(p.command, 32),
    path: str(p.path, 512),
    file_text: str(p.file_text, 64 * 1024),
    old_str: str(p.old_str, 64 * 1024),
    new_str: typeof p.new_str === 'string' ? p.new_str.slice(0, 64 * 1024) : undefined,
    insert_line: p.insert_line,
    insert_text: str(p.insert_text, 64 * 1024),
    old_path: str(p.old_path, 512),
    new_path: str(p.new_path, 512),
    view_range: Array.isArray(p.view_range) ? p.view_range.slice(0, 2) : undefined,
  }, { sessionId: str(p.session_id, 80) })));

  // Classic ka Grep — Claude Code ke Grep jaisa, sirf yaadash ke folder mein.
  ipcMain.handle('file-memory:grep', call((svc, p) => svc.grep({
    pattern: str(p.pattern, 1000),
    path: str(p.path, 512) || undefined,
    glob: str(p.glob, 200) || undefined,
    output_mode: str(p.output_mode, 32) || undefined,
    head_limit: Number.isFinite(Number(p.head_limit)) ? Number(p.head_limit) : undefined,
    offset: Number.isFinite(Number(p.offset)) ? Number(p.offset) : undefined,
    context: Number.isFinite(Number(p.context)) ? Number(p.context) : undefined,
    case_insensitive: p.case_insensitive === true,
  })));

  // Memory page.
  ipcMain.handle('file-memory:list', call((svc) => svc.list()));
  ipcMain.handle('file-memory:read', call((svc, p) => svc.read(str(p.path, 512))));
  ipcMain.handle('file-memory:save', call((svc, p) => svc.save(str(p.path, 512), str(p.text, 64 * 1024))));
  ipcMain.handle('file-memory:delete', call((svc, p) => svc.remove(str(p.path, 512))));
  ipcMain.handle('file-memory:open-folder', call(async (svc) => {
    const dir = svc.folder();
    if (!dir) return { ok: false, reason: 'signed_out' };
    const error = await shell.openPath(dir);
    return error ? { ok: false, reason: 'open_failed', text: error } : { ok: true };
  }));
}

module.exports = { registerFileMemoryHandlers };
