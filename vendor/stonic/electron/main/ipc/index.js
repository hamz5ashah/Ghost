const { registerSkillsHandlers } = require('./skills-handlers');
const { registerSystemHandlers } = require('./system-handlers');
const { registerWhatsAppHandlers } = require('./whatsapp-handlers');
const { registerComputerHandlers } = require('./computer-handlers');
const { registerAwarenessHandlers } = require('./awareness-handlers');
const { registerMemoryHandlers } = require('./memory-handlers');
const { registerFaceLockHandlers } = require('./facelock-handlers');
const { registerOnboardingHandlers } = require('./onboarding-handlers');
const { registerVoiceBetaHandlers } = require('./voice-beta-handlers');
const { registerFileMemoryHandlers } = require('./file-memory-handlers');
const { registerWorldFeedHandlers } = require('./world-feed-handlers');
const { registerWeatherHandlers } = require('./weather-handlers');
const { registerTutorialHandlers } = require('./tutorial-handlers');

// Shared state for cross-module access (set by main.js after window creation)
let _mainWindow = null;
let _computerManager = null;
let _awarenessManager = null;
let _memoryService = null;
let _faceLockManager = null;
let _voiceBetaManager = null;
let _fileMemoryService = null;

function registerDomainHandlers({ getMediaDir } = {}) {
  registerSystemHandlers();
  registerSkillsHandlers();
  registerWhatsAppHandlers();
  // Computer use (Agent S3 sidecar). Managers getter se aate hain, seedhi value
  // se nahi: ye handlers window banne se PEHLE register hote hain, aur managers
  // us waqt mojood hi nahi hote — main.js unhe baad mein set karta hai.
  registerComputerHandlers({
    getComputerManager: () => _computerManager,
  });
  // Awareness (Proactive Presence ki aankhein). Getter, same reason as above.
  registerAwarenessHandlers({
    getAwarenessManager: () => _awarenessManager,
  });
  // Memory Core (Stonic ki apni yaadash). Getter, same reason as above.
  registerMemoryHandlers({
    getMemoryService: () => _memoryService,
  });
  // Face Lock (Settings → Profile ka taala). Getter, same reason as above.
  registerFaceLockHandlers({
    getFaceLockManager: () => _faceLockManager,
  });
  // Onboarding ka system scan (is machine ke asli haqaiq, sirf screen ke liye).
  registerOnboardingHandlers();
  // Stonic Voice (Beta): doosra, alag voice assistant — user ki apni API keys par.
  // Getter, same reason as above.
  registerVoiceBetaHandlers({
    getVoiceBetaManager: () => _voiceBetaManager,
  });
  // Nayi yaadash — ek folder (%APPDATA%\Stonic AI\Memory). Getter, same reason as above.
  registerFileMemoryHandlers({
    getFileMemoryService: () => _fileMemoryService,
  });
  // World Feed: apni khabrein (khule zariye se), main process mein cache.
  registerWorldFeedHandlers();
  // Mausam ("Around you"): MET Norway, main process mein cache.
  registerWeatherHandlers();
  // Settings → Tutorials: app ke saath aane wali videos (bahar ke player mein kholna).
  registerTutorialHandlers({ getMediaDir });
}

function setMainWindow(win) { _mainWindow = win; }
function getMainWindow() { return _mainWindow; }
function getComputerManager() { return _computerManager; }
function setComputerManager(mgr) { _computerManager = mgr; }
function getAwarenessManager() { return _awarenessManager; }
function setAwarenessManager(mgr) { _awarenessManager = mgr; }
function getMemoryService() { return _memoryService; }
function setMemoryService(svc) { _memoryService = svc; }
function getFaceLockManager() { return _faceLockManager; }
function setFaceLockManager(mgr) { _faceLockManager = mgr; }
function getVoiceBetaManager() { return _voiceBetaManager; }
function setVoiceBetaManager(mgr) { _voiceBetaManager = mgr; }
function getFileMemoryService() { return _fileMemoryService; }
function setFileMemoryService(svc) { _fileMemoryService = svc; }

module.exports = {
  registerDomainHandlers,
  setMainWindow,
  getMainWindow,
  getComputerManager,
  setComputerManager,
  getAwarenessManager,
  setAwarenessManager,
  getMemoryService,
  setMemoryService,
  getFaceLockManager,
  setFaceLockManager,
  getVoiceBetaManager,
  setVoiceBetaManager,
  getFileMemoryService,
  setFileMemoryService,
};
