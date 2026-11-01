//! Ghost for Android.
//!
//! The phone app is a thin native shell around the Ghost web interface
//! (built with `--mode mobile`). All AI work happens in Ghost on the user's
//! PC; the interface talks to it over the local network using the address
//! and access key from the desktop app's Settings → Phone access.

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running Ghost");
}
