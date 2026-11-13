# Ghost: project summary for a new chat

Paste this into a new chat so the assistant knows what has already been done.

## Owner and repo
- **Owner:** Syed Hamza Ali Shah. **Repo:** https://github.com/hamz5ashah/Ghost (branch `main`, private, for personal use).
- Ghost started from a clone of the open-source OpenJarvis project. It was renamed to **Ghost** everywhere, given a fresh git history, and relicensed as proprietary: "Copyright (c) 2026 Syed Hamza Ali Shah. All Rights Reserved." All references to the original authors were removed.
- Latest commits: `79af392` (hands-free voice mode), then `9d2cf45` (CI fix that prints Urdu in the Windows check).
- Builds are published as GitHub Releases named `build-N`, with app version `1.1.<run_number>`. The last confirmed one is **build-12**: `Ghost_1.1.12_x64-setup.exe` plus the Android APK.

## What Ghost is
Ghost is a personal AI assistant styled after Iron Man's JARVIS (a polite British "butler" persona). It has three parts:
1. **Python backend.** A FastAPI server started with `ghost serve` (package in `src/ghost/`, managed with uv, Rust extension `ghost_rust` built with maturin, Python 3.12).
2. **Windows desktop app.** Tauri 2 plus React/Vite (`frontend/` and `frontend/src-tauri/`). The installer is NSIS and bundles:
   - Ollama (CPU and Vulkan),
   - uv.exe,
   - the prebuilt ghost_rust wheel,
   - the backend source, which is copied to `~/Ghost` on first launch.

   Helper processes start with CREATE_NO_WINDOW, so no empty console windows appear.
3. **Android app.** A separate Tauri mobile shell in `mobile/`. It works as a remote client for Ghost running on your PC over Wi-Fi. You turn this on in desktop Settings → Phone & connection, which uses trusted-loopback auth and an API key. CI generates `gen/android`, and `mobile/scripts/patch-android.mjs` patches it to allow plain HTTP and the internet and microphone permissions.

## Desktop UI decisions (requested by the owner)
- Model setup, cloud providers, the system/usage panel and other configuration all live in **Settings**. None of it is on the launch screen or the chat screen.
- Settings tabs (`?tab=` URL parameter): General, Assistant, AI Model, Cloud providers, Phone & connection, Tools & memory, Usage & cost, Data, About.
- Cloud providers include Anthropic, OpenAI, DeepSeek and others. Keys are managed by the desktop app, including `DEEPSEEK_API_KEY` and `ELEVENLABS_API_KEY`. A cloud-only mode is available: `ghost serve --engine cloud`.
- The design is a professional baseline, with the JARVIS persona and a butler greeting on the home screen (`ButlerHome.tsx`).
- The PWA service worker is turned off in desktop builds (vite `--mode desktop`, a kill-switch `sw.js`, and cleanup at startup), so updates never show a stale UI.

## Assistant and language
- `frontend/src/lib/assistant-prompt.ts` builds the system prompt from:
  - the persona ('jarvis'),
  - the response style,
  - "about me" and custom instructions,
  - `replyLanguage`: auto, urdu or english.
- **Urdu is the main language and English is optional.** Urdu replies are written in Urdu script.

## Voice (speech output and speech input)
- **Offline voice:** Kokoro ONNX (`src/ghost/speech/kokoro_onnx_tts.py`). The model (about 354 MB) is downloaded on demand to `<config>/models/kokoro`.
  - English voices default to `bm_george`.
  - Urdu uses espeak's "ur" phonemizer with Hindi voices `hm_omega` (default), `hm_psi`, `hf_alpha` and `hf_beta`, and splits text into runs by script.
- **Online voice:** ElevenLabs. You can pick any voice from your account, the Voice Library or a custom voice ID.
- **Fallback:** the system voices (Web Speech API).
- Replies are spoken sentence by sentence as they arrive.
- **Speech input:** faster-whisper with VAD filtering, using the `small` model for Urdu and `base` for English. It is served at `/v1/speech/stt/status` and `/stt/prepare`.
- **Hands-free voice mode:**
  - Files: `VoiceMode.tsx`, `useVoiceConversation.ts` and `voice-activity.ts`.
  - Shortcut: Ctrl+Shift+Space.
  - Flow: it listens, detects when you stop speaking, transcribes, replies and speaks, then listens again.
  - Barge-in: you can talk over Ghost to interrupt it.
  - Labels appear in Urdu or English.
- The owner asked to copy another creator's voice ("Stonic AI"). This was declined. The allowed alternatives are ElevenLabs Voice Library voices, or cloning the owner's own voice.

## CI: `.github/workflows/build-apps.yml`
- Builds the Windows NSIS installer and the Android APK, then publishes the release `build-N`. Artifacts are kept for 3 days and each job has a timeout.
- A Windows smoke test then runs:
  1. a silent install, using `MSYS_NO_PATHCONV=1` so Git Bash doesn't rewrite the `/S` flag,
  2. Ollama, then `uv sync` with the bundled wheel,
  3. `ghost serve` against a mock LLM,
  4. an Urdu voice round trip (text to speech, then speech back to text) with `PYTHONIOENCODING=utf-8`.
- In the last run, Ghost said "السلام علیکم۔ آج لاہور کا موسم بہت اچھا ہے۔" and speech recognition heard nearly the same sentence.
- Note: GitHub Actions once stopped with a billing error ("recent account payments have failed"). Builds ran free while the repo was public.

## Tests
- Python test suite: everything passes except 4 tests that need network access.
- Frontend: 131 vitest tests. Desktop: 60 tests.
- Browser checks use Playwright with a stubbed `window.__TAURI_INTERNALS__`.

## Possible next steps (not started)
- Speak replies while they are still being written (streaming TTS).
- A "Hey Ghost" wake word.
- Cloning the owner's own voice for Ghost.
