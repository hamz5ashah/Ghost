// Desktop entry point, only used for `tauri dev` previews of the mobile UI.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    ghost_mobile_lib::run()
}
