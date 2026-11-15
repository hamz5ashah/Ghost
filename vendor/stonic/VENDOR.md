# vendor/stonic — reference source for the Stonic port

This is the **recovered** Stonic AI 3.1.21 source, vendored as read-only
reference material for the port tracked in `/STONIC_PORT_PLAN.md`.

It is vendored rather than linked because it is the only surviving copy: the
Stonic repository was deleted and never pushed to GitHub, so this tree was
reconstructed from `Stonic-AI-Setup-3_1_21.exe` on 14 Nov 2026.

**Nothing here is built or imported by Ghost.** Ported code is rewritten into
`src/ghost/`, `frontend/src/` and `frontend/src-tauri/src/`. This directory
exists so each port can be checked against the original, and so the recovered
source is under version control.

## What is here

| Path | Lines | Notes |
|---|---|---|
| `electron/services/` | 23,121 | 58 modules; comments are Roman Urdu, preserve the logic behind them |
| `electron/main/`, `electron/pwa/`, `preload.js` | 2,537 | bootstrap, IPC allow-list |
| `stonic-voice/` | 11,455 | Pipecat + Claude Agent SDK realtime voice engine |
| `stonic-computer/` | 3,035 | Agent S3 sidecar, overlay HUD, esc guard |
| `wake_word_bg.py` | ~170 | wake-word detector |
| `reconstruction/` | — | symbol maps for `main.jsc` and the renderer (both unrecoverable) |
| `RECOVERY.md` | — | what survived the installer and what did not |

`main.js` here is only the 2-line bytenode loader. The real main process was
`main.jsc` (V8 bytecode, not decompilable); `reconstruction/main-jsc-symbols.md`
holds its 829 identifiers, 135 class names and 152 IPC channel names.

## Media and models — not vendored

Binary assets are **not** committed here (124 MB). They live outside the repo
and are placed into `frontend/public/` and `frontend/src-tauri` resources during
Phase 1. Verify against these digests before use:

```
9b1fbf0c11c520ae714af8aa9af12cfd48503eedecd7398d8992ee94d1b4dc37  helpers/elevate.exe
ac3f2778f384acbe366d34de0317d53ae9d61dbd285e87ad0b14c07c149dc144  helpers/swap-helper.exe
64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff  models/face_landmarker.task
97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482  models/gesture_recognizer.task
0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79  models/sface.onnx
737220a872eddd72f96f4bafc445f7cabf7b03c12261100312966add084fdc69  onboarding/music/onboarding-score.mp3
7a752b5f7aff4e9957ebd5a576a8566df6c8257b06c2ed85ed8b080c9a2d64e7  videos/light-female-1.mp4
0e7ca8e7306b944076702565860507a4451ced8f5849570ca4f0860943199ab6  videos/light-female-1r.mp4
b9067532a0d5320183cd15bd7729876e898b4ba138bffa3d4d8b7ad706670a26  videos/stonic-ai-engine-guide.mp4
```

Counts: 16 UI sound effects, 97 onboarding voice lines, 28 tour voice lines
(14 of them `male_jarvis-*`, which are kept; the Stonic-specific screen and
billing lines are dropped per the approved plan).

The three on-device models are off-the-shelf (OpenCV Zoo SFace, Google
MediaPipe) and re-downloadable; they are kept only to avoid re-sourcing.

## Rules for this directory

1. Do not add it to any build path, `pyproject.toml`, `package.json` or
   `Cargo.toml`.
2. Do not edit it. It is a historical record; fixes belong in the ported code.
3. Do not ship it. It is excluded from Tauri `bundle.resources`.
