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

## Phase 4 — biometric and gesture input

- [ ] `facelock-core.js` + `facelock-manager.js` → `src/ghost/security/facelock.py`
- [ ] Models to `<config>/models/facelock` (on-demand, like Kokoro)
- [ ] Blink liveness, PIN fallback, Tauri commands
- [ ] MediaPipe gesture recognition behind `gestures`

## Phase 5 — awareness and desktop presence

- [ ] `awareness-manager` / `-timeline` / `-focus-watch` → Rust + `src/ghost/daemon/`
- [ ] Idle / lock / sleep / battery / active-window / mic state
- [ ] Local diary + proactive speak decision
- [ ] `desktop-presence` — always-on-top notch overlay with click-through
- [ ] Living wallpaper layer (Windows only)

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
wake_word_name = "Ghost"

[stonic.voice_runtime]
auto_install = true
```

All flags default to `false` (`computer_overlay` defaults `true` but is inert
while `computer_use` is off). Flag names are defined in three places that must
stay in step: `StonicConfig` (Python), `_STONIC_FLAG_NAMES` (API),
`STONIC_FLAG_NAMES` (Rust) — a Rust test and a Python test assert the lists
match.
