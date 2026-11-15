# Stonic → Ghost port plan

Merging the recovered **Stonic AI 3.1.21** feature set into Ghost, making Ghost
the unified flagship app. Branch: `feat/stonic-port`.

Reference source is vendored read-only at [`vendor/stonic/`](vendor/stonic/) —
see [`vendor/stonic/VENDOR.md`](vendor/stonic/VENDOR.md). Stonic's repository was
deleted and never pushed, so that tree is the only surviving copy.

## Ground rules

1. **Zero regression.** Ghost's existing behaviour is the baseline below. Every
   ported subsystem sits behind a `[stonic]` flag that defaults to **off**.
2. **Additive voice.** Kokoro ONNX (Urdu + English) and faster-whisper stay the
   default engine and are never removed. The Stonic realtime pipeline is a
   *second, selectable* engine.
3. **Sidecars, not merged deps.** `uv.lock` stays clean on Python 3.12. The two
   Stonic engines install into their own venvs under `<config>/runtimes/` on
   first use, like Kokoro's 354 MB model download.
4. **No old server.** No gateway, no Supabase, no licensing, no plan checks.
   Keys are the user's own (Gemini / Claude / DeepSeek) via Ghost's existing
   keyring + Cloud-providers Settings tab.
5. **Preserve the logic behind Roman Urdu comments.** The recovered comments
   document real decisions; carry the reasoning into the ported code.

## Test baseline (untouched `main`, 15 Nov 2026)

| Suite | Command | Baseline |
|---|---|---|
| Python | `uv run pytest tests/ -n auto -m "not live and not cloud and not hub"` | **9,352 passed**, 4 failed, 82 skipped |
| Frontend | `npm test` (vitest) | **131 passed** |
| Desktop | `cargo test` (in `frontend/src-tauri`) | **60 passed** |

The 4 Python failures are pre-existing and need network access
(`test_connectors_router` ×2, `test_web_search` ×2). Lint baseline:
`ruff check src/ tests/` reports 25 pre-existing errors — do not let that number
grow.

Local setup notes: frontend needs **npm ≥ 11.19**; `cargo test` needs the
frontend built once (`npm run build:tauri`) plus GTK/WebKit dev packages on
Linux; Python needs `uv sync --extra dev --extra framework-comparison --extra
server --extra inference-cloud` then `uv run maturin develop --manifest-path
rust/crates/ghost-python/Cargo.toml`.

---

## Phase 0 — foundation ✅ complete

- [x] Branch `feat/stonic-port`
- [x] Vendor recovered Stonic source into `vendor/stonic/` (128 files) with
      `VENDOR.md` and media checksums
- [x] `StonicConfig` + `StonicRuntimeConfig` in `src/ghost/core/config.py`,
      registered in `GhostConfig` and in `load_config`'s section list
- [x] `GET /v1/stonic/flags` in `src/ghost/server/api_routes.py` (advisory —
      never raises, reports all-off on failure)
- [x] `frontend/src/lib/stonic-flags.ts` — typed flags, cached, all-off on
      failure, shared in-flight request
- [x] `stonic_flags` Tauri command + pure `stonic_flags_from_config` in
      `frontend/src-tauri/src/lib.rs`
- [x] `src/ghost/runtimes/` — sidecar venv manager (locate, status, on-demand
      install, staleness marker keyed to the requirements digest)
- [x] Sidecar requirements staged at `configs/runtimes/{voice,computer}-requirements.txt`
      (ships via the existing Tauri `configs/` resource mapping)
- [x] Tests: **+19 frontend**, **+8 Rust**, **+48 Python**; zero new lint errors

Phase 0 result: Python **9,400 passed** / 4 pre-existing failures · frontend
**150** · desktop **68**.

---

## Phase 1 — media, audio and character assets ✅ complete

- [x] UI sound effects (16 `.wav`, 8.8 MB) → `frontend/public/stonic/audio/`,
      behind `ui_sounds`
- [x] `frontend/src/lib/stonic-sounds.ts` — semantic cue manifest, lazy load
      (`preload="none"`), volume/mute, loop handling for the ambience bed;
      playback never throws on a blocked autoplay policy
- [x] `frontend/src/hooks/useStonicSounds.ts` — delegated click/hover at the
      document root, one call site in `App.tsx`. Registers **no listeners at
      all** while the flag is off. Per-element override via `data-sound="…"`
- [x] Videos (3 files, 12 MB) → `frontend/src-tauri/resources/stonic/video/`,
      mapped in `tauri.conf.json` as `stonic/video/`
- [x] `stonic_asset_path` + `stonic_helpers` Tauri commands, with an
      allow-listed asset kind and a pure `is_safe_asset_name` guard
- [x] Avatar image + earth day/night textures →
      `frontend/public/stonic/images/`
- [x] `male_jarvis-*` voice lines (14 files) → `frontend/public/stonic/voice/`;
      Stonic-specific screen and billing tour lines dropped per approval
- [x] Character TOMLs → `configs/characters/`; `src/ghost/speech/characters.py`
      loader; `GET /v1/stonic/characters` (flag-gated, dev-only hidden);
      `characterDelivery()` in `assistant-prompt.ts`
- [x] Voice delivery prompt → `configs/prompts/voice_delivery.md`
- [x] `elevate.exe` + `swap-helper.exe` → `frontend/src-tauri/resources/stonic/win32/`,
      mapped to `./` in the **CI Windows overlay** (not `tauri.conf.json`,
      which is shared with the macOS/Linux jobs where these binaries don't
      exist), landing beside the exe where `bundled_helper()` already looks
- [x] Tests: **+37 Python**, **+65 frontend**, **+5 Rust**

Phase 1 result: Python **9,437 passed** / 4 pre-existing failures · frontend
**215** · desktop **73** · lint still 25.

### Decisions taken in Phase 1

**Three instructions from Stonic's `prompt.md` were deliberately not ported**
(recorded at the top of `configs/prompts/voice_delivery.md`): never calling
itself an AI, never using the word "AI" about itself, and crediting the Stonic
AI team / "Inventor Usman" as its maker and owner. Ghost's own baseline prompt
says plainly that it is the user's personal AI assistant and forbids claiming
to be another branded assistant, so those lines would have contradicted it. The
valuable part — the bracket performance system — was ported in full. Tests
assert the omissions stay omitted.

**Bracket cues are inert until Phase 3.** They need a TTS engine that
understands them. Kokoro ONNX does not, so selecting a character changes
nothing audible until the realtime voice engine lands.

**Five of the 14 `male_jarvis-*` lines narrate Stonic's own screens** —
`billing`, `classic`, `new`, `lanes`, `floating`. All 14 are committed, but
those five describe features and a subscription model Ghost does not have, so
they need re-recording before any guided tour uses them.

**Installer cost: ~21 MB** (8.8 MB audio + 12 MB video + 600 KB images +
672 KB voice). The WAVs are lossless; re-encoding to Opus would recover ~8 MB
if that matters more than keeping the originals byte-exact.

## Phase 2 — computer use (Agent S3) + OCR fix — partly complete

### Done

- [x] `src/ghost/computer/protocol.py` — pure stdlib, imported by both Ghost
      and the sidecar. Per-provider engine params for OpenRouter, Claude,
      Gemini and DeepSeek with direct user keys only; grounding coordinate
      conventions; Tesseract locate + probe; sidecar env + fingerprint;
      provider-aware error messages (no gateway, plan or quota text)
- [x] `src/ghost/computer/hud.py` — the HUD state machine, ported from
      `computer-overlay.js`
- [x] `src/ghost/computer/sidecar/` — `runner.py`, `overlay_hud.py`,
      `agent_cursor.py` (was `stonic_overlay.py`), `esc_guard.py`. Gateway,
      `ROUTE` and plan handling removed; per-role providers; Gemini grounding
      in its own `[y, x]` 0–1000 convention; Claude calls made abortable and
      retried; OCR probed at boot and reported in `ready`
- [x] **Esc-stop armed by the runner itself**, every task. In Stonic only the
      HUD process armed it, so with the overlay off Esc stopped nothing
- [x] **OCR bug fix:** `GET /v1/computer/ocr/status` runs the binary
      (`--version`, `--list-langs`) so "available" means OCR works; Windows CI
      stages `tesseract.exe` + DLLs + `eng.traineddata` into
      `binaries/tesseract/` → installed as `tesseract/`; the staged copy is
      OCR-tested with `PATH` cut to `System32`; the installed app's
      `/v1/computer/ocr/status` must report `available` from source
      `bundled`; Rust forwards the bundled path as `GHOST_TESSERACT`
- [x] `[stonic]` config: `computer_planner`, `computer_grounding`,
      `computer_max_steps` (`computer_use` still defaults to **off**)
- [x] Tests: +127 Python (protocol, HUD, OCR route), +2 Rust

### Deviation from the original plan

The HUD is **not** routed through Ghost's `overlay.html` / `toggle_overlay`.
That overlay is a macOS-only native chat panel; on Windows both commands are
no-ops. Stonic's own notes also record that a webview overlay comes out black
in the agent's screenshots. The HUD stays Stonic's native layered window
(`overlay_hud.py`), which Windows excludes from capture.

### Remaining — two wiring pieces, to finish locally on Windows

These two pieces are all that's left. Everything they call already exists
and is tested.

**1. The supervisor process — `src/ghost/computer/manager.py`**

Port of `vendor/stonic/electron/services/computer-manager.js`. It owns one
resident runner process:

- **Spawn.** `ghost.runtimes.venv_python("computer")` runs
  `src/ghost/computer/sidecar/runner.py`, with args `-X utf8 -u`. Don't use
  `-I`: it ignores the `PYTHON*` env the runner relies on. The env comes from
  `protocol.sidecar_env(protocol.choose(os.environ, stonic.computer_planner,
  stonic.computer_grounding), protocol.locate_tesseract(...), os.environ)`.
  Keys come only from `os.environ`, which the Tauri shell fills from the OS
  keyring and `/v1/cloud/reload` updates.
- **Start condition.** Start only when `stonic.computer_use` is true and the
  runtime is installed. Restart when `protocol.fingerprint(...)` changes.
- **Protocol.** NDJSON on stdin/stdout, documented at the top of `runner.py`.
  Drain stderr on its own thread, so a full pipe can't hang the child.
- **Supervision.** Reuse Stonic's timing constants (top of
  `computer-manager.js`):
    - load stall 30 s and hard cap 15 min
    - start timeout 15 s
    - stop grace 12 s
    - task watchdog 22 min
    - restart backoff 3 s × [1, 3, 10, 20, 40], capped at 2 min
    - give up after 5 failures
- **Tasks.** One at a time; a second request gets "busy". If the agent is
  still loading, queue a single pending task.
- **HUD.** Spawn `overlay_hud.py`, Windows only, behind
  `stonic.computer_overlay`. Feed it `hud.reduce(...)` states. Hide once
  `hud.should_hide` is true. Forward its `{"event":"stop"}` to the runner.

**2. The task routes — `/v1/computer/*` on `computer_router` in
`src/ghost/server/api_routes.py`**

- `GET /v1/computer/status`: manager state, the chosen providers and models,
  and the last `ocr` from `ready`
- `POST /v1/computer/tasks` `{task, max_steps?, fast?}`: returns `{id}`, or
  409 when busy and 503 when disabled or unconfigured. Return
  `ProviderError.code` and its message as-is
- `POST /v1/computer/tasks/{id}/stop`
- `GET /v1/computer/tasks/{id}`: the result once `task_done` arrives
- `GET /v1/computer/events?since=N`: buffered runner events for the UI
- Construct the manager in `create_app` only when `stonic.computer_use` is
  true, and shut it down on app shutdown

Optional: a `computer_use` tool in `src/ghost/tools/`, with
`requires_confirmation=True` (as `shell_exec` has), that calls the manager.
Register it only when the flag is on.

## Phase 3 — realtime voice engine (talk-only) — complete

Ported as a **second, selectable engine**. Ghost's default voice path
(Kokoro ONNX speaking, faster-whisper listening) is unchanged and remains the
default; read-aloud and dictation always use it.

### Done

- [x] `src/ghost/voice_sidecar/` — Stonic's Pipecat engine, **talk-only**:
      `server.py` + `ghost_voice/` (`ears`, `turns`, `tts_gemini`, `pipeline`,
      `config`, `providers`, `errors`, `bridge`, `host`). Not a package (no
      `__init__.py`), run with the `voice` runtime's interpreter
- [x] **Agent path not ported:** no `claude-agent-sdk`/`claude.exe`, no
      tools, screen or `/work/*`, `/beta/tool-result`, `/beta/mouse`,
      `/beta/cue`, `/beta/interrupt` routes (all 404). Runtime venv 706 → 375 MB
- [x] **Gateway stripped:** `cloud.py` deleted, every `is_cloud` branch
      collapsed, Cloud/plan/limit error categories removed; `mode` other than
      `custom` refused by name. Providers: Claude, Gemini, DeepSeek (replaces
      Groq), OpenRouter
- [x] Silero VAD + Smart Turn v3 (`GhostTurnStopStrategy`), both verified to
      load in the real runtime (`warm` event)
- [x] `src/ghost/speech/realtime.py` — supervisor: spawns the engine,
      mints a per-launch token, loopback only, waits for `ready`, exits with
      Ghost (`GHOST_VOICE_PARENT_PID`), drains stdout
- [x] `/v1/speech/realtime/{status,install,start,stop,hangup,offer}` — all but
      `status`/`stop` refuse with 403 unless `realtime_voice` is on. `offer`
      proxies SmallWebRTC (POST + PATCH) so the browser only talks to Ghost
- [x] **Wake word** — `src/ghost/speech/wake_word.py` +
      `/v1/speech/realtime/wake/{start,stop,pause,events}`; needs both
      `realtime_voice` and `wake_word`; name from `stonic.wake_word_name`
      (default "Ghost"). Paused for the length of a realtime call
- [x] Settings → Assistant → **Conversation engine** (hidden unless the flag
      is on): Ghost voice (default) / Realtime, brain, voice mode, runtime
      install, saved-key presence, wake word toggle. Voice Mode opens the
      realtime call only when chosen **and** the flag is on
- [x] Persona "Jarvis" → Stonic's `male_jarvis` delivery profile, so the
      Phase 1 bracket cues are now audible (realtime engine, Expressive voice)
- [x] Tests: +42 Python (`test_realtime.py` against a fake engine speaking
      the real contract; `test_wake_word.py`), +10 frontend

### Deviations

- **Keys go over `/beta/config`, not the sidecar's environment.** The
  supervisor reads them from Ghost's own environment (filled from the OS
  keyring by the shell) and strips every `*_API_KEY` from the child's env, so
  they are never visible in the engine's process table. Only the keys a
  session needs are sent (Gemini + the chosen brain)
- **Wake word listens on-device.** Stonic sent every phrase to Google's free
  speech API (`recognize_google`); Ghost transcribes with its own
  faster-whisper. Stonic's name matching is kept (first name, ≥3 letters,
  slack on ≥4), its "anything ending in -nic" rule is dropped (it would wake on
  "picnic"/"clinic"), and Whisper punctuation is handled
- Gemini still does the realtime engine's listening and speaking (as in
  Stonic), so a Gemini key is required whichever brain thinks

### Verified

- Real runtime (Python 3.11, pipecat 1.12.0): engine boots in ~3 s, 17/17
  HTTP-surface checks, all 4 brains × both voice modes construct, prompt is
  Ghost-branded and says it is an AI assistant
- End to end: aiortc client → Ghost's `/v1/speech/realtime/offer` proxy →
  real engine → **connected**, audio track back; with test keys the engine
  reached Gemini and Anthropic directly and returned
  `gemini_key_invalid`/`anthropic_key_invalid` with no key text in any message
- Default path: Kokoro ONNX spoke the CI Urdu sentence (`hm_omega`, 4.0 s);
  `uv.lock` untouched; full suites show no new failures (Python 24 failing =
  pre-existing baseline; frontend 225/225); ruff unchanged at 25

### Remaining

- [ ] Gate: the CI Urdu voice round-trip (Kokoro → Whisper "small") on the
      Windows job — the Whisper half needs Hugging Face, unreachable from the
      workspace this was built in, so it is confirmed by the push build
- [ ] A live call with real keys on a Windows machine

## Phase 4 — biometric and gesture input — complete

Both features are off unless `[stonic].facelock` / `[stonic].gestures` is on.

### Done

- [x] `src/ghost/security/facelock.py` — `facelock-core.js` + `facelock-manager.js`
      ported: cosine matching with Stonic's thresholds (0.5 match, 0.45 probe
      coherence, 0.3 template coherence, median of 3–8 probes), scrypt PIN
      (N=16384, r=8, p=1) with the same weak-PIN rules, 5 free attempts then
      30 s → 1 → 5 → 15 min lockouts, single-use 2-minute manage tickets,
      corrupt file = no lock. State sealed with **Windows DPAPI** in
      `<config>/face_lock.dpapi` (Windows-only, as in Stonic)
- [x] **Lock enforced in the backend.** `FaceLockGate` returns 423 for the
      desktop UI's (loopback) `/v1` + `/api` calls while locked, except the
      lock screen's own routes and `/v1/cloud/reload`. Remote clients (phone)
      use the API key and are unaffected. Sits inside Auth + CORS
- [x] `SessionWatcher` re-locks on Windows lock (input desktop check) and on
      sleep (a 2 s poll that wakes 30 s+ late). Locking pauses the wake word
- [x] `/v1/security/facelock/{status,verify-face,verify-pin,enroll,change-pin,disable,lock}`
      and `/models` + `/models/download`; templates and PIN hash never returned
- [x] `src/ghost/security/facelock_models.py` — on-demand download to
      `<config>/models/facelock` (Kokoro pattern), every file pinned by size +
      SHA-256 with mirror fallback; re-hashed before first serve. Served
      read-only from `/facelock-assets/` (fixed allow-list, only while a flag
      is on)
- [x] Frontend: camera engine (MediaPipe face mesh + SFace via ONNX Runtime
      Web), blink liveness detector, unlock and enrollment flows, framing
      checks — all recovered from Stonic's built renderer (deobfuscated) with
      its constants unchanged; lock screen (rest of the page `inert`), PIN
      pad fallback, Settings → Security & input
- [x] Hand control: Stonic's gesture controller (two fingers move, pinch
      left/right click, three fingers scroll, fist drag) + Tauri commands
      (`hand_*`, `SendInput`/`SetCursorPos`) refused unless `gestures` is on;
      stops on Esc, on lock, and when the hand leaves the frame
- [x] Tests: +50 Python, +39 frontend, +3 Rust, +1 CSP policy test

### Deviations

- **Pinch clicks once per pinch.** Stonic re-clicked every 400 ms while a
  pinch was held (a slow pinch became a double-click)
- **CSP gains `'wasm-unsafe-eval'`** (WebAssembly compilation only; JS eval
  still refused, scripts still `'self'` only — test-pinned). MediaPipe's
  320 KB loader script ships with the app for that reason; the ~68 MB of
  WASM and models stay on-demand, and ORT's default 14 MB `.wasm` that Vite
  would copy in is dropped from the build
- Runtimes pinned to the exact builds Stonic shipped:
  `@mediapipe/tasks-vision@0.10.35` (wasm hash match) and
  `onnxruntime-web@1.30.0`; `sface.onnx` is byte-identical to OpenCV Zoo's
  `face_recognition_sface_2021dec.onnx`
- `protobufjs` (via onnxruntime-web) added to the npm `allowScripts` deny
  list; its postinstall is CLI tooling only

### Verified

- Headless Chrome under the **exact desktop CSP**: WASM compiles, JS eval
  blocked, all models load through `/facelock-assets` (hash-checked), the face
  mesh runs, SFace returns a unit 128-d print, the gesture recognizer runs
- Real app stack: enroll → lock → `/v1/models` 423 (with CORS headers, auth
  still first) → face unlock → 200
- Full suites: no new failures (Python 24 = pre-existing baseline), frontend
  262/262, Rust 78/78, ruff 25 (unchanged), format check unchanged

### Remaining

- [ ] On Windows: a real enroll + unlock with a webcam, DPAPI round-trip,
      Windows-lock re-lock, and hand control moving the real cursor — none of
      these can run in the Linux workspace this was built in
- [ ] The two MediaPipe `.task` hashes are pinned to Stonic's recovered
      copies (sizes match Google's published files); Google's model bucket
      was unreachable from the build workspace, so the first real download
      confirms the pins
- [ ] The Windows `SendInput` code was checked against the `windows-sys`
      0.59 signatures but not compiled for Windows here (the Windows Rust
      target could not be downloaded); the Windows build confirms it

## Phase 5 — awareness and desktop presence — complete

Flags: `[stonic].awareness` and `[stonic].desktop_presence`, both off. Each
also needs the user's own switch in Settings → Security & input (awareness is
double opt-in: flag + `<config>/awareness/settings.json`; presence prefs live
in `~/.ghost/desktop-presence.json`, `enabled: false` by default).

### Done

- [x] **Eyes** — `src/ghost/daemon/awareness/{timeline,eyes,moments}.py`,
      ported from `awareness-manager.js` / `awareness-timeline.js` and the
      moment detector recovered from Stonic's renderer bundle: away / back,
      Windows lock and sleep, battery, front app and title, mic-in-call,
      "screen kept awake" (video), the day ledger (5 AM day cut), a rolling
      diary at `<config>/awareness/diary.json` (stale diary deleted after 18 h
      off), and the moments: call ended, video ended, stretch, late night,
      restless switching
- [x] **Sensor** — `frontend/src-tauri/src/awareness_sensor.rs` replaces
      Stonic's PowerShell/C# `awareness-focus-watch`: native Win32 every 2 s
      (`GetLastInputInfo`, input-desktop lock check, `GetSystemPowerStatus`,
      foreground window/process, ConsentStore mic users, `ES_DISPLAY_REQUIRED`,
      a 30 s wall-clock gap = slept), posted to `POST /v1/awareness/signals`.
      It runs in the shell, not the WebView, so minimising Ghost doesn't
      throttle it, and it sends nothing unless the flag is on
- [x] **Brain** — `brain.py` + `engine.py`: Stonic's decision prompt and
      parser, and the whole engine (`TFe`): pending triggers with settle
      times, hello / heartbeat / follow-ups, the call / rest / cap /
      in-conversation / away / unchanged-screen gates, the token bucket and
      adaptive gap (answered ×0.7, ignored ×1.4, dismissed ×2, 4–60 min),
      300 thoughts / 30 spoken lines a day, all tunables as recovered.
      Thinks with Ghost's own engine and model (`make_think`); state in
      `state.json` / `thoughts.json` / `journal.json`
- [x] **API** — `/v1/awareness/*`: status, configure, signals (write-only,
      204, allowed through the Face Lock gate), activity, outcome, quiet,
      think-now, events, diary, forget; everything but status/signals is 403
      while the flag is off
- [x] **Voice** — `useAwareness` (main window): polls events, speaks a line
      with Ghost's own TTS (never during voice mode, a streaming reply or
      other speech; stale lines from before launch are dropped), reports
      answered / dismissed (ignored settles by itself after 2 min), and sends
      the conversation state every 10 s so the brain never talks over you
- [x] **Desktop presence** — `frontend/src-tauri/src/presence.rs` +
      `components/presence/PresenceOverlay.tsx`: Stonic's notch (236×38 pill
      → 376-wide panel with the character, caption, talk / quiet / open /
      more; size S/M/L, "Leave the desktop"; thought bubble for 3 min).
      Transparent, always on top, no taskbar entry, never focusable, top
      centre of the work area of the screen Ghost was on. Click-through
      everywhere except the hit rect the page reports (the shell polls the
      cursor, since Tauri has no "ignore but forward" mode). Shows on
      minimise, hides on restore, full-screen apps
      (`SHQueryUserNotificationState`) and Windows lock
- [x] **Living wallpaper** (Windows) — Stonic's WorkerW attach ported to
      Rust (Progman `0x052C`, raised 24H2+ desktop vs classic WorkerW,
      layered child under `SHELLDLL_DefView`, spans the virtual screen,
      fades in), "covered" detection (every monitor's work area ≥90 % under a
      visible window → rest), re-attach after Explorer restarts, switches
      itself off with a reason if attach fails. The page
      (`LivingWallpaper.tsx` + `lib/wallpaper-gl.ts`) uses the recovered
      shaders verbatim: quarter-res fbm nebula, dither blit, 3800 stars +
      2200 orbiting motes (seed 334462), 20/30 fps, 12/24 on battery, night
      dimming 20:00–07:00, flare when a conversation starts
- [x] Asset protocol enabled, scoped to `$RESOURCE/stonic/video/*` only, with
      `asset:` / `http://asset.localhost` added to CSP `media-src`, for the
      optional "Light figure" avatar (`light-female-1.mp4`)
- [x] New windows get their own capability (`capabilities/presence.json`):
      events only — no shell, dialog, updater or file permissions

### Deviations

- **No screen or camera looks.** Stonic's brain could ask to look at the
  screen or camera; Ghost's schema offers only `silent` / `speak`, and a
  `look` answer from the model is treated as silent
- **No memory or promises.** Stonic's awareness wrote to its memory store and
  tracked promises; that waits for Phase 6. The engine keeps only its own
  short journal
- **Awareness context is not injected into chats.** Stonic added the diary to
  ordinary chat prompts; Ghost keeps it to the awareness brain
- **Simpler avatar.** Stonic's particle-from-video character is replaced by
  Ghost's orb (default) or a masked `<video>` of the light figure using the
  same per-phase tempo table; the 5 tour clips stay unwired
- "Mute" on the notch means *quiet for an hour* (stop talking, awareness
  rests), not mic mute — voice mode owns the mic
- Stonic's computer-use "ghost" mode (overlay steps aside while an agent
  drives the screen) is not wired; computer use is still opt-in and its
  Windows wiring is Phase 2's remaining item

### Verified

- Python: 59 new awareness tests (eyes, moments, prompt, parser, engine
  gates and pacing, routes, flag gating); full suite 24 failed = the
  pre-existing baseline (re-checked at HEAD for the `server/` ones)
- Rust: 90/90 (12 new: sample JSON matches the backend, sleep gap, mic key
  names, battery decoding, overlay sizes, top-centre placement on
  multi-monitor / top taskbar, hit-testing at 150 % scaling, prefs
  sanitising, flag default off, 90 % cover rule, show/hide conditions)
- Frontend: 275/275 (13 new), `tsc -b` clean, production build OK
- ruff 25 (unchanged), formatting unchanged

### Remaining

- [ ] On Windows: the sensor's real readings (lock, mic in a Zoom/Teams call,
      video keeping the display awake, sleep), the notch over a minimised
      Ghost (click-through, hover, full-screen hide, DPI), and the wallpaper
      attach on both a classic and a 24H2 "raised" desktop
- [ ] Like Phase 4, the Win32 code was checked against the `windows-sys`
      0.59 signatures but not compiled for Windows here (the Windows Rust
      target could not be downloaded); the Windows build confirms it

## Phase 6 — memory merge ⚠ highest risk

Ghost's learning loop (81 modules) and eval harness (147 modules) both read
`src/ghost/memory/`. Stonic's schema is richer (≈6,600 lines vs 978) and
different. This is a **migration behind `memory_v2`**, not a replacement, and it
goes last.

- [ ] Port `memory/*` (16 modules) and `file-memory/*` (5 modules)
- [ ] Keep Ghost's `service.py` interface intact
- [ ] Forward migration with a rollback path
- [ ] Gate: learning + evals suites still pass

---

## Not being ported

**Ghost already covers these — 11 modules, ~3,900 lines left alone:**

| Stonic | Ghost equivalent |
|---|---|
| `staged-updater` (1,783) + `macos-updater` (356) | `tauri-plugin-updater` |
| `telemetry` (366) | `/v1/telemetry/energy`, `/v1/savings` |
| `web-search` (355) | `tools/web_search.py` |
| `shell-runner` (411) | `tools/shell_exec.py`, `docker_shell_exec.py` |
| `key-store` (99) + `memory/secrets` (846) | keyring via `secure_store_*` |
| `chat-store` (211) | Ghost chat store |
| `static-server` (180) | FastAPI |
| `python-runtime` (67) | `prepare_backend_python` |
| `frame-guard` (61) | Tauri CSP |

**Dropped — Stonic business logic:** `account-profile`, `login-result-page`,
`found-us-answer`, `skills/library` (Stonic's own catalogue; Ghost has an
agentskills.io loader), Supabase sync, Polar licensing, the Netlify
`track-app-open` ping.

**Unrecoverable, needs rewriting if ever wanted:** Stonic's Electron `main.js`
(shipped as V8 bytecode) and its React renderer (minified, no sourcemaps). See
`vendor/stonic/reconstruction/` for the extracted symbol and string maps.

## Turning features on

Flags live in `~/.ghost/config.toml`:

```toml
[stonic]
computer_use = true
realtime_voice = true
awareness = true
desktop_presence = true
wake_word_name = "Ghost"

[stonic.voice_runtime]
auto_install = true
```

All flags default to `false` (`computer_overlay` defaults `true` but is inert
while `computer_use` is off). Flag names are defined in three places that must
stay in step: `StonicConfig` (Python), `_STONIC_FLAG_NAMES` (API),
`STONIC_FLAG_NAMES` (Rust) — a Rust test and a Python test assert the lists
match.
