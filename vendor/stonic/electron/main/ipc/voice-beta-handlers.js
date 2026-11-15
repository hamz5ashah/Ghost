const { ipcMain } = require('electron');

// IPC surface for Stonic Voice (Beta) — the second, separate voice assistant
// that runs on the user's own API keys (VoiceBetaManager). The Beta screen in
// the renderer talks to this; engine state comes back on `voice-beta:state`.
//
// Jo cheez chabi likhti ya engine chalati hai wo sirf us user ke liye hai jis
// ke paas app ka access hai. Faisla main process ki apni yaad se hota hai
// (`mgr.isAllowed`, main.js deta hai), renderer ke kehne se nahi — UI ka band
// hona kaafi nahi, IPC channel har renderer code ke liye khula rehta hai.
function registerVoiceBetaHandlers({ getVoiceBetaManager }) {
  const withManager = (fn, { needsAccess = false } = {}) => (_event, args = {}) => {
    const mgr = getVoiceBetaManager();
    if (!mgr) return { ok: false, code: 'not_ready', message: 'Voice Beta manager not initialised' };
    if (needsAccess && !mgr.isAllowed()) return { ok: false, code: 'no_access' };
    return fn(mgr, args || {});
  };

  ipcMain.handle('voice-beta:get-config', withManager((mgr) => mgr.getConfig()));
  ipcMain.handle('voice-beta:status', withManager((mgr) => mgr.getStatus()));
  ipcMain.handle('voice-beta:save-config', withManager((mgr, args) => mgr.saveConfig(args), { needsAccess: true }));
  ipcMain.handle('voice-beta:check-provider', withManager((mgr, args) => mgr.checkProvider(args), { needsAccess: true }));
  ipcMain.handle('voice-beta:start', withManager((mgr, args) => mgr.start(args), { needsAccess: true }));
  // Dimagh ka web_search: chabiyan main process mein hi rehti hain (Cloud = gateway, Custom = user ki keys).
  ipcMain.handle('voice-beta:web-search', withManager((mgr, args) => mgr.webSearch(args), { needsAccess: true }));
  // Chat box mein paste ki gayi tasveer (disk par koi file nahi): dimagh ke kholne ke liye PC par rakho.
  ipcMain.handle('voice-beta:stage-files', withManager((mgr, args) => mgr.stageFiles(args), { needsAccess: true }));
  // Band karne wale hamesha khule: access jate hi chalti baat-cheet rukni chahiye.
  ipcMain.handle('voice-beta:hangup', withManager((mgr) => mgr.hangup()));
  ipcMain.handle('voice-beta:leave', withManager((mgr) => mgr.leave()));
  // Chat mitayi gayi: us ki Beta notebook bhi (sirf PC ki files; access ke baghair bhi — mitana hamesha khula).
  ipcMain.handle('voice-beta:forget-session', withManager((mgr, args) => mgr.forgetSession(args)));

  // Classic voice ka kaam (Gemini Live ka `agent_task`): wohi engine, wohi Claude Agent SDK
  // dimagh. Shuru karna access maangta hai; rokna / band karna / haal hamesha khula.
  ipcMain.handle('voice-beta:work-task', withManager((mgr, args) => mgr.workTask(args), { needsAccess: true }));
  // Classic judi: engine pehle se garam (pehla kaam foran) — engine chalata hai, is liye access chahiye.
  ipcMain.handle('voice-beta:work-warm', withManager((mgr) => mgr.workWarm(), { needsAccess: true }));
  ipcMain.handle('voice-beta:work-idle', withManager((mgr) => mgr.workIdle()));
  ipcMain.handle('voice-beta:work-stop', withManager((mgr, args) => mgr.workStop(args)));
  ipcMain.handle('voice-beta:work-close', withManager((mgr, args) => mgr.workClose(args)));
  ipcMain.handle('voice-beta:work-status', withManager((mgr, args) => mgr.workStatus(args)));
}

module.exports = { registerVoiceBetaHandlers };
