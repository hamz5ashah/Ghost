# Stonic AI 3.1.21 — what was recovered from the installer

Recovered from `Stonic-AI-Setup-3_1_21.exe` on 14 Nov 2026. There is no Stonic
repository on GitHub, so this installer is the only copy.

Build fingerprint from the artifacts: app `stonic_gen3` v3.1.21, Electron app
built 7 Nov 2026, voice runtime built 2026-11-07, computer runtime 2026-11-03,
original build machine path `C:\Users\Usman\Desktop\Workspace Stonic AI\Stonic Ai - AGI\StonicAi_main\`.

---

## `source/` — real, editable source. 40,321 lines.

This is genuine source, not decompiled output. Comments, structure and naming
are all original.

| Area | Files | Lines | Notes |
|---|---|---|---|
| `electron/services/` | 58 | 23,121 | the whole service layer, comments intact |
| `electron/main/` | 16 | 1,791 | bootstrap, shared paths |
| `electron/preload.js` | 1 | 290 | full IPC allow-list |
| `electron/pwa/` | 2 | 456 | companion PWA |
| `stonic-voice/` | 22 | 11,455 | Pipecat + Claude Agent SDK voice engine |
| `stonic-computer/` | 4 | 3,035 | Agent S3 sidecar, overlay HUD, esc guard |
| `wake_word_bg.py` | 1 | ~170 | wake-word detector |

Also recovered: `package.json`, `settings.toml`, `prompt.md`,
`agent_prompt.md`, `work_prompt.md`, both character files
(`male_jarvis.toml`, `female_soulmate.toml`), `requirements.txt` for both
Python runtimes (fully pinned), `app-update.yml`, `stonic-gen3.marker`,
and the upgrade-cleanup PowerShell script.

Nothing in here needs rewriting. Open it and keep working.

---

## `binary-only/` — cannot be turned back into source

**`main.jsc`** (209 KB) — the Electron main process, compiled to V8 bytecode by
bytenode. `main.js` in the source tree is only the 2-line loader that requires
it. V8 bytecode cannot be decompiled back to JavaScript. It still *runs*, but
only on the exact Electron/V8 build it was compiled against, and it cannot be
edited.

**`renderer-built/`** — the React UI, bundled and mangled by Vite. No
sourcemaps were shipped and no `src/` paths survive in the bundle, so the
original `.tsx` is not recoverable. The built app is complete and runnable; the
source behind it is gone.

Everything else — `videos/`, `facelock/` models (`sface.onnx`,
`face_landmarker.task`), `mediapipe/`, `onboarding/`, `tour/`, audio, avatar,
earth textures — is shipped assets, already intact in the installer. Copy them
straight across; they were never source.

---

## `reconstruction/` — what to rebuild the two missing pieces from

- **`main-jsc-symbols.md`** — 829 identifiers, 135 class names, 152 IPC channel
  names, local `require()` targets and every URL, lifted out of the V8 cache.
  These are the exact names the original `main.js` used.
- **`service-api-map.md`** — all 78 recovered modules with their exports and
  local dependencies.
- **`renderer-ui-strings.md`** — 9,242 user-facing strings from the bundle: the
  surviving copy, labels, errors and prompts.

`main.js` is the realistic rebuild. It was a wiring file: it created windows,
registered IPC handlers, and delegated to the services that are already intact.
With the channel list, the class names and the service exports, it can be
reconstructed to working order.

The React renderer is a genuine rewrite — 3.9 MB of mangled bundle is a
behavioural reference, not a starting point. The UI strings plus the built app
(run it side by side) are what to work from.

---

## Build config that was never in the installer

These were in the project root and are not shipped, so they need recreating:
`vite.config.ts`, `tsconfig.json`, `electron-builder.yml` / build block,
`scripts/setup-voice-runtime.js`, `scripts/setup-computer-runtime.js`, the
`voice-lab/` and `tests/` directories the comments refer to, and the Supabase
SQL (`supabase/user-characters.sql`).

Several are partly described in the recovered comments — the setup scripts and
their `--no-deps` install behaviour are documented in detail at the top of both
`requirements.txt` files.

---

## First thing to do

Put this in git and push it, private, today. Then rebuild `main.js` against the
symbol list before anything else — until it exists, the source tree cannot be
built into a running app.
