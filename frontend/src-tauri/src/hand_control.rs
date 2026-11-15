//! Hand control (Stonic port, Phase 4): the user's own hand, seen by the
//! webcam, moves the cursor, clicks, scrolls and drags.
//!
//! The WebView recognises the hand (MediaPipe) and decides the gesture; these
//! commands only carry out the resulting pointer input, the way Stonic's
//! `desktop-manager.js` did. Every command is refused unless
//! `[stonic].gestures` is on, and every value is validated and clamped to the
//! screen. Windows-only, like Stonic's gesture control.

use std::sync::Mutex;
use std::time::{Duration, Instant};

/// How long a read of the flag is trusted. Pointer moves arrive ~30 times a
/// second; re-reading config.toml for each would be wasteful, but turning the
/// flag off must still take effect promptly.
const FLAG_TTL: Duration = Duration::from_secs(2);

static FLAG_CACHE: Mutex<Option<(Instant, bool)>> = Mutex::new(None);

/// `[stonic].gestures` from a config.toml string. Pure.
pub fn gestures_enabled_in(config_text: &str) -> bool {
    crate::stonic_flags_from_config(config_text)
        .get("gestures")
        .copied()
        .unwrap_or(false)
}

fn gestures_enabled() -> bool {
    let mut cache = FLAG_CACHE.lock().unwrap_or_else(|e| e.into_inner());
    if let Some((at, on)) = *cache {
        if at.elapsed() < FLAG_TTL {
            return on;
        }
    }
    let text = std::fs::read_to_string(crate::ghost_config_toml_path()).unwrap_or_default();
    let on = gestures_enabled_in(&text);
    *cache = Some((Instant::now(), on));
    on
}

fn guard() -> Result<(), String> {
    if gestures_enabled() {
        Ok(())
    } else {
        Err("disabled".into())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Button {
    Left,
    Right,
}

pub fn parse_button(name: &str) -> Option<Button> {
    match name {
        "left" => Some(Button::Left),
        "right" => Some(Button::Right),
        _ => None,
    }
}

/// A target point, rounded and clamped onto a `w`×`h` screen. Pure.
pub fn clamp_point(x: f64, y: f64, w: i32, h: i32) -> Option<(i32, i32)> {
    if !x.is_finite() || !y.is_finite() || w <= 0 || h <= 0 {
        return None;
    }
    let cx = x.round().clamp(0.0, (w - 1) as f64) as i32;
    let cy = y.round().clamp(0.0, (h - 1) as f64) as i32;
    Some((cx, cy))
}

/// Wheel delta for Stonic's scroll amounts (10 ≈ one notch, max 80). Pure.
pub fn wheel_delta(direction: &str, amount: u32) -> Option<i32> {
    let notch = 120 * amount.clamp(1, 80) as i32 / 10;
    match direction {
        "up" => Some(notch),
        "down" => Some(-notch),
        _ => None,
    }
}

#[cfg(target_os = "windows")]
mod os {
    use super::Button;
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
        SendInput, INPUT, INPUT_0, INPUT_MOUSE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP,
        MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_WHEEL, MOUSEINPUT,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetSystemMetrics, SetCursorPos, SM_CXSCREEN, SM_CYSCREEN,
    };

    pub fn screen() -> (i32, i32) {
        unsafe { (GetSystemMetrics(SM_CXSCREEN), GetSystemMetrics(SM_CYSCREEN)) }
    }

    pub fn move_to(x: i32, y: i32) -> Result<(), String> {
        if unsafe { SetCursorPos(x, y) } == 0 {
            return Err("cursor move refused".into());
        }
        Ok(())
    }

    fn send(flags: u32, data: i32) -> Result<(), String> {
        let input = INPUT {
            r#type: INPUT_MOUSE,
            Anonymous: INPUT_0 {
                mi: MOUSEINPUT {
                    dx: 0,
                    dy: 0,
                    mouseData: data as _,
                    dwFlags: flags,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        };
        let sent = unsafe { SendInput(1, &input, std::mem::size_of::<INPUT>() as i32) };
        if sent == 1 {
            Ok(())
        } else {
            Err("input refused".into())
        }
    }

    pub fn button(button: Button, down: bool) -> Result<(), String> {
        let flags = match (button, down) {
            (Button::Left, true) => MOUSEEVENTF_LEFTDOWN,
            (Button::Left, false) => MOUSEEVENTF_LEFTUP,
            (Button::Right, true) => MOUSEEVENTF_RIGHTDOWN,
            (Button::Right, false) => MOUSEEVENTF_RIGHTUP,
        };
        send(flags, 0)
    }

    pub fn wheel(delta: i32) -> Result<(), String> {
        send(MOUSEEVENTF_WHEEL, delta)
    }
}

#[cfg(not(target_os = "windows"))]
mod os {
    use super::Button;
    const NO: &str = "unsupported";
    pub fn screen() -> (i32, i32) {
        (0, 0)
    }
    pub fn move_to(_x: i32, _y: i32) -> Result<(), String> {
        Err(NO.into())
    }
    pub fn button(_b: Button, _down: bool) -> Result<(), String> {
        Err(NO.into())
    }
    pub fn wheel(_delta: i32) -> Result<(), String> {
        Err(NO.into())
    }
}

#[derive(serde::Serialize)]
pub struct ScreenSize {
    pub supported: bool,
    pub width: i32,
    pub height: i32,
}

#[tauri::command]
pub fn hand_screen_size() -> Result<ScreenSize, String> {
    guard()?;
    let (width, height) = os::screen();
    Ok(ScreenSize {
        supported: cfg!(target_os = "windows") && width > 0 && height > 0,
        width,
        height,
    })
}

#[tauri::command]
pub fn hand_mouse_move(x: f64, y: f64) -> Result<(), String> {
    guard()?;
    let (w, h) = os::screen();
    let (cx, cy) = clamp_point(x, y, w, h).ok_or("bad point")?;
    os::move_to(cx, cy)
}

#[tauri::command]
pub fn hand_mouse_button(button: String, down: bool) -> Result<(), String> {
    guard()?;
    os::button(parse_button(&button).ok_or("bad button")?, down)
}

#[tauri::command]
pub fn hand_mouse_click(button: String) -> Result<(), String> {
    guard()?;
    let b = parse_button(&button).ok_or("bad button")?;
    os::button(b, true)?;
    os::button(b, false)
}

#[tauri::command]
pub fn hand_scroll(direction: String, amount: u32) -> Result<(), String> {
    guard()?;
    os::wheel(wheel_delta(&direction, amount).ok_or("bad direction")?)
}

/// Let go of both buttons (used when hand control stops mid-drag). Allowed
/// even with the flag off, so turning the flag off can never leave a button
/// held down.
#[tauri::command]
pub fn hand_release() -> Result<(), String> {
    if cfg!(target_os = "windows") {
        let _ = os::button(Button::Left, false);
        let _ = os::button(Button::Right, false);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flag_defaults_off_and_reads_true_only() {
        assert!(!gestures_enabled_in(""));
        assert!(!gestures_enabled_in("[stonic]\ngestures = \"yes\"\n"));
        assert!(!gestures_enabled_in("[stonic]\nfacelock = true\n"));
        assert!(gestures_enabled_in("[stonic]\ngestures = true\n"));
    }

    #[test]
    fn points_are_clamped_to_the_screen() {
        assert_eq!(clamp_point(-50.0, 99999.0, 1920, 1080), Some((0, 1079)));
        assert_eq!(clamp_point(100.4, 200.6, 1920, 1080), Some((100, 201)));
        assert_eq!(clamp_point(f64::NAN, 1.0, 1920, 1080), None);
        assert_eq!(clamp_point(1.0, 1.0, 0, 1080), None);
    }

    #[test]
    fn buttons_and_wheel_are_validated() {
        assert_eq!(parse_button("left"), Some(Button::Left));
        assert_eq!(parse_button("middle"), None);
        assert_eq!(wheel_delta("up", 10), Some(120));
        assert_eq!(wheel_delta("down", 10), Some(-120));
        assert_eq!(wheel_delta("down", 5000), Some(-960));
        assert_eq!(wheel_delta("sideways", 10), None);
    }
}
