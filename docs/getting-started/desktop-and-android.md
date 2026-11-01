# Desktop and Android apps

Ghost comes as two apps that work together:

- **Ghost Desktop** (Windows, also macOS/Linux) runs the AI on your computer.
- **Ghost for Android** is a remote for it: your phone sends messages to Ghost
  on your PC over your home Wi-Fi. The phone itself does not run any models.

## 1. Get the installers

Installers are built by the **Build apps** workflow in this repository:

1. On GitHub, open **Actions → Build apps → Run workflow**.
2. Pick what to build (default: Windows and Android) and start it. It takes
   roughly 20–40 minutes.
3. When it finishes, open **Releases**. The newest `build-N` release has
   `Ghost_<version>_x64-setup.exe` (Windows) and `Ghost-android.apk`.

Pushing a version tag such as `v1.2.0` builds every platform automatically.

## 2. Install Ghost Desktop (Windows)

1. Run the `.exe` installer. Windows SmartScreen may warn that the app is
   unrecognised because it is not code-signed: choose **More info → Run anyway**.
2. Ghost needs these on your PC. It tells you if one is missing:
   - [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python
     manager): in PowerShell run
     `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
   - [Rust](https://rustup.rs) (used once to build Ghost's fast extension)
3. Open Ghost and choose where models run: a local model (Ollama, included)
   or a cloud provider with your API key.

On first launch Ghost sets itself up in your user folder (`%USERPROFILE%\Ghost`).
This takes a few minutes the first time only.

## 3. Turn on Phone access

In Ghost Desktop open **Settings → Phone access** and switch it on. Ghost
restarts and shows:

- **PC address**, e.g. `http://192.168.1.20:8000`
- **Access key**, e.g. `ghost_sk_…`

If Windows asks whether Ghost (Python) may use the network, allow it on
**private networks**.

Only devices with the access key can use Ghost. To lock out a phone, use
**Make a new key**. Switching Phone access off makes Ghost reachable only from
the PC again.

## 4. Install Ghost on your phone

1. Copy `Ghost-android.apk` to your phone (or download it from the release on
   the phone) and open it. Allow installing apps from that source when asked.
2. Open Ghost, enter the **PC address** and **Access key**, and tap **Connect**.

Your phone must be on the same Wi-Fi as the PC, and the PC must be on with
Ghost open. To use Ghost away from home, put both devices on a private network
such as [Tailscale](https://tailscale.com) and use the PC's Tailscale address.

If an update refuses to install over the old app, uninstall the old Ghost app
first. Adding the optional signing secrets described at the top of
`.github/workflows/build-apps.yml` avoids this.

## Building locally instead

You can build on your own computer instead of GitHub Actions:

- **Desktop**: install Node.js 22, Rust and the
  [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/), then
  `cd frontend && npm ci && npx tauri build`.
- **Android**: also install Android Studio (SDK + NDK) and Java 17, then
  `cd mobile && npm ci && npm run android:init && npm run android:build`.
