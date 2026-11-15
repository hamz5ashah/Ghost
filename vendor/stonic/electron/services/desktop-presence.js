// ═══════════════════════════════════════════════════════════════════════════
//  Desktop Presence — Stonic app ki window se nikal kar desktop par
// ═══════════════════════════════════════════════════════════════════════════
//
//  Do cheezein, dono user ki marzi se (title bar → Desktop):
//
//   1. Overlay ("Live on desktop"): Stonic minimize ho to character desktop
//      par reh jata hai — ek chhoti, shaffaf, hamesha-upar window. Pakki jagah:
//      screen ke UPAR BEECH mein, upar ke kinare se juda kaala "notch" (Taby /
//      Dynamic Island jaisa — Usman, 7 Nov). Chup ho to chhota; hover ya baat
//      par neeche khul kar poora character (renderer: PresenceOverlay).
//      Ghaseetna nahi. Wahin se baat shuru/band, mic,
//      New | Classic, aur wapas poori app. Khaali
//      hissa click-through hai: character ke aas paas ka click neeche wali app
//      ko jata hai. Bari window wapas khuli → overlay ghayab (ek waqt mein ek
//      hi Stonic nazar aaye).
//
//   2. Living wallpaper (sirf Windows): desktop icons ke PEECHE Stonic ki
//      apni duniya — ek window jo wallpaper ki satah par lagti hai
//      (desktop-presence-native.js). Character jahan khara ho roshni wahin;
//      bole to roshni zara tez (lehrein nahi). Bari window / koi app desktop dhaanp le to rendering
//      ruk jati hai.
//
//  Voice yahan nahi chalti. Voice hamesha bari window ke renderer mein hai
//  (minimize ho kar bhi chalti rehti hai); wo yahan apni halat
//  (`presence:snapshot`) aur awaaz ki lehar (`presence:frame`, ~30/s, worker
//  ki ghari se — minimize window ki aam ghari 1/s par atak jati hai) bhejti
//  hai. Ye class sirf in dono ko overlay/wallpaper tak pohanchati hai, aur
//  overlay ke hukm (`presence:command`) wapas bari window ko.
//
//  Kuch bhi ghalat ho to app par asar nahi: overlay na bane to minimize aam
//  minimize hai; wallpaper na lage to toggle khud band ho kar wajah batata hai.

const { app, BrowserWindow, ipcMain, screen, powerMonitor } = require('electron');
const fs = require('fs');
const path = require('path');
const { PresenceNative } = require('./desktop-presence-native');

const PRELOAD = path.join(__dirname, '..', 'preload.js');

// ── Overlay ka naap (CSS px = DIP) ─────────────────────────────────────────
// src/lib/presence/layout.ts mein bhi BILKUL yehi — dono saath badlein.
const STAGE = { s: 150, m: 210, l: 280 };
const THOUGHT_ZONE = 112;
const CAPTION = 34;
const BAR = 52;
const PAD_BOTTOM = 128; // notch ke neeche menu (neeche khulta hai, ~220px) aur soch ke bubble ki jagah
const MIN_WIDTH = 412; // khula panel 376px (patti sab buttons ke saath ~360px) + dono taraf saaye ki jagah
const SIDE = 48;
const SIZES = ['s', 'm', 'l'];

const overlayBox = (size) => {
  const stage = STAGE[size] || STAGE.m;
  return {
    width: Math.max(MIN_WIDTH, stage + SIDE * 2),
    height: THOUGHT_ZONE + stage + CAPTION + BAR + PAD_BOTTOM,
    stage,
  };
};

const HIDE_ANIMATION_MS = 220;
const GHOST_POLL_MS = 600;
const WALLPAPER_START_DELAY_MS = 3_000;
const WALLPAPER_MAX_RETRIES = 3;
const WALLPAPER_FADE_IN_MS = 900;
const WALLPAPER_FADE_OUT_MS = 600;

const DEFAULT_PREFS = Object.freeze({ enabled: false, size: 'm', wallpaper: false });
const COMMANDS = new Set(['toggle', 'mute', 'lane', 'stop-work']);
const LANES = new Set(['beta', 'standard']);
const AGENT_BUSY = new Set(['starting', 'running', 'stopping']);

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const clamp01 = (v) => (Number.isFinite(v) ? clamp(v, 0, 1) : 0);

const clampToArea = (box, area) => ({
  x: Math.round(clamp(box.x, area.x, area.x + area.width - box.width)),
  y: Math.round(clamp(box.y, area.y, area.y + area.height - box.height)),
});

/** Saari screens ka mila-jula dabba (DIP). */
const virtualBounds = () => {
  const displays = screen.getAllDisplays();
  const left = Math.min(...displays.map((d) => d.bounds.x));
  const top = Math.min(...displays.map((d) => d.bounds.y));
  const right = Math.max(...displays.map((d) => d.bounds.x + d.bounds.width));
  const bottom = Math.max(...displays.map((d) => d.bounds.y + d.bounds.height));
  return { x: left, y: top, width: right - left, height: bottom - top };
};

class DesktopPresence {
  /**
   * @param {{
   *   userDataDir: string,
   *   loadPage: (win: BrowserWindow, query: string, label: string) => void,
   *   isLocked?: () => boolean,
   * }} opts
   */
  constructor({ userDataDir, loadPage, isLocked }) {
    this.file = path.join(userDataDir, 'desktop-presence.json');
    this.prefs = this._load();
    this.loadPage = loadPage;
    this.isLocked = typeof isLocked === 'function' ? isLocked : () => false;
    this.native = PresenceNative.supported() ? new PresenceNative({ dir: path.join(userDataDir, 'presence') }) : null;

    this.main = null;
    this.hostReady = false;
    this.snapshot = null;

    this.overlay = null;
    this.overlayLoaded = false;
    this.wantOverlay = false;
    this.overlayOnScreen = false;
    this.hideTimer = null;

    this.wallpaper = null;
    this.wallpaperStatus = 'off';
    this.wallpaperError = null;
    this.wallpaperRetries = 0;
    this.wallpaperClosing = false;
    this.wallpaperStartTimer = null;
    this.wallpaperActive = false;

    this.screenState = { fullscreen: false, covered: false };
    this.systemLocked = false;
    this.suspended = false;
    this.onBattery = false;

    this.agents = [];
    this.ghost = false;
    this.ghostTimer = null;

    this.framesWanted = false;
    this.ipcRegistered = false;

    if (this.native) this.native.on('screen', (state) => this._onScreen(state));
  }

  // ── Bahar se jorna ────────────────────────────────────────────────────────

  attachMainWindow(win) {
    if (!win || win.isDestroyed()) return;
    this.main = win;
    this.hostReady = false;
    win.on('minimize', () => this._onMainMinimize());
    win.on('restore', () => this._onMainRestore());
    win.on('show', () => { if (!win.isMinimized()) this._onMainRestore(); });
    win.on('closed', () => {
      if (this.main === win) this.main = null;
      this.hostReady = false;
      this._destroyAll();
    });
  }

  /** Screen par kaam karne wala agent (ComputerOverlay jaisa: getState().phase). Chale to overlay raasta chhorta hai. */
  watchScreenAgent(agent) {
    if (agent && typeof agent.getState === 'function') this.agents.push(agent);
  }

  /** Face Lock laga: character desktop se hat jaye. */
  handleLock() {
    if (!this.wantOverlay) return;
    this.wantOverlay = false;
    this._syncOverlay();
    this._afterOverlayChange();
  }

  registerIpc() {
    if (this.ipcRegistered) return;
    this.ipcRegistered = true;

    ipcMain.handle('presence:get-state', () => this._state());
    ipcMain.handle('presence:set-prefs', (_event, patch) => this._setPrefs(patch));
    ipcMain.handle('presence:go-desktop', () => {
      this._setPrefs({ enabled: true });
      const main = this.main;
      if (main && !main.isDestroyed()) main.minimize();
      return this._state();
    });

    // Bari window (voice ki maalik) se.
    ipcMain.on('presence:host-ready', (event, ready) => {
      if (!this._fromMain(event)) return;
      const next = Boolean(ready);
      if (next === this.hostReady) return;
      this.hostReady = next;
      if (!next && this.wantOverlay) {
        this.wantOverlay = false;
        this._syncOverlay();
        this._afterOverlayChange();
      }
      if (next) this._scheduleWallpaperStart();
      this._sendHostConfig(true);
    });
    ipcMain.on('presence:snapshot', (event, snapshot) => {
      if (!this._fromMain(event) || !snapshot || typeof snapshot !== 'object') return;
      this.snapshot = snapshot;
      this._sendTo(this.overlay, 'presence:snapshot', snapshot);
      this._sendTo(this.wallpaper, 'presence:snapshot', snapshot);
    });
    ipcMain.on('presence:frame', (event, frame) => {
      if (!this._fromMain(event) || !frame) return;
      const clean = {
        ai: clamp01(Number(frame.ai)),
        mic: clamp01(Number(frame.mic)),
        rms: clamp01(Number(frame.rms)),
        airms: clamp01(Number(frame.airms)),
      };
      if (this.overlayOnScreen) this._sendTo(this.overlay, 'presence:frame', clean);
      if (this.wallpaperActive) this._sendTo(this.wallpaper, 'presence:frame', clean);
    });

    // Overlay se.
    ipcMain.on('presence:overlay-ready', (event) => {
      if (!this._fromOverlay(event)) return;
      this.overlayLoaded = true;
      this._sendTo(this.overlay, 'presence:state', this._state());
      if (this.snapshot) this._sendTo(this.overlay, 'presence:snapshot', this.snapshot);
      this._syncOverlay();
      this._afterOverlayChange();
    });
    ipcMain.on('presence:hit', (event, interactive) => {
      if (!this._fromOverlay(event)) return;
      this._setInteractive(Boolean(interactive));
    });
    ipcMain.on('presence:command', (event, command) => {
      if (!this._fromOverlay(event) || !command || !COMMANDS.has(command.type)) return;
      if (command.type === 'lane' && !LANES.has(command.lane)) return;
      this._sendTo(this.main, 'presence:command', { type: command.type, lane: command.lane });
    });
    ipcMain.on('presence:open-main', (event) => {
      if (!this._fromOverlay(event)) return;
      this._openMain();
    });

    // Wallpaper se.
    ipcMain.on('presence:wallpaper-ready', (event) => {
      if (!this.wallpaper || event.sender !== this.wallpaper.webContents) return;
      void this._attachWallpaper();
    });

    // Screen / bijli ki halat.
    try {
      powerMonitor.on('lock-screen', () => { this.systemLocked = true; this._refreshActive(); });
      powerMonitor.on('unlock-screen', () => { this.systemLocked = false; this._refreshActive(); });
      powerMonitor.on('suspend', () => { this.suspended = true; this._refreshActive(); });
      powerMonitor.on('resume', () => { this.suspended = false; this._refreshActive(); });
      powerMonitor.on('on-battery', () => { this.onBattery = true; this._sendWallpaperPower(); });
      powerMonitor.on('on-ac', () => { this.onBattery = false; this._sendWallpaperPower(); });
      this.onBattery = Boolean(powerMonitor.isOnBatteryPower?.());
    } catch { /* powerMonitor na mile to bas ye signals nahi */ }

    const onDisplays = () => this._onDisplaysChanged();
    screen.on('display-added', onDisplays);
    screen.on('display-removed', onDisplays);
    screen.on('display-metrics-changed', onDisplays);

    app.on('before-quit', () => this.shutdown());
  }

  shutdown() {
    this._destroyAll();
    try { this.native?.stop(); } catch { /* */ }
  }

  // ── Halat ─────────────────────────────────────────────────────────────────

  _state() {
    return {
      prefs: { enabled: this.prefs.enabled, size: this.prefs.size, wallpaper: this.prefs.wallpaper },
      supported: { overlay: true, wallpaper: Boolean(this.native) },
      overlay: this.wantOverlay,
      wallpaper: { status: this.native ? this.wallpaperStatus : 'unsupported', error: this.wallpaperError },
    };
  }

  _broadcastState() {
    const state = this._state();
    this._sendTo(this.main, 'presence:state', state);
    this._sendTo(this.overlay, 'presence:state', state);
    this._sendTo(this.wallpaper, 'presence:state', state);
  }

  _setPrefs(patch) {
    if (!patch || typeof patch !== 'object') return this._state();
    const before = { ...this.prefs };
    if (typeof patch.enabled === 'boolean') this.prefs.enabled = patch.enabled;
    if (typeof patch.size === 'string' && SIZES.includes(patch.size)) this.prefs.size = patch.size;
    if (typeof patch.wallpaper === 'boolean') this.prefs.wallpaper = patch.wallpaper && Boolean(this.native);
    this._save();

    if (before.enabled && !this.prefs.enabled) {
      this.wantOverlay = false;
      this._destroyOverlay();
      this._afterOverlayChange();
    }
    if (before.size !== this.prefs.size) this._placeOverlay();
    if (!before.wallpaper && this.prefs.wallpaper) {
      this.wallpaperError = null;
      this.wallpaperRetries = 0;
      void this._startWallpaper();
    }
    if (before.wallpaper && !this.prefs.wallpaper) this._stopWallpaper({ fade: true });
    this._broadcastState();
    return this._state();
  }

  _load() {
    try {
      const raw = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      // Purani file ki `position` (ghaseetne wale daur ki) ab nahi parhte — jagah pakki hai.
      return {
        enabled: raw.enabled === true,
        size: SIZES.includes(raw.size) ? raw.size : DEFAULT_PREFS.size,
        wallpaper: raw.wallpaper === true,
      };
    } catch {
      return { ...DEFAULT_PREFS };
    }
  }

  _save() {
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      fs.writeFileSync(this.file, JSON.stringify(this.prefs, null, 2), 'utf8');
    } catch (err) {
      console.warn('[Presence] prefs save nahi hui:', err.message);
    }
  }

  // ── Bari window ───────────────────────────────────────────────────────────

  _onMainMinimize() {
    if (!this.prefs.enabled || !this.hostReady || this.isLocked()) return;
    this.wantOverlay = true;
    this._ensureOverlay();
    this._syncOverlay();
    this._afterOverlayChange();
  }

  _onMainRestore() {
    if (!this.wantOverlay) return;
    this.wantOverlay = false;
    this._syncOverlay();
    this._afterOverlayChange();
  }

  _openMain() {
    const main = this.main;
    if (!main || main.isDestroyed()) return;
    try {
      if (main.isMinimized()) main.restore();
      if (!main.isVisible()) main.show();
      // Windows peeche wali app ko seedha aage nahi aane deta — ek lamhe ka
      // always-on-top ye rok paar karta hai (main.js bringAppForward jaisa).
      if (process.platform === 'darwin') app.focus({ steal: true });
      else {
        const pinned = main.isAlwaysOnTop();
        main.setAlwaysOnTop(true);
        main.focus();
        if (!pinned) main.setAlwaysOnTop(false);
      }
      main.focus();
      main.moveTop();
    } catch (err) {
      console.warn('[Presence] bari window khul nahi saki:', err.message);
    }
    this._onMainRestore();
  }

  // ── Overlay window ────────────────────────────────────────────────────────

  _ensureOverlay() {
    if (this.overlay && !this.overlay.isDestroyed()) return;
    const box = overlayBox(this.prefs.size);
    const pos = this._overlayPosition(box);
    let win;
    try {
      win = new BrowserWindow({
        x: pos.x,
        y: pos.y,
        width: box.width,
        height: box.height,
        show: false,
        frame: false,
        transparent: true,
        backgroundColor: '#00000000',
        resizable: false,
        movable: true,
        minimizable: false,
        maximizable: false,
        fullscreenable: false,
        skipTaskbar: true,
        alwaysOnTop: true,
        hasShadow: false,
        // Click se kisi aur app ka focus na chheene (Windows: WS_EX_NOACTIVATE).
        focusable: false,
        title: 'Stonic',
        webPreferences: {
          nodeIntegration: false,
          contextIsolation: true,
          preload: PRELOAD,
        },
      });
    } catch (err) {
      console.warn('[Presence] overlay window nahi bani:', err.message);
      return;
    }
    this.overlay = win;
    this.overlayLoaded = false;
    this.overlayOnScreen = false;
    try { win.setAlwaysOnTop(true, 'floating'); } catch { /* */ }
    try { win.setIgnoreMouseEvents(true, { forward: true }); } catch { /* */ }
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('will-navigate', (event) => event.preventDefault());
    win.on('closed', () => {
      if (this.overlay !== win) return;
      this.overlay = null;
      this.overlayLoaded = false;
      this.overlayOnScreen = false;
      this._afterOverlayChange();
    });
    this.loadPage(win, `?mode=presence&size=${this.prefs.size}`, 'presence overlay');
  }

  _destroyOverlay() {
    const win = this.overlay;
    this.overlay = null;
    this.overlayLoaded = false;
    this.overlayOnScreen = false;
    if (this.hideTimer) { clearTimeout(this.hideTimer); this.hideTimer = null; }
    if (win && !win.isDestroyed()) {
      try { win.destroy(); } catch { /* */ }
    }
  }

  /** Overlay waqai screen par ho ya nahi — chaar shartein. */
  _syncOverlay() {
    const win = this.overlay;
    const should = this.wantOverlay && this.overlayLoaded && !this.screenState.fullscreen
      && !this.systemLocked && !this.suspended;
    if (!win || win.isDestroyed()) { this.overlayOnScreen = false; return; }

    if (should && !this.overlayOnScreen) {
      if (this.hideTimer) { clearTimeout(this.hideTimer); this.hideTimer = null; }
      const box = overlayBox(this.prefs.size);
      const pos = this._overlayPosition(box);
      try {
        win.setBounds({ x: pos.x, y: pos.y, width: box.width, height: box.height });
        win.showInactive();
        win.setAlwaysOnTop(true, 'floating');
        win.setIgnoreMouseEvents(true, { forward: true });
      } catch (err) {
        console.warn('[Presence] overlay dikh nahi saka:', err.message);
        return;
      }
      this.overlayOnScreen = true;
      this._sendTo(win, 'presence:visible', { visible: true });
      this._startGhostWatch();
      return;
    }

    if (!should && this.overlayOnScreen) {
      this.overlayOnScreen = false;
      this._stopGhostWatch();
      this._sendTo(win, 'presence:visible', { visible: false });
      if (this.hideTimer) clearTimeout(this.hideTimer);
      this.hideTimer = setTimeout(() => {
        this.hideTimer = null;
        if (this.overlayOnScreen || !win || win.isDestroyed()) return;
        try { win.hide(); } catch { /* */ }
      }, HIDE_ANIMATION_MS);
    }
  }

  /** Overlay dikha/chhupa — helper, frames aur wallpaper ki roshni ko khabar. */
  _afterOverlayChange() {
    if (this.native) {
      if (this.wantOverlay) this.native.acquire('overlay').catch(() => {});
      else this.native.release('overlay');
    }
    this._sendHostConfig();
    this._sendWallpaperFocus();
    this._broadcastState();
  }

  /**
   * Pakki jagah: jis screen par Stonic khuli thi (warna primary), UPAR BEECH mein, kaam ki jagah
   * (workArea) ke upar ke kinare se bilkul chipki — notch screen ke kinare se nikalta lagta hai.
   * Taskbar upar ho to us ke neeche.
   */
  _overlayPosition(box) {
    let display;
    try {
      const main = this.main;
      display = main && !main.isDestroyed()
        ? screen.getDisplayMatching(main.getNormalBounds ? main.getNormalBounds() : main.getBounds())
        : screen.getPrimaryDisplay();
    } catch {
      display = screen.getPrimaryDisplay();
    }
    const area = display.workArea;
    const x = area.x + (area.width - box.width) / 2;
    return clampToArea({ x, y: area.y, width: box.width, height: box.height }, area);
  }

  /** Size badla ya screen badli: overlay foran apni pakki jagah par. */
  _placeOverlay() {
    const win = this.overlay;
    if (!win || win.isDestroyed()) return;
    const box = overlayBox(this.prefs.size);
    const pos = this._overlayPosition(box);
    try { win.setBounds({ x: pos.x, y: pos.y, width: box.width, height: box.height }); } catch { /* */ }
    this._sendWallpaperFocus();
  }

  _setInteractive(interactive) {
    const win = this.overlay;
    if (!win || win.isDestroyed()) return;
    try {
      if (this.ghost) win.setIgnoreMouseEvents(true);
      else if (interactive) win.setIgnoreMouseEvents(false);
      else win.setIgnoreMouseEvents(true, { forward: true });
    } catch { /* */ }
  }

  // ── Screen par kaam karta agent → overlay raasta chhore ───────────────────

  _startGhostWatch() {
    if (this.ghostTimer || this.agents.length === 0) return;
    const check = () => {
      let busy = false;
      for (const agent of this.agents) {
        try { if (AGENT_BUSY.has(agent.getState().phase)) { busy = true; break; } } catch { /* */ }
      }
      if (busy === this.ghost) return;
      this.ghost = busy;
      this._sendTo(this.overlay, 'presence:ghost', { ghost: busy });
      this._setInteractive(false);
    };
    check();
    this.ghostTimer = setInterval(check, GHOST_POLL_MS);
  }

  _stopGhostWatch() {
    if (this.ghostTimer) { clearInterval(this.ghostTimer); this.ghostTimer = null; }
    if (this.ghost) {
      this.ghost = false;
      this._sendTo(this.overlay, 'presence:ghost', { ghost: false });
    }
  }

  // ── Screen ki halat (helper se) ───────────────────────────────────────────

  _onScreen(state) {
    const changed = state.fullscreen !== this.screenState.fullscreen || state.covered !== this.screenState.covered;
    this.screenState = state;
    if (!changed) return;
    this._syncOverlay();
    this._refreshActive();
  }

  _refreshActive() {
    this._syncOverlay();
    const active = this.wallpaperStatus === 'on' && !this.screenState.covered && !this.systemLocked && !this.suspended;
    if (active !== this.wallpaperActive) {
      this.wallpaperActive = active;
      this._sendTo(this.wallpaper, 'presence:wallpaper-active', { active });
    }
    this._sendHostConfig();
  }

  _onDisplaysChanged() {
    // Resolution / taskbar / screen badli: jagah naye naap se.
    if (this.overlayOnScreen) this._placeOverlay();
    if (this.wallpaper && this.wallpaperStatus === 'on' && this.native) {
      void this.native.place(this.wallpaper.getNativeWindowHandle());
      this._sendWallpaperLayout();
    }
  }

  // ── Bari window ko: frames chahiye ya nahi ────────────────────────────────

  _sendHostConfig(force = false) {
    const want = this.hostReady
      && (this.overlayOnScreen || (this.wantOverlay && !this.screenState.fullscreen) || this.wallpaperActive);
    if (!force && want === this.framesWanted) return;
    this.framesWanted = want;
    this._sendTo(this.main, 'presence:host-config', { frames: want });
  }

  // ── Living wallpaper ──────────────────────────────────────────────────────

  _scheduleWallpaperStart() {
    if (!this.prefs.wallpaper || this.wallpaper || this.wallpaperStartTimer) return;
    this.wallpaperStartTimer = setTimeout(() => {
      this.wallpaperStartTimer = null;
      void this._startWallpaper();
    }, WALLPAPER_START_DELAY_MS);
  }

  async _startWallpaper() {
    if (!this.native || !this.prefs.wallpaper || this.wallpaper) return;
    this.wallpaperStatus = 'starting';
    this.wallpaperError = null;
    this._broadcastState();
    const ok = await this.native.acquire('wallpaper');
    if (!this.prefs.wallpaper) { this.native.release('wallpaper'); return; }
    if (!ok) { this._wallpaperFailed('helper'); return; }

    const v = virtualBounds();
    let win;
    try {
      win = new BrowserWindow({
        // Chhoti aur shaffaf shuru: icons ke peeche lagte hi helper ise saari
        // screens jitna bara karta hai. Pehle bari hoti to ek lamhe sab dhak leti.
        x: v.x,
        y: v.y,
        width: 64,
        height: 64,
        show: false,
        frame: false,
        skipTaskbar: true,
        focusable: false,
        resizable: false,
        movable: false,
        minimizable: false,
        maximizable: false,
        fullscreenable: false,
        hasShadow: false,
        backgroundColor: '#04060a',
        title: 'Stonic Wallpaper',
        webPreferences: {
          nodeIntegration: false,
          contextIsolation: true,
          preload: PRELOAD,
          // Wallpaper ki satah par Chromium ise "chhupi" samajh kar rok deta
          // hai. Rokna hum khud karte hain (desktop dhaka ho to) — helper se.
          backgroundThrottling: false,
        },
      });
    } catch (err) {
      console.warn('[Presence] wallpaper window nahi bani:', err.message);
      this._wallpaperFailed('window');
      return;
    }
    this.wallpaper = win;
    this.wallpaperClosing = false;
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('will-navigate', (event) => event.preventDefault());
    // Page aate hi dikhao (bilkul shaffaf) — chhupi window ko frame nahi milta,
    // aur wallpaper apna pehla frame bana kar hi "tayyar" kehti hai.
    win.webContents.once('did-finish-load', () => {
      if (win.isDestroyed()) return;
      try {
        win.setOpacity(0);
        win.setIgnoreMouseEvents(true);
        win.showInactive();
      } catch { /* */ }
    });
    win.on('closed', () => {
      if (this.wallpaper !== win) return;
      this.wallpaper = null;
      const expected = this.wallpaperClosing;
      this.wallpaperClosing = false;
      this.wallpaperActive = false;
      if (expected || !this.prefs.wallpaper) return;
      // Explorer restart / wallpaper badalne par Windows hamari window bhi mita deta hai.
      this.wallpaperStatus = 'starting';
      this._broadcastState();
      if (this.wallpaperRetries >= WALLPAPER_MAX_RETRIES) { this._wallpaperFailed('lost'); return; }
      this.wallpaperRetries += 1;
      setTimeout(() => { void this._startWallpaper(); }, 2500 * this.wallpaperRetries);
    });
    this.loadPage(win, '?mode=wallpaper', 'presence wallpaper');
  }

  async _attachWallpaper() {
    const win = this.wallpaper;
    if (!win || win.isDestroyed() || !this.native) return;
    try {
      // Chamak se bachne ke liye: bilkul shaffaf hi icons ke peeche, phir aahista dikhao.
      win.setOpacity(0);
      if (!win.isVisible()) win.showInactive();
    } catch { /* */ }
    const result = await this.native.attach(win.getNativeWindowHandle());
    if (this.wallpaper !== win || win.isDestroyed()) return;
    if (!result || !result.ok) {
      console.warn('[Presence] wallpaper desktop par nahi laga:', result && result.error);
      this._stopWallpaper();
      this._wallpaperFailed('attach');
      return;
    }
    // Windows ki wallpaper se hamari tak narm cross-fade.
    this._fadeWallpaper(win, 0, 1, WALLPAPER_FADE_IN_MS);
    console.log(`[Presence] Living wallpaper laga (${result.mode}).`);
    this.wallpaperStatus = 'on';
    this.wallpaperRetries = 0;
    this._sendTo(win, 'presence:state', this._state());
    if (this.snapshot) this._sendTo(win, 'presence:snapshot', this.snapshot);
    this._sendWallpaperLayout();
    this._sendWallpaperFocus();
    this._sendWallpaperPower();
    this.wallpaperActive = false;
    this.native.probe();
    this._refreshActive();
    this._broadcastState();
  }

  /**
   * @param {{ fade?: boolean }} [opts] fade = narmi se wapas Windows ki
   *   wallpaper (user ne khud band kiya). App band ho rahi ho to foran.
   */
  _stopWallpaper({ fade = false } = {}) {
    if (this.wallpaperStartTimer) { clearTimeout(this.wallpaperStartTimer); this.wallpaperStartTimer = null; }
    const win = this.wallpaper;
    this.wallpaper = null;
    this.wallpaperActive = false;
    this.wallpaperStatus = 'off';
    const native = this.native;
    const finish = () => {
      if (win && !win.isDestroyed()) {
        this.wallpaperClosing = true;
        try { win.destroy(); } catch { /* */ }
        if (native) void native.refresh().finally(() => { if (!this.wallpaper) native.release('wallpaper'); });
      } else if (native && !this.wallpaper) {
        native.release('wallpaper');
      }
    };
    if (win && !win.isDestroyed()) this.wallpaperClosing = true;
    if (fade && win && !win.isDestroyed()) this._fadeWallpaper(win, 1, 0, WALLPAPER_FADE_OUT_MS, finish);
    else finish();
    this._sendHostConfig();
  }

  _fadeWallpaper(win, from, to, ms, done) {
    const started = Date.now();
    const step = () => {
      if (!win || win.isDestroyed()) { done?.(); return; }
      const t = Math.min(1, (Date.now() - started) / ms);
      const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      try { win.setOpacity(from + (to - from) * e); } catch { /* */ }
      if (t < 1) setTimeout(step, 16);
      else done?.();
    };
    step();
  }

  _wallpaperFailed(reason) {
    this.wallpaperStatus = 'error';
    this.wallpaperError = reason;
    this.prefs.wallpaper = false;
    this._save();
    if (this.native) this.native.release('wallpaper');
    this._broadcastState();
  }

  _sendWallpaperLayout() {
    if (!this.wallpaper) return;
    const v = virtualBounds();
    const displays = screen.getAllDisplays().map((d) => ({
      x: (d.bounds.x - v.x) / v.width,
      y: (d.bounds.y - v.y) / v.height,
      width: d.bounds.width / v.width,
      height: d.bounds.height / v.height,
      primary: d.id === screen.getPrimaryDisplay().id,
    }));
    this._sendTo(this.wallpaper, 'presence:wallpaper-layout', { width: v.width, height: v.height, displays });
  }

  /** Character kahan khara hai (saari screens ke dabbe mein 0..1) — wallpaper ki roshni wahin. */
  _sendWallpaperFocus() {
    if (!this.wallpaper) return;
    const v = virtualBounds();
    const win = this.overlay;
    let focus;
    if (this.overlayOnScreen && win && !win.isDestroyed()) {
      const b = win.getBounds();
      const box = overlayBox(this.prefs.size);
      focus = {
        present: true,
        x: (b.x + b.width / 2 - v.x) / v.width,
        // Character notch ke andar, window ke upar wale hisse mein (soch ab neeche).
        y: (b.y + box.stage * 0.5 - v.y) / v.height,
      };
    } else {
      const p = screen.getPrimaryDisplay().bounds;
      focus = { present: false, x: (p.x + p.width * 0.7 - v.x) / v.width, y: (p.y + p.height * 0.42 - v.y) / v.height };
    }
    this._sendTo(this.wallpaper, 'presence:wallpaper-focus', focus);
  }

  _sendWallpaperPower() {
    this._sendTo(this.wallpaper, 'presence:wallpaper-power', { battery: this.onBattery });
  }

  // ── Chhoti cheezein ───────────────────────────────────────────────────────

  _destroyAll() {
    this.wantOverlay = false;
    this._stopGhostWatch();
    this._destroyOverlay();
    const wasPref = this.prefs.wallpaper;
    this._stopWallpaper();
    this.prefs.wallpaper = wasPref;
    if (this.native) this.native.release('overlay');
  }

  _fromMain(event) {
    return Boolean(this.main && !this.main.isDestroyed() && event.sender === this.main.webContents);
  }

  _fromOverlay(event) {
    return Boolean(this.overlay && !this.overlay.isDestroyed() && event.sender === this.overlay.webContents);
  }

  _sendTo(win, channel, payload) {
    if (!win || win.isDestroyed()) return;
    try { win.webContents.send(channel, payload); } catch { /* window ja rahi hai */ }
  }
}

module.exports = { DesktopPresence, overlayBox };
