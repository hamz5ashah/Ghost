"""Offline natural voice: Kokoro on ONNX Runtime.

Same Kokoro-82M voices as ``kokoro_tts`` (e.g. ``af_heart``, American female)
but without PyTorch or a system espeak-ng: ``kokoro-onnx`` bundles espeak-ng
through ``espeakng-loader``, so it installs cleanly on Windows. The model
files (~350 MB) are downloaded once into ``<config dir>/models/kokoro`` via
:func:`start_model_download` (Settings → Assistant → Voice in the app), after
which synthesis works with no internet connection.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import urllib.request
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ghost.core.registry import TTSRegistry
from ghost.speech.tts import TTSBackend, TTSResult

logger = logging.getLogger(__name__)

_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODEL_FILES: Dict[str, int] = {
    # file name -> expected size in bytes (used for progress and validation)
    "kokoro-v1.0.onnx": 325_532_387,
    "voices-v1.0.bin": 28_214_398,
}

# Ghost's default companion is female: Heart is Kokoro v1.0's best-rated voice.
DEFAULT_VOICE = "af_heart"
# Kokoro has no Urdu voices, but its Hindi voices with espeak-ng's Urdu
# phonemizer ("ur") speak Urdu-script text naturally.
DEFAULT_URDU_VOICE = "hf_alpha"
# For a male English voice the Urdu runs keep a male voice too.
DEFAULT_URDU_MALE_VOICE = "hm_omega"

# Voices offered in the app (Kokoro v1.0 names). Prefix: h = Hindi/Urdu,
# a = American, b = British; then f/m = female/male.
VOICES: Dict[str, str] = {
    "hm_omega": "Omega — Urdu male",
    "hm_psi": "Psi — Urdu male, softer",
    "hf_alpha": "Alpha — Urdu female (default)",
    "hf_beta": "Beta — Urdu female, softer",
    "bm_george": "George — British male",
    "bm_lewis": "Lewis — British male, deeper",
    "bm_daniel": "Daniel — British male, calm",
    "bm_fable": "Fable — British male, storyteller",
    "bf_emma": "Emma — British female",
    "bf_isabella": "Isabella — British female",
    "bf_alice": "Alice — British female",
    "am_michael": "Michael — American male",
    "am_adam": "Adam — American male",
    "am_onyx": "Onyx — American male, deep",
    "af_heart": "Heart — American female (default)",
    "af_bella": "Bella — American female",
    "af_nicole": "Nicole — American female, soft",
}


def urdu_voice_for(voice: str) -> str:
    """The Urdu voice that goes with an English one: same gender (``?f_`` / ``?m_``)."""
    male = len(voice) > 1 and voice[1] == "m"
    return DEFAULT_URDU_MALE_VOICE if male else DEFAULT_URDU_VOICE


def model_dir() -> Path:
    from ghost.core.paths import get_config_dir

    return get_config_dir() / "models" / "kokoro"


def models_present(directory: Optional[Path] = None) -> bool:
    d = directory or model_dir()
    return all(
        (d / name).is_file() and (d / name).stat().st_size == size
        for name, size in MODEL_FILES.items()
    )


# ---------------------------------------------------------------------------
# One-time model download (runs in a background thread)
# ---------------------------------------------------------------------------

_download_lock = threading.Lock()
_download: Dict[str, Any] = {"state": "idle", "downloaded": 0, "total": 0, "error": ""}


def download_status() -> Dict[str, Any]:
    """``state``: missing | downloading | ready | error, with byte progress."""
    with _download_lock:
        status = dict(_download)
    if status["state"] != "downloading":
        if models_present():
            status["state"] = "ready"
        elif status["state"] != "error":
            status["state"] = "missing"
    status["total"] = status["total"] or sum(MODEL_FILES.values())
    return status


def _fetch(url: str, dest: Path, on_bytes) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "Ghost"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            on_bytes(len(chunk))
    tmp.replace(dest)


def start_model_download() -> Dict[str, Any]:
    """Start downloading the model files (no-op if present or in progress)."""
    with _download_lock:
        if _download["state"] == "downloading":
            return dict(_download)
        if models_present():
            return {**_download, "state": "ready"}
        _download.update(state="downloading", downloaded=0,
                         total=sum(MODEL_FILES.values()), error="")

    def run() -> None:
        d = model_dir()
        d.mkdir(parents=True, exist_ok=True)

        def add(n: int) -> None:
            with _download_lock:
                _download["downloaded"] += n

        try:
            for name, size in MODEL_FILES.items():
                path = d / name
                if path.is_file() and path.stat().st_size == size:
                    add(size)
                    continue
                _fetch(f"{_RELEASE}/{name}", path, add)
                if path.stat().st_size != size:
                    path.unlink(missing_ok=True)
                    raise RuntimeError(f"{name} downloaded incompletely")
            with _download_lock:
                _download["state"] = "ready"
        except Exception as exc:  # network errors, disk full, …
            logger.warning("Kokoro model download failed: %s", exc)
            with _download_lock:
                _download.update(state="error", error=str(exc))

    threading.Thread(target=run, name="kokoro-download", daemon=True).start()
    return download_status()


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


def script_runs(text: str) -> List[Tuple[str, bool]]:
    """Split *text* into ``(chunk, is_urdu_script)`` runs.

    Digits, spaces and punctuation stay with the run they sit in, so an Urdu
    sentence with an English word in it becomes three runs, not dozens.
    """
    runs: List[Tuple[str, bool]] = []
    current, current_urdu = "", None
    for ch in text:
        if _ARABIC_SCRIPT.match(ch):
            is_urdu: Optional[bool] = True
        elif ch.isalpha():
            is_urdu = False
        else:
            is_urdu = None  # neutral: joins the current run
        if is_urdu is None or current_urdu is None or is_urdu == current_urdu:
            current += ch
            if current_urdu is None and is_urdu is not None:
                current_urdu = is_urdu
        else:
            runs.append((current, bool(current_urdu)))
            current, current_urdu = ch, is_urdu
    if current.strip():
        runs.append((current, bool(current_urdu)))
    return [(t, u) for t, u in runs if t.strip()]


def _wav_bytes(samples: Any, sample_rate: int) -> bytes:
    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


@TTSRegistry.register("kokoro_onnx")
class KokoroOnnxTTSBackend(TTSBackend):
    """Kokoro voices on ONNX Runtime — fully offline once downloaded."""

    backend_id = "kokoro_onnx"
    _engine_lock = threading.Lock()

    def __init__(self, *, model_path: str = "") -> None:
        self._dir = Path(model_path) if model_path else model_dir()
        self._engine: Any = None

    def _load(self) -> Any:
        with self._engine_lock:
            if self._engine is None:
                from kokoro_onnx import Kokoro

                self._engine = Kokoro(
                    str(self._dir / "kokoro-v1.0.onnx"),
                    str(self._dir / "voices-v1.0.bin"),
                )
            return self._engine

    def health(self) -> bool:
        try:
            import kokoro_onnx  # noqa: F401
        except Exception:
            return False
        return models_present(self._dir)

    def available_voices(self) -> List[str]:
        return list(VOICES)

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "",
        speed: float = 1.0,
        output_format: str = "wav",
    ) -> TTSResult:
        import numpy as np

        voice = voice_id if voice_id in VOICES else DEFAULT_VOICE
        speed = max(0.5, min(float(speed or 1.0), 2.0))
        engine = self._load()
        english_lang = "en-gb" if voice.startswith("b") else "en-us"
        # Urdu-script runs use the Urdu phonemizer with an Urdu (Hindi) voice;
        # other runs use English. If the chosen voice is an Urdu voice it
        # speaks everything, so a mixed reply keeps one consistent voice.
        urdu_voice = voice if voice.startswith("h") else urdu_voice_for(voice)
        pieces = []
        sample_rate = 24000
        for chunk, is_urdu in script_runs(text) or [(text, False)]:
            samples, sample_rate = engine.create(
                chunk,
                voice=urdu_voice if is_urdu else voice,
                speed=speed,
                lang="ur" if is_urdu else english_lang,
            )
            pieces.append(samples)
            pieces.append(np.zeros(int(sample_rate * 0.06), dtype="float32"))
        samples = np.concatenate(pieces[:-1]) if pieces else np.zeros(1, dtype="float32")
        return TTSResult(
            audio=_wav_bytes(samples, sample_rate),
            format="wav",
            duration_seconds=len(samples) / float(sample_rate),
            voice_id=voice,
            sample_rate=sample_rate,
        )
