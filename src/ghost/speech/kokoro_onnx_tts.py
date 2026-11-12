"""Offline natural voice: Kokoro on ONNX Runtime.

Same Kokoro-82M voices as ``kokoro_tts`` (e.g. ``bm_george``, British male)
but without PyTorch or a system espeak-ng: ``kokoro-onnx`` bundles espeak-ng
through ``espeakng-loader``, so it installs cleanly on Windows. The model
files (~350 MB) are downloaded once into ``<config dir>/models/kokoro`` via
:func:`start_model_download` (Settings → Assistant → Voice in the app), after
which synthesis works with no internet connection.
"""

from __future__ import annotations

import io
import logging
import threading
import urllib.request
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

from ghost.core.registry import TTSRegistry
from ghost.speech.tts import TTSBackend, TTSResult

logger = logging.getLogger(__name__)

_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODEL_FILES: Dict[str, int] = {
    # file name -> expected size in bytes (used for progress and validation)
    "kokoro-v1.0.onnx": 325_532_387,
    "voices-v1.0.bin": 28_214_398,
}

DEFAULT_VOICE = "bm_george"

# Voices offered in the app (Kokoro v1.0 names). Prefix: a = American,
# b = British; then f/m = female/male.
VOICES: Dict[str, str] = {
    "bm_george": "George — British male (default)",
    "bm_lewis": "Lewis — British male, deeper",
    "bm_daniel": "Daniel — British male, calm",
    "bm_fable": "Fable — British male, storyteller",
    "bf_emma": "Emma — British female",
    "bf_isabella": "Isabella — British female",
    "bf_alice": "Alice — British female",
    "am_michael": "Michael — American male",
    "am_adam": "Adam — American male",
    "am_onyx": "Onyx — American male, deep",
    "af_heart": "Heart — American female",
    "af_bella": "Bella — American female",
    "af_nicole": "Nicole — American female, soft",
}


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
        voice = voice_id if voice_id in VOICES else DEFAULT_VOICE
        lang = "en-gb" if voice.startswith("b") else "en-us"
        samples, sample_rate = self._load().create(
            text, voice=voice, speed=max(0.5, min(float(speed or 1.0), 2.0)), lang=lang
        )
        return TTSResult(
            audio=_wav_bytes(samples, sample_rate),
            format="wav",
            duration_seconds=len(samples) / float(sample_rate),
            voice_id=voice,
            sample_rate=sample_rate,
        )
