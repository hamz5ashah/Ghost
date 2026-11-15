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

## Phase 2 — computer use (Agent S3) + OCR fix

- [ ] `stonic-computer/` → `voice`/`computer` sidecar wiring via
      `src/ghost/runtimes/`
- [ ] `src/ghost/tools/computer_use.py` exposing it as a Ghost tool
- [ ] Supervisor with backoff restart (port `computer-manager.js`, 1,031 lines)
- [ ] Overlay HUD onto the existing `frontend/src-tauri/src/overlay.html` and
      `toggle_overlay` / `hide_overlay` commands
- [ ] **OCR bug fix:** bundle `tesseract` + `eng.traineddata`, set
      `pytesseract.tesseract_cmd` from the resolved resource path, honour
      `stonic.tesseract_path` / `tessdata_path`
- [ ] `GET /v1/computer/ocr/status` so Settings can show green/red
- [ ] Startup probe fails loudly instead of degrading silently

## Phase 3 — realtime voice engine

- [ ] `stonic-voice/` → sidecar, registered alongside `kokoro_onnx_tts.py`
- [ ] **Strip the gateway:** delete `cloud.py`, the `Cloud` config class,
      `allow_plain_gateway`, `listen_http_options`, `describe_denial`; collapse
      every `if settings.is_cloud:` branch (6 in `pipeline.py`, plus
      `agent.py`'s `cloud_env`); reduce `MODES` to `("custom",)`
- [ ] Keys injected from Ghost's keyring into the sidecar env
- [ ] Silero VAD + `smart-turn-v3.2` (both ship inside `pipecat-ai`)
- [ ] Wake word (`wake_word_bg.py`), name from `stonic.wake_word_name`
- [ ] Engine selector in Settings → Assistant; Kokoro remains default
- [ ] Gate: the CI Urdu voice round-trip still passes

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
