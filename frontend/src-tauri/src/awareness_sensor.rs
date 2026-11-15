//! Awareness sensor (Stonic port, Phase 5): what is happening on the PC,
//! read natively and posted to the backend's awareness eyes.
//!
//! Stonic read these from Electron's `powerMonitor` plus a PowerShell/C#
//! watcher. Ghost reads them directly through Win32 — no PowerShell, nothing
//! for an antivirus to flag, and it keeps running while the window is
//! minimised (a WebView's timers are throttled then; this thread is not).
//!
//! Every two seconds, only while `[stonic].awareness` is on:
//!
//! * idle seconds (`GetLastInputInfo`), Windows lock (input desktop check),
//!   battery (`GetSystemPowerStatus`);
//! * the front window's app and title, and whether it is Ghost itself;
//! * every third pass: which apps hold the microphone (the ConsentStore
//!   registry keys Windows keeps for the privacy indicator) and whether an app
//!   keeps the display awake (video, presentation);
//! * a wall-clock gap of 30 s+ between passes means the PC slept: the next
//!   sample says for how long.
//!
//! The backend decides what any of it means (`ghost.daemon.awareness.eyes`).
//! Nothing here looks at the screen's pixels, the camera or the audio.

#![cfg_attr(not(target_os = "windows"), allow(dead_code))]

use serde::Serialize;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const INTERVAL: Duration = Duration::from_secs(2);
/// Wall-clock gap between two passes that means the PC was asleep.
pub const SLEEP_GAP_MS: u64 = 30_000;
/// Read the microphone and display state every this many passes.
pub const SLOW_EVERY: u32 = 3;

#[derive(Debug, Default, Clone, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Sample {
    pub idle_seconds: f64,
    pub locked: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub on_battery: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub battery_level: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub app: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub own: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mic: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub display: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub slept_ms: Option<u64>,
}

/// How long the PC slept, from two wall-clock readings one pass apart. Pure.
pub fn slept_between(prev_ms: u64, now_ms: u64, interval: Duration) -> Option<u64> {
    let gap = now_ms.saturating_sub(prev_ms);
    (gap > SLEEP_GAP_MS).then(|| gap.saturating_sub(interval.as_millis() as u64))
}

/// Name of a microphone user from its ConsentStore key. Pure.
///
/// Packaged apps are stored by package family name (`Microsoft.SkypeApp_kzf…`);
/// desktop apps under `NonPackaged` by path with `#` for `\`
/// (`C:#Program Files#Zoom#bin#Zoom.exe`).
pub fn mic_user_name(key: &str, non_packaged: bool) -> String {
    if non_packaged {
        key.rsplit('#').next().unwrap_or(key).to_string()
    } else {
        key.to_string()
    }
}

/// Battery from `GetSystemPowerStatus` fields. Pure.
///
/// `ac_line`: 0 = on battery, 1 = plugged in, 255 = unknown. `flag` 128 = no
/// battery. `percent` 255 = unknown.
pub fn battery_from(ac_line: u8, flag: u8, percent: u8) -> (Option<bool>, Option<f64>) {
    if flag == 128 || ac_line == 255 {
        return (None, None);
    }
    let level = (percent <= 100).then(|| percent as f64 / 100.0);
    (Some(ac_line == 0), level)
}

/// File stem of a process image path. Pure.
pub fn exe_stem(path: &str) -> String {
    let base = path.rsplit(['\\', '/']).next().unwrap_or(path);
    base.strip_suffix(".exe")
        .or_else(|| base.strip_suffix(".EXE"))
        .unwrap_or(base)
        .to_string()
}

pub fn wall_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[cfg(target_os = "windows")]
pub mod os {
    use super::{battery_from, exe_stem, mic_user_name, Sample};
    use windows_sys::Win32::Foundation::{CloseHandle, ERROR_SUCCESS, HWND};
    use windows_sys::Win32::System::Power::{
        CallNtPowerInformation, GetSystemPowerStatus, SystemExecutionState, ES_DISPLAY_REQUIRED,
        SYSTEM_POWER_STATUS,
    };
    use windows_sys::Win32::System::Registry::{
        RegCloseKey, RegEnumKeyExW, RegGetValueW, RegOpenKeyExW, HKEY, HKEY_CURRENT_USER, KEY_READ,
        RRF_RT_REG_QWORD,
    };
    use windows_sys::Win32::System::StationsAndDesktops::{
        CloseDesktop, OpenInputDesktop, SwitchDesktop, DESKTOP_SWITCHDESKTOP,
    };
    use windows_sys::Win32::System::SystemInformation::GetTickCount;
    use windows_sys::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32,
        PROCESS_QUERY_LIMITED_INFORMATION,
    };
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetLastInputInfo, LASTINPUTINFO};
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, GetWindowTextW, GetWindowThreadProcessId,
    };

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    pub fn idle_seconds() -> f64 {
        let mut info = LASTINPUTINFO {
            cbSize: std::mem::size_of::<LASTINPUTINFO>() as u32,
            dwTime: 0,
        };
        if unsafe { GetLastInputInfo(&mut info) } == 0 {
            return 0.0;
        }
        let now = unsafe { GetTickCount() };
        now.wrapping_sub(info.dwTime) as f64 / 1000.0
    }

    pub fn locked() -> bool {
        let desk = unsafe { OpenInputDesktop(0, 0, DESKTOP_SWITCHDESKTOP) };
        if desk.is_null() {
            return true;
        }
        let switchable = unsafe { SwitchDesktop(desk) } != 0;
        unsafe { CloseDesktop(desk) };
        !switchable
    }

    pub fn battery() -> (Option<bool>, Option<f64>) {
        let mut st: SYSTEM_POWER_STATUS = unsafe { std::mem::zeroed() };
        if unsafe { GetSystemPowerStatus(&mut st) } == 0 {
            return (None, None);
        }
        battery_from(st.ACLineStatus, st.BatteryFlag, st.BatteryLifePercent)
    }

    fn process_name(pid: u32) -> String {
        if pid == 0 {
            return String::new();
        }
        let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if handle.is_null() {
            return String::new();
        }
        let mut buf = [0u16; 1024];
        let mut len = buf.len() as u32;
        let ok = unsafe {
            QueryFullProcessImageNameW(handle, PROCESS_NAME_WIN32, buf.as_mut_ptr(), &mut len)
        };
        unsafe { CloseHandle(handle) };
        if ok == 0 {
            return String::new();
        }
        exe_stem(&String::from_utf16_lossy(&buf[..len as usize]))
    }

    /// Front window: (app, title, pid).
    pub fn foreground() -> (String, String, u32) {
        let hwnd: HWND = unsafe { GetForegroundWindow() };
        if hwnd.is_null() {
            return (String::new(), String::new(), 0);
        }
        let mut title = [0u16; 512];
        let n = unsafe { GetWindowTextW(hwnd, title.as_mut_ptr(), title.len() as i32) };
        let mut pid = 0u32;
        unsafe { GetWindowThreadProcessId(hwnd, &mut pid) };
        let text = String::from_utf16_lossy(&title[..n.max(0) as usize]);
        (process_name(pid), text, pid)
    }

    fn open_key(parent: HKEY, path: &str) -> Option<HKEY> {
        let mut key: HKEY = std::ptr::null_mut();
        let path = wide(path);
        let rc = unsafe { RegOpenKeyExW(parent, path.as_ptr(), 0, KEY_READ, &mut key) };
        (rc == ERROR_SUCCESS).then_some(key)
    }

    /// Subkeys whose `LastUsedTimeStop` is 0 — "in use right now".
    fn busy_under(root: HKEY, non_packaged: bool, out: &mut Vec<String>) {
        let mut i = 0u32;
        loop {
            let mut name = [0u16; 512];
            let mut len = name.len() as u32;
            let rc = unsafe {
                RegEnumKeyExW(
                    root,
                    i,
                    name.as_mut_ptr(),
                    &mut len,
                    std::ptr::null(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            };
            if rc != ERROR_SUCCESS {
                break;
            }
            i += 1;
            let key_name = String::from_utf16_lossy(&name[..len as usize]);
            if !non_packaged && key_name == "NonPackaged" {
                continue;
            }
            let sub = wide(&key_name);
            let value = wide("LastUsedTimeStop");
            let mut stop: u64 = 1;
            let mut size = std::mem::size_of::<u64>() as u32;
            let rc = unsafe {
                RegGetValueW(
                    root,
                    sub.as_ptr(),
                    value.as_ptr(),
                    RRF_RT_REG_QWORD,
                    std::ptr::null_mut(),
                    (&mut stop as *mut u64).cast(),
                    &mut size,
                )
            };
            if rc == ERROR_SUCCESS && stop == 0 {
                out.push(mic_user_name(&key_name, non_packaged));
            }
        }
    }

    pub fn mic_users() -> String {
        let path = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone";
        let mut busy = Vec::new();
        if let Some(root) = open_key(HKEY_CURRENT_USER, path) {
            busy_under(root, false, &mut busy);
            if let Some(np) = open_key(root, "NonPackaged") {
                busy_under(np, true, &mut busy);
                unsafe { RegCloseKey(np) };
            }
            unsafe { RegCloseKey(root) };
        }
        busy.join("|")
    }

    pub fn display_kept_awake() -> bool {
        let mut state: u32 = 0;
        let status = unsafe {
            CallNtPowerInformation(
                SystemExecutionState,
                std::ptr::null(),
                0,
                (&mut state as *mut u32).cast(),
                std::mem::size_of::<u32>() as u32,
            )
        };
        status == 0 && state & ES_DISPLAY_REQUIRED != 0
    }

    pub fn sample(slow: bool) -> Sample {
        let (on_battery, battery_level) = battery();
        let (app, title, pid) = foreground();
        let own = pid != 0 && pid == std::process::id();
        Sample {
            idle_seconds: idle_seconds(),
            locked: locked(),
            on_battery,
            battery_level,
            app: Some(app),
            title: Some(if own { String::new() } else { title }),
            own,
            mic: slow.then(mic_users),
            display: slow.then(display_kept_awake),
            slept_ms: None,
        }
    }
}

/// Run forever on the async runtime: sample, post, sleep. Returns at once on
/// platforms without a sensor.
pub async fn run(enabled: impl Fn() -> bool + Send + 'static, url: String) {
    if !cfg!(target_os = "windows") {
        return;
    }
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .no_proxy()
        .build()
    {
        Ok(c) => c,
        Err(_) => return,
    };
    let mut pass: u32 = 0;
    let mut last_wall = wall_ms();
    loop {
        tokio::time::sleep(INTERVAL).await;
        let now = wall_ms();
        let slept = slept_between(last_wall, now, INTERVAL);
        last_wall = now;
        if !enabled() {
            continue;
        }
        #[cfg(target_os = "windows")]
        {
            let slow = pass % SLOW_EVERY == 0;
            let mut sample = tokio::task::spawn_blocking(move || os::sample(slow))
                .await
                .unwrap_or_default();
            sample.slept_ms = slept;
            // The backend may be restarting; a missed sample is harmless.
            let _ = client.post(&url).json(&sample).send().await;
        }
        #[cfg(not(target_os = "windows"))]
        let _ = (&client, slept, &url);
        pass = pass.wrapping_add(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sleep_is_a_long_wall_clock_gap() {
        assert_eq!(slept_between(0, 2_000, INTERVAL), None);
        assert_eq!(slept_between(0, 30_000, INTERVAL), None);
        assert_eq!(slept_between(0, 600_000, INTERVAL), Some(598_000));
        assert_eq!(slept_between(10, 5, INTERVAL), None); // clock set back
    }

    #[test]
    fn mic_names() {
        assert_eq!(
            mic_user_name(r"C:#Program Files#Zoom#bin#Zoom.exe", true),
            "Zoom.exe"
        );
        assert_eq!(
            mic_user_name("MicrosoftTeams_8wekyb3d8bbwe", false),
            "MicrosoftTeams_8wekyb3d8bbwe"
        );
    }

    #[test]
    fn battery_states() {
        assert_eq!(battery_from(0, 1, 18), (Some(true), Some(0.18)));
        assert_eq!(battery_from(1, 8, 100), (Some(false), Some(1.0)));
        assert_eq!(battery_from(1, 128, 255), (None, None));
        assert_eq!(battery_from(0, 0, 255), (Some(true), None));
    }

    #[test]
    fn exe_stems() {
        assert_eq!(
            exe_stem(r"C:\Program Files\Microsoft VS Code\Code.exe"),
            "Code"
        );
        assert_eq!(exe_stem("/usr/bin/firefox"), "firefox");
    }

    #[test]
    fn sample_json_matches_the_backend() {
        let s = Sample {
            idle_seconds: 3.5,
            locked: false,
            on_battery: Some(true),
            battery_level: Some(0.5),
            app: Some("Code".into()),
            title: Some("main.py".into()),
            own: false,
            mic: Some("Zoom.exe".into()),
            display: Some(false),
            slept_ms: None,
        };
        let v = serde_json::to_value(&s).unwrap();
        assert_eq!(v["idleSeconds"], 3.5);
        assert_eq!(v["onBattery"], true);
        assert_eq!(v["batteryLevel"], 0.5);
        assert_eq!(v["mic"], "Zoom.exe");
        assert!(v.get("own").is_none());
        assert!(v.get("sleptMs").is_none());
    }
}
