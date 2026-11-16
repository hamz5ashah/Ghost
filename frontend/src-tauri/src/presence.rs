//! Desktop presence (Stonic port, Phase 5): Ghost stays on the desktop.
//!
//! Ported from Stonic's `desktop-presence.js` and its native helper:
//!
//! 1. **Overlay ("live on the desktop").** When Ghost's window is minimised,
//!    a small notch sits at the top centre of the screen it was on: the
//!    character, a caption, and talk / mute / open buttons. It is
//!    click-through everywhere except the notch itself, never takes focus,
//!    and steps aside for full-screen apps (a game, a film, a presentation)
//!    and while Windows is locked.
//! 2. **Living wallpaper (Windows only).** A quiet animated scene behind the
//!    desktop icons, attached with the same WorkerW technique Stonic used.
//!    It pauses whenever the desktop is fully covered or Windows is locked,
//!    and drops its frame rate on battery.
//!
//! Both are off unless `[stonic].desktop_presence` is on *and* the user
//! turns them on (Settings or the notch's menu). If anything fails, the app is
//! unaffected: without an overlay, minimise is an ordinary minimise; a
//! wallpaper that cannot attach switches itself off and says why.

#![cfg_attr(not(target_os = "windows"), allow(dead_code))]

use serde::{Deserialize, Serialize};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};

pub const OVERLAY: &str = "presence";
pub const WALLPAPER: &str = "wallpaper";
const MAIN: &str = "main";

// Layout, unchanged from Stonic (CSS px).
const THOUGHT_ZONE: u32 = 112;
const CAPTION: u32 = 34;
const BAR: u32 = 52;
const PAD_BOTTOM: u32 = 128;
const MIN_WIDTH: u32 = 412;
const SIDE: u32 = 48;

const FLAG_TTL: Duration = Duration::from_secs(2);
const HOVER_POLL: Duration = Duration::from_millis(120);
const IDLE_POLL: Duration = Duration::from_millis(1000);
const SCREEN_POLL: Duration = Duration::from_millis(1000);
const WALLPAPER_START_DELAY: Duration = Duration::from_secs(3);
const WALLPAPER_FADE_IN_MS: u64 = 900;

pub fn stage_for(size: &str) -> u32 {
    match size {
        "s" => 150,
        "l" => 280,
        _ => 210,
    }
}

/// Overlay window size for a character size. Pure.
pub fn overlay_box(size: &str) -> (u32, u32) {
    let stage = stage_for(size);
    (
        MIN_WIDTH.max(stage + SIDE * 2),
        THOUGHT_ZONE + stage + CAPTION + BAR + PAD_BOTTOM,
    )
}

/// Top centre of a work area, clamped inside it. Pure. All values physical px.
pub fn top_centre(area: (i32, i32, u32, u32), size: (u32, u32)) -> (i32, i32) {
    let (ax, ay, aw, ah) = area;
    let x = ax + (aw as i32 - size.0 as i32) / 2;
    let max_x = ax + aw as i32 - size.0 as i32;
    let max_y = ay + ah as i32 - size.1 as i32;
    (x.clamp(ax, max_x.max(ax)), ay.clamp(ay, max_y.max(ay)))
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq)]
pub struct HitRect {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

/// Is a cursor (physical px) inside a hit rect given in CSS px of a window
/// at `origin` (physical px) with `scale`? Pure.
pub fn cursor_in(cursor: (f64, f64), origin: (f64, f64), scale: f64, r: &HitRect) -> bool {
    let x = (cursor.0 - origin.0) / scale;
    let y = (cursor.1 - origin.1) / scale;
    x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct Prefs {
    pub enabled: bool,
    pub size: String,
    pub wallpaper: bool,
}

impl Default for Prefs {
    fn default() -> Self {
        Prefs {
            enabled: false,
            size: "m".into(),
            wallpaper: false,
        }
    }
}

/// Prefs from the saved file; anything odd falls back to the default. Pure.
pub fn prefs_from(text: &str) -> Prefs {
    let v: serde_json::Value = serde_json::from_str(text).unwrap_or_default();
    let size = v.get("size").and_then(|s| s.as_str()).unwrap_or("m");
    Prefs {
        enabled: v.get("enabled").and_then(|b| b.as_bool()).unwrap_or(false),
        size: if ["s", "m", "l"].contains(&size) {
            size
        } else {
            "m"
        }
        .into(),
        wallpaper: v
            .get("wallpaper")
            .and_then(|b| b.as_bool())
            .unwrap_or(false),
    }
}

fn prefs_path() -> std::path::PathBuf {
    crate::ghost_config_toml_path().with_file_name("desktop-presence.json")
}

/// `[stonic].desktop_presence` from a config.toml string. Pure.
pub fn flag_in(config_text: &str) -> bool {
    crate::stonic_flags_from_config(config_text)
        .get("desktop_presence")
        .copied()
        .unwrap_or(false)
}

static FLAG_CACHE: Mutex<Option<(Instant, bool)>> = Mutex::new(None);

fn flag_on() -> bool {
    let mut cache = FLAG_CACHE.lock().unwrap_or_else(|e| e.into_inner());
    if let Some((at, on)) = *cache {
        if at.elapsed() < FLAG_TTL {
            return on;
        }
    }
    let text = std::fs::read_to_string(crate::ghost_config_toml_path()).unwrap_or_default();
    let on = flag_in(&text);
    *cache = Some((Instant::now(), on));
    on
}

#[derive(Debug, Default)]
struct State {
    prefs: Prefs,
    loaded: bool,
    main_minimized: bool,
    overlay_shown: bool,
    hit: Option<HitRect>,
    hover: bool,
    fullscreen: bool,
    covered: bool,
    locked: bool,
    on_battery: bool,
    wallpaper_status: &'static str,
    wallpaper_error: Option<String>,
    wallpaper_active: bool,
    wallpaper_wanted_at: Option<Instant>,
}

static STATE: Mutex<Option<State>> = Mutex::new(None);

fn with_state<T>(f: impl FnOnce(&mut State) -> T) -> T {
    let mut guard = STATE.lock().unwrap_or_else(|e| e.into_inner());
    let st = guard.get_or_insert_with(State::default);
    if !st.loaded {
        st.loaded = true;
        st.prefs = std::fs::read_to_string(prefs_path())
            .map(|t| prefs_from(&t))
            .unwrap_or_default();
        st.wallpaper_status = "off";
    }
    f(st)
}

#[derive(Serialize)]
pub struct PresenceInfo {
    available: bool,
    prefs: Prefs,
    overlay: bool,
    wallpaper_supported: bool,
    wallpaper: &'static str,
    wallpaper_error: Option<String>,
}

fn info() -> PresenceInfo {
    let available = flag_on();
    with_state(|st| PresenceInfo {
        available,
        prefs: st.prefs.clone(),
        overlay: st.overlay_shown,
        wallpaper_supported: cfg!(target_os = "windows"),
        wallpaper: st.wallpaper_status,
        wallpaper_error: st.wallpaper_error.clone(),
    })
}

// -- commands ------------------------------------------------------------------

#[tauri::command]
pub fn presence_state() -> PresenceInfo {
    info()
}

#[derive(Deserialize)]
pub struct PrefsPatch {
    enabled: Option<bool>,
    size: Option<String>,
    wallpaper: Option<bool>,
}

#[tauri::command]
pub fn presence_set_prefs(app: AppHandle, patch: PrefsPatch) -> Result<PresenceInfo, String> {
    if !flag_on() {
        return Err("disabled".into());
    }
    let prefs = with_state(|st| {
        if let Some(e) = patch.enabled {
            st.prefs.enabled = e;
        }
        if let Some(s) = patch.size.as_deref() {
            if ["s", "m", "l"].contains(&s) {
                st.prefs.size = s.into();
            }
        }
        if let Some(w) = patch.wallpaper {
            st.prefs.wallpaper = w && cfg!(target_os = "windows");
            if st.prefs.wallpaper {
                st.wallpaper_error = None;
                st.wallpaper_wanted_at = Some(Instant::now());
            }
        }
        st.prefs.clone()
    });
    if let Ok(text) = serde_json::to_string(&prefs) {
        let _ = std::fs::write(prefs_path(), text);
    }
    place_overlay(&app);
    let _ = app.emit_to(OVERLAY, "presence:prefs", &prefs);
    Ok(info())
}

/// The notch reports where its clickable part is (CSS px in its window).
#[tauri::command]
pub fn presence_hit(window: tauri::Window, rect: Option<HitRect>) {
    if window.label() != OVERLAY {
        return;
    }
    with_state(|st| st.hit = rect);
}

/// A button on the notch. `toggle` / `mute` go to the main window (which owns
/// voice); `open` brings Ghost back; `leave` turns the overlay off.
#[tauri::command]
pub fn presence_command(app: AppHandle, window: tauri::Window, kind: String) -> Result<(), String> {
    if window.label() != OVERLAY {
        return Err("not the overlay".into());
    }
    match kind.as_str() {
        "toggle" | "mute" => {
            let _ = app.emit_to(
                MAIN,
                "presence:command",
                serde_json::json!({ "type": kind }),
            );
        }
        "open" => restore_main(&app),
        "leave" => {
            with_state(|st| st.prefs.enabled = false);
            let prefs = with_state(|st| st.prefs.clone());
            if let Ok(text) = serde_json::to_string(&prefs) {
                let _ = std::fs::write(prefs_path(), text);
            }
            restore_main(&app);
        }
        _ => return Err("unknown command".into()),
    }
    Ok(())
}

/// The main window's live state (voice phase, caption, the latest thought),
/// relayed to the notch and the wallpaper.
#[tauri::command]
pub fn presence_snapshot(app: AppHandle, window: tauri::Window, snapshot: serde_json::Value) {
    if window.label() != MAIN {
        return;
    }
    let _ = app.emit_to(OVERLAY, "presence:snapshot", &snapshot);
    let _ = app.emit_to(WALLPAPER, "presence:snapshot", &snapshot);
}

fn restore_main(app: &AppHandle) {
    if let Some(main) = app.get_webview_window(MAIN) {
        let _ = main.unminimize();
        let _ = main.show();
        let _ = main.set_focus();
    }
}

// -- main window events --------------------------------------------------------

/// Called from the main window's event hook in `lib.rs`.
pub fn on_main_event(app: &AppHandle, event: &tauri::WindowEvent) {
    let minimized = match event {
        tauri::WindowEvent::Resized(_) | tauri::WindowEvent::Focused(_) => app
            .get_webview_window(MAIN)
            .and_then(|w| w.is_minimized().ok())
            .unwrap_or(false),
        _ => return,
    };
    let changed = with_state(|st| {
        let c = st.main_minimized != minimized;
        st.main_minimized = minimized;
        c
    });
    if changed {
        sync_overlay(app);
    }
}

// -- overlay -------------------------------------------------------------------

fn should_show(st: &State, flag: bool) -> bool {
    flag && st.prefs.enabled && st.main_minimized && !st.fullscreen && !st.locked
}

fn ensure_overlay(app: &AppHandle) -> Option<tauri::WebviewWindow> {
    if let Some(w) = app.get_webview_window(OVERLAY) {
        return Some(w);
    }
    let size = with_state(|st| st.prefs.size.clone());
    let (w, h) = overlay_box(&size);
    let url = format!("index.html?mode=presence&size={size}");
    let win = WebviewWindowBuilder::new(app, OVERLAY, WebviewUrl::App(url.into()))
        .title("Ghost")
        .inner_size(w as f64, h as f64)
        .decorations(false)
        .transparent(true)
        .shadow(false)
        .resizable(false)
        .maximizable(false)
        .minimizable(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .focusable(false)
        .focused(false)
        .visible(false)
        .build()
        .ok()?;
    let _ = win.set_ignore_cursor_events(true);
    Some(win)
}

fn place_overlay(app: &AppHandle) {
    let Some(win) = app.get_webview_window(OVERLAY) else {
        return;
    };
    let size = with_state(|st| st.prefs.size.clone());
    let (w, h) = overlay_box(&size);
    // On the screen Ghost was on (else primary), top centre of its work area.
    let monitor = app
        .get_webview_window(MAIN)
        .and_then(|m| m.current_monitor().ok().flatten())
        .or_else(|| win.primary_monitor().ok().flatten());
    let Some(mon) = monitor else {
        return;
    };
    let scale = mon.scale_factor();
    let pw = (w as f64 * scale).round() as u32;
    let ph = (h as f64 * scale).round() as u32;
    let area = mon.work_area();
    let (x, y) = top_centre(
        (
            area.position.x,
            area.position.y,
            area.size.width,
            area.size.height,
        ),
        (pw, ph),
    );
    let _ = win.set_size(tauri::PhysicalSize::new(pw, ph));
    let _ = win.set_position(tauri::PhysicalPosition::new(x, y));
}

fn sync_overlay(app: &AppHandle) {
    let flag = flag_on();
    let (want, shown) = with_state(|st| (should_show(st, flag), st.overlay_shown));
    if want && !shown {
        let Some(win) = ensure_overlay(app) else {
            return;
        };
        place_overlay(app);
        let _ = win.set_ignore_cursor_events(true);
        let _ = win.show();
        let _ = win.set_always_on_top(true);
        with_state(|st| {
            st.overlay_shown = true;
            st.hover = false;
        });
        let _ = app.emit_to(
            OVERLAY,
            "presence:visible",
            serde_json::json!({ "visible": true }),
        );
    } else if !want && shown {
        with_state(|st| st.overlay_shown = false);
        let _ = app.emit_to(
            OVERLAY,
            "presence:visible",
            serde_json::json!({ "visible": false }),
        );
        if let Some(win) = app.get_webview_window(OVERLAY) {
            let _ = win.set_ignore_cursor_events(true);
            let _ = win.hide();
        }
    }
}

/// Tauri has no "click-through but still see the mouse" mode, so the notch's
/// hover is worked out here: the cursor inside the reported hit rect makes the
/// window clickable; outside, clicks pass through to whatever is underneath.
fn poll_hover(app: &AppHandle) {
    let Some(win) = app.get_webview_window(OVERLAY) else {
        return;
    };
    let (rect, was) = with_state(|st| (st.hit, st.hover));
    let inside = match (
        rect,
        app.cursor_position(),
        win.outer_position(),
        win.scale_factor(),
    ) {
        (Some(r), Ok(c), Ok(o), Ok(s)) => cursor_in((c.x, c.y), (o.x as f64, o.y as f64), s, &r),
        _ => false,
    };
    if inside != was {
        with_state(|st| st.hover = inside);
        let _ = win.set_ignore_cursor_events(!inside);
        let _ = app.emit_to(
            OVERLAY,
            "presence:hover",
            serde_json::json!({ "hover": inside }),
        );
    }
}

// -- the loop ------------------------------------------------------------------

pub fn start(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let mut last_screen = Instant::now() - SCREEN_POLL;
        loop {
            let flag = flag_on();
            let shown = with_state(|st| st.overlay_shown);
            if flag && last_screen.elapsed() >= SCREEN_POLL {
                last_screen = Instant::now();
                let screen = tokio::task::spawn_blocking(os::screen_state)
                    .await
                    .unwrap_or_default();
                let changed = with_state(|st| {
                    let c = (st.fullscreen, st.covered, st.locked, st.on_battery)
                        != (
                            screen.fullscreen,
                            screen.covered,
                            screen.locked,
                            screen.on_battery,
                        );
                    st.fullscreen = screen.fullscreen;
                    st.covered = screen.covered;
                    st.locked = screen.locked;
                    st.on_battery = screen.on_battery;
                    c
                });
                if changed {
                    let _ = app.emit_to(
                        WALLPAPER,
                        "presence:wallpaper-power",
                        serde_json::json!({ "battery": screen.on_battery }),
                    );
                }
            }
            sync_overlay(&app);
            if shown {
                poll_hover(&app);
            }
            wallpaper_step(&app, flag).await;
            tokio::time::sleep(if shown { HOVER_POLL } else { IDLE_POLL }).await;
        }
    });
}

// -- living wallpaper ----------------------------------------------------------

async fn wallpaper_step(app: &AppHandle, flag: bool) {
    let (want, status, wanted_at, covered, locked, was_active) = with_state(|st| {
        (
            flag && st.prefs.wallpaper && cfg!(target_os = "windows"),
            st.wallpaper_status,
            st.wallpaper_wanted_at,
            st.covered,
            st.locked,
            st.wallpaper_active,
        )
    });
    if !want {
        if status != "off" {
            stop_wallpaper(app);
        }
        return;
    }
    match status {
        "off" => {
            // A short delay so turning it on and off again quickly does nothing.
            let ready = wanted_at
                .map(|t| t.elapsed() >= WALLPAPER_START_DELAY)
                .unwrap_or(true);
            if ready {
                start_wallpaper(app).await;
            }
        }
        "on" => {
            let active = !covered && !locked;
            if active != was_active {
                with_state(|st| st.wallpaper_active = active);
                let _ = app.emit_to(
                    WALLPAPER,
                    "presence:wallpaper-active",
                    serde_json::json!({ "active": active }),
                );
            }
            if app.get_webview_window(WALLPAPER).is_none() {
                // Explorer restarted, or the wallpaper changed: Windows took our window.
                with_state(|st| {
                    st.wallpaper_status = "off";
                    st.wallpaper_active = false;
                    st.wallpaper_wanted_at = Some(Instant::now());
                });
            }
        }
        _ => {}
    }
}

fn wallpaper_failed(app: &AppHandle, why: &str) {
    stop_wallpaper(app);
    with_state(|st| {
        st.prefs.wallpaper = false;
        st.wallpaper_error = Some(why.into());
        if let Ok(text) = serde_json::to_string(&st.prefs) {
            let _ = std::fs::write(prefs_path(), text);
        }
    });
    let _ = app.emit_to(OVERLAY, "presence:prefs", with_state(|st| st.prefs.clone()));
}

async fn start_wallpaper(app: &AppHandle) {
    with_state(|st| st.wallpaper_status = "starting");
    let built = WebviewWindowBuilder::new(
        app,
        WALLPAPER,
        WebviewUrl::App("index.html?mode=wallpaper".into()),
    )
    .title("Ghost Wallpaper")
    .inner_size(64.0, 64.0)
    .decorations(false)
    .resizable(false)
    .skip_taskbar(true)
    .focusable(false)
    .focused(false)
    .shadow(false)
    .visible(false)
    .build();
    let Ok(win) = built else {
        wallpaper_failed(app, "window");
        return;
    };
    // Give the page a moment to paint its first frame before it goes behind the icons.
    tokio::time::sleep(Duration::from_millis(600)).await;
    #[cfg(target_os = "windows")]
    {
        let Ok(hwnd) = win.hwnd() else {
            wallpaper_failed(app, "window");
            return;
        };
        let raw = hwnd.0 as isize;
        let _ = win.set_ignore_cursor_events(true);
        let attached = tokio::task::spawn_blocking(move || os::attach(raw))
            .await
            .unwrap_or_else(|_| Err("attach".into()));
        match attached {
            Ok(mode) => {
                let _ = win.show();
                tokio::task::spawn_blocking(move || os::fade_in(raw, WALLPAPER_FADE_IN_MS));
                with_state(|st| {
                    st.wallpaper_status = "on";
                    st.wallpaper_active = false;
                });
                eprintln!("[Presence] living wallpaper attached ({mode})");
            }
            Err(why) => {
                eprintln!("[Presence] wallpaper did not attach: {why}");
                wallpaper_failed(app, "attach");
            }
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = win.close();
        wallpaper_failed(app, "unsupported");
    }
}

fn stop_wallpaper(app: &AppHandle) {
    if let Some(win) = app.get_webview_window(WALLPAPER) {
        let _ = win.destroy();
    }
    with_state(|st| {
        st.wallpaper_status = "off";
        st.wallpaper_active = false;
    });
    // On older Windows the desktop keeps our last frame until it repaints.
    os::refresh_desktop();
}

// -- native --------------------------------------------------------------------

#[derive(Debug, Default, Clone, Copy)]
pub struct ScreenState {
    pub fullscreen: bool,
    pub covered: bool,
    pub locked: bool,
    pub on_battery: bool,
}

/// Does a window rect hide (90%+ of) a monitor's work area? Pure.
pub fn hides(win: (i32, i32, i32, i32), work: (i32, i32, i32, i32)) -> bool {
    let area = (work.2 - work.0) as i64 * (work.3 - work.1) as i64;
    if area <= 0 {
        return true;
    }
    let w = (win.2.min(work.2) - win.0.max(work.0)) as i64;
    let h = (win.3.min(work.3) - win.1.max(work.1)) as i64;
    w > 0 && h > 0 && w * h >= area * 9 / 10
}

#[cfg(target_os = "windows")]
mod os {
    use super::{hides, ScreenState};
    use windows_sys::Win32::Foundation::{BOOL, HWND, LPARAM, RECT};
    use windows_sys::Win32::Graphics::Dwm::{DwmGetWindowAttribute, DWMWA_CLOAKED};
    use windows_sys::Win32::Graphics::Gdi::{
        EnumDisplayMonitors, GetMonitorInfoW, RedrawWindow, ScreenToClient, HDC, HMONITOR,
        MONITORINFO, RDW_ALLCHILDREN, RDW_ERASE, RDW_INVALIDATE, RDW_UPDATENOW,
    };
    use windows_sys::Win32::UI::Shell::{
        SHQueryUserNotificationState, QUNS_BUSY, QUNS_PRESENTATION_MODE,
        QUNS_RUNNING_D3D_FULL_SCREEN,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        EnumWindows, FindWindowExW, FindWindowW, GetClassNameW, GetParent, GetSystemMetrics,
        GetWindow, GetWindowLongW, GetWindowRect, IsIconic, IsWindow, IsWindowVisible,
        SendMessageTimeoutW, SetLayeredWindowAttributes, SetParent, SetWindowLongW, SetWindowPos,
        GWL_EXSTYLE, GWL_STYLE, GW_CHILD, GW_HWNDLAST, HWND_BOTTOM, LWA_ALPHA, SMTO_NORMAL,
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN,
        SWP_FRAMECHANGED, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_SHOWWINDOW,
        WS_CAPTION, WS_CHILD, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_NOREDIRECTIONBITMAP,
        WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT, WS_POPUP, WS_THICKFRAME,
    };

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn class_of(h: HWND) -> String {
        let mut buf = [0u16; 128];
        let n = unsafe { GetClassNameW(h, buf.as_mut_ptr(), buf.len() as i32) };
        String::from_utf16_lossy(&buf[..n.max(0) as usize])
    }

    struct Desktop {
        progman: HWND,
        def_view: HWND,
        worker: HWND,
        raised: bool,
    }

    unsafe extern "system" fn find_def_view(h: HWND, data: LPARAM) -> BOOL {
        let out = &mut *(data as *mut (HWND, HWND));
        let class = wide("SHELLDLL_DefView");
        let dv = FindWindowExW(h, std::ptr::null_mut(), class.as_ptr(), std::ptr::null());
        if !dv.is_null() {
            *out = (h, dv);
            return 0;
        }
        1
    }

    /// Find the surface behind the icons (Stonic's `Find`).
    fn find() -> Desktop {
        let progman_class = wide("Progman");
        let worker_class = wide("WorkerW");
        let def_class = wide("SHELLDLL_DefView");
        let progman = unsafe { FindWindowW(progman_class.as_ptr(), std::ptr::null()) };
        let mut d = Desktop {
            progman,
            def_view: std::ptr::null_mut(),
            worker: std::ptr::null_mut(),
            raised: false,
        };
        if progman.is_null() {
            return d;
        }
        // Ask Explorer to create the wallpaper WorkerW (it also does this for
        // its own wallpaper transition animation).
        let mut ignored: usize = 0;
        unsafe {
            SendMessageTimeoutW(progman, 0x052C, 0xD, 1, SMTO_NORMAL, 1000, &mut ignored);
        }
        // Windows 11 24H2+: the desktop is "raised" and WorkerW lives inside Progman.
        let ex = unsafe { GetWindowLongW(progman, GWL_EXSTYLE) } as u32;
        d.raised = ex & WS_EX_NOREDIRECTIONBITMAP != 0;
        if d.raised {
            d.def_view = unsafe {
                FindWindowExW(
                    progman,
                    std::ptr::null_mut(),
                    def_class.as_ptr(),
                    std::ptr::null(),
                )
            };
            d.worker = unsafe {
                FindWindowExW(
                    progman,
                    std::ptr::null_mut(),
                    worker_class.as_ptr(),
                    std::ptr::null(),
                )
            };
            if !d.worker.is_null() {
                return d;
            }
            d.raised = false;
        }
        let mut found: (HWND, HWND) = (std::ptr::null_mut(), std::ptr::null_mut());
        unsafe { EnumWindows(Some(find_def_view), &mut found as *mut _ as LPARAM) };
        if !found.0.is_null() {
            d.def_view = found.1;
            d.worker = unsafe {
                FindWindowExW(
                    std::ptr::null_mut(),
                    found.0,
                    worker_class.as_ptr(),
                    std::ptr::null(),
                )
            };
        }
        d
    }

    fn parent_for(d: &Desktop) -> HWND {
        if d.raised || d.worker.is_null() {
            d.progman
        } else {
            d.worker
        }
    }

    /// Cover the whole virtual screen, in the parent's client coordinates.
    fn place(hwnd: HWND, parent: HWND) {
        unsafe {
            let mut p = windows_sys::Win32::Foundation::POINT {
                x: GetSystemMetrics(SM_XVIRTUALSCREEN),
                y: GetSystemMetrics(SM_YVIRTUALSCREEN),
            };
            let w = GetSystemMetrics(SM_CXVIRTUALSCREEN);
            let h = GetSystemMetrics(SM_CYVIRTUALSCREEN);
            ScreenToClient(parent, &mut p);
            SetWindowPos(
                hwnd,
                std::ptr::null_mut(),
                p.x,
                p.y,
                w,
                h,
                SWP_NOACTIVATE | SWP_NOZORDER | SWP_SHOWWINDOW,
            );
        }
    }

    /// Attach a window behind the desktop icons. Ok(mode) or Err(why).
    pub fn attach(raw: isize) -> Result<&'static str, String> {
        let hwnd = raw as HWND;
        unsafe {
            if IsWindow(hwnd) == 0 {
                return Err("no_window".into());
            }
            let d = find();
            if d.progman.is_null() {
                return Err("no_progman".into());
            }
            let parent = parent_for(&d);
            let style = GetWindowLongW(hwnd, GWL_STYLE) as u32;
            let style = (style & !WS_POPUP & !WS_CAPTION & !WS_THICKFRAME) | WS_CHILD;
            SetWindowLongW(hwnd, GWL_STYLE, style as i32);
            // On a raised desktop a child only composes if it is layered. Start
            // fully transparent; fade_in brings it up gently.
            let ex = GetWindowLongW(hwnd, GWL_EXSTYLE) as u32;
            SetWindowLongW(hwnd, GWL_EXSTYLE, (ex | WS_EX_LAYERED) as i32);
            SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA);
            SetParent(hwnd, parent);
            if d.raised {
                // Just below the icons; Windows' own wallpaper (WorkerW) at the very bottom.
                if !d.def_view.is_null() {
                    SetWindowPos(
                        hwnd,
                        d.def_view,
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                    );
                }
                if !d.worker.is_null()
                    && GetWindow(GetWindow(d.progman, GW_CHILD), GW_HWNDLAST) != d.worker
                {
                    SetWindowPos(
                        d.worker,
                        HWND_BOTTOM,
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                    );
                }
            }
            SetWindowPos(
                hwnd,
                std::ptr::null_mut(),
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            );
            place(hwnd, parent);
            if GetParent(hwnd) == parent {
                Ok(if d.raised { "raised" } else { "classic" })
            } else {
                Err("set_parent_failed".into())
            }
        }
    }

    pub fn fade_in(raw: isize, ms: u64) {
        let hwnd = raw as HWND;
        let steps = 18u64;
        for i in 1..=steps {
            let alpha = (255 * i / steps) as u8;
            unsafe { SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA) };
            std::thread::sleep(std::time::Duration::from_millis(ms / steps));
        }
    }

    pub fn refresh_desktop() {
        let d = find();
        let target = parent_for(&d);
        if !target.is_null() {
            unsafe {
                RedrawWindow(
                    target,
                    std::ptr::null(),
                    std::ptr::null_mut(),
                    RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW,
                );
            }
        }
    }

    fn fullscreen() -> bool {
        let mut state = 0;
        if unsafe { SHQueryUserNotificationState(&mut state) } != 0 {
            return false;
        }
        state == QUNS_BUSY
            || state == QUNS_RUNNING_D3D_FULL_SCREEN
            || state == QUNS_PRESENTATION_MODE
    }

    unsafe extern "system" fn collect_monitor(
        mon: HMONITOR,
        _hdc: HDC,
        _rc: *mut RECT,
        data: LPARAM,
    ) -> BOOL {
        let works = &mut *(data as *mut Vec<(i32, i32, i32, i32)>);
        let mut info: MONITORINFO = std::mem::zeroed();
        info.cbSize = std::mem::size_of::<MONITORINFO>() as u32;
        if GetMonitorInfoW(mon, &mut info) != 0 {
            let w = info.rcWork;
            works.push((w.left, w.top, w.right, w.bottom));
        }
        1
    }

    struct Cover {
        works: Vec<(i32, i32, i32, i32)>,
        hidden: Vec<bool>,
    }

    unsafe extern "system" fn check_cover(h: HWND, data: LPARAM) -> BOOL {
        let c = &mut *(data as *mut Cover);
        if IsWindowVisible(h) == 0 || IsIconic(h) != 0 {
            return 1;
        }
        let ex = GetWindowLongW(h, GWL_EXSTYLE) as u32;
        if ex & (WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE) != 0 {
            return 1;
        }
        let mut cloaked: u32 = 0;
        if DwmGetWindowAttribute(
            h,
            DWMWA_CLOAKED as u32,
            (&mut cloaked as *mut u32).cast(),
            4,
        ) == 0
            && cloaked != 0
        {
            return 1;
        }
        let class = class_of(h);
        if [
            "Progman",
            "WorkerW",
            "Shell_TrayWnd",
            "Shell_SecondaryTrayWnd",
        ]
        .contains(&class.as_str())
        {
            return 1;
        }
        let mut r: RECT = std::mem::zeroed();
        if GetWindowRect(h, &mut r) == 0 {
            return 1;
        }
        for (i, work) in c.works.iter().enumerate() {
            if !c.hidden[i] && hides((r.left, r.top, r.right, r.bottom), *work) {
                c.hidden[i] = true;
            }
        }
        if c.hidden.iter().all(|x| *x) {
            0
        } else {
            1
        }
    }

    /// Every screen's desktop is behind a big window — the wallpaper can rest.
    fn covered() -> bool {
        let mut works: Vec<(i32, i32, i32, i32)> = Vec::new();
        unsafe {
            EnumDisplayMonitors(
                std::ptr::null_mut(),
                std::ptr::null(),
                Some(collect_monitor),
                &mut works as *mut _ as LPARAM,
            );
        }
        if works.is_empty() {
            return false;
        }
        let mut c = Cover {
            hidden: vec![false; works.len()],
            works,
        };
        unsafe { EnumWindows(Some(check_cover), &mut c as *mut _ as LPARAM) };
        c.hidden.iter().all(|x| *x)
    }

    pub fn screen_state() -> ScreenState {
        let (on_battery, _) = crate::awareness_sensor::os::battery();
        ScreenState {
            fullscreen: fullscreen(),
            covered: covered(),
            locked: crate::awareness_sensor::os::locked(),
            on_battery: on_battery.unwrap_or(false),
        }
    }
}

#[cfg(not(target_os = "windows"))]
mod os {
    use super::ScreenState;
    pub fn screen_state() -> ScreenState {
        ScreenState::default()
    }
    pub fn refresh_desktop() {}
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn overlay_sizes_match_stonic() {
        assert_eq!(overlay_box("s"), (412, 112 + 150 + 34 + 52 + 128));
        assert_eq!(overlay_box("m"), (412, 112 + 210 + 34 + 52 + 128));
        assert_eq!(overlay_box("l"), (412, 112 + 280 + 34 + 52 + 128));
        assert_eq!(overlay_box("x"), overlay_box("m"));
    }

    #[test]
    fn top_centre_stays_inside_the_work_area() {
        assert_eq!(top_centre((0, 0, 1920, 1040), (412, 536)), (754, 0));
        // Taskbar on top: the work area starts lower.
        assert_eq!(top_centre((0, 48, 1920, 1032), (412, 536)), (754, 48));
        // A second monitor to the left.
        assert_eq!(top_centre((-1280, 0, 1280, 1000), (412, 536)), (-846, 0));
        // A work area smaller than the window.
        assert_eq!(top_centre((0, 0, 300, 300), (412, 536)), (0, 0));
    }

    #[test]
    fn hit_test_accounts_for_position_and_scale() {
        let r = HitRect {
            x: 88.0,
            y: 112.0,
            w: 236.0,
            h: 38.0,
        };
        // Window at (754, 0), 150 % scaling: CSS (100, 120) = physical (904, 180).
        assert!(cursor_in((904.0, 180.0), (754.0, 0.0), 1.5, &r));
        assert!(!cursor_in((754.0, 0.0), (754.0, 0.0), 1.5, &r));
    }

    #[test]
    fn prefs_are_sanitised() {
        assert_eq!(prefs_from(""), Prefs::default());
        let p = prefs_from(r#"{"enabled":true,"size":"xl","wallpaper":"yes"}"#);
        assert!(p.enabled && p.size == "m" && !p.wallpaper);
        assert!(!Prefs::default().enabled && !Prefs::default().wallpaper);
    }

    #[test]
    fn flag_defaults_off() {
        assert!(!flag_in(""));
        assert!(flag_in("[stonic]\ndesktop_presence = true\n"));
        assert!(!flag_in("[stonic]\nawareness = true\n"));
    }

    #[test]
    fn cover_needs_ninety_percent() {
        let work = (0, 0, 1000, 1000);
        assert!(hides((-8, -8, 1008, 1008), work)); // maximised
        assert!(!hides((0, 0, 1000, 800), work)); // 80 %
        assert!(!hides((2000, 0, 3000, 1000), work)); // other monitor
    }

    #[test]
    fn overlay_shows_only_when_wanted() {
        let mut st = State {
            prefs: Prefs {
                enabled: true,
                ..Prefs::default()
            },
            main_minimized: true,
            ..State::default()
        };
        assert!(should_show(&st, true));
        assert!(!should_show(&st, false)); // flag off
        st.fullscreen = true;
        assert!(!should_show(&st, true));
        st.fullscreen = false;
        st.locked = true;
        assert!(!should_show(&st, true));
        st.locked = false;
        st.main_minimized = false;
        assert!(!should_show(&st, true));
    }
}
