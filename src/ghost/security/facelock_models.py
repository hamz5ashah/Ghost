"""On-demand models and runtimes for FaceLock and gesture control (Phase 4).

Nothing here ships in the installer. The first time the user turns on Face
Lock or hand control, these public files are fetched once into
``<config>/models/facelock`` — the same pattern as the Kokoro voice model
(:mod:`ghost.speech.kokoro_onnx_tts`): a presence check, a status dict the UI
polls, a background download.

Every file is pinned by size **and** SHA-256, so a changed or tampered mirror
is refused rather than loaded into the camera pipeline. The pins are the exact
bytes Stonic shipped (recovered from its installer). ``sface.onnx`` is
byte-identical to OpenCV Zoo's ``face_recognition_sface_2021dec.onnx``, and
the two WASM runtimes to ``@mediapipe/tasks-vision@0.10.35`` and
``onnxruntime-web@1.30.0`` on npm — all three checked when this was written.

The files are served read-only to the WebView from ``/facelock-assets/`` (see
``api_routes``). They are public model weights and runtimes, never user data:
face prints and the PIN stay in :mod:`ghost.security.facelock`.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MEDIAPIPE = "https://storage.googleapis.com/mediapipe-models"
_TASKS_VISION = "@mediapipe/tasks-vision@0.10.35/wasm/vision_wasm_internal.wasm"
_ORT = "onnxruntime-web@1.30.0/dist/ort-wasm-simd-threaded.wasm"


@dataclass(frozen=True)
class Asset:
    name: str
    size: int
    sha256: str
    urls: Tuple[str, ...]
    media_type: str = "application/octet-stream"


ASSETS: Dict[str, Asset] = {
    a.name: a
    for a in (
        Asset(
            "face_landmarker.task",
            3_758_596,
            "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff",
            (
                f"{_MEDIAPIPE}/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
            ),
        ),
        Asset(
            "sface.onnx",
            38_696_353,
            "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
            (
                "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
                "models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
            ),
        ),
        Asset(
            "gesture_recognizer.task",
            8_373_440,
            "97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482",
            (
                f"{_MEDIAPIPE}/gesture_recognizer/gesture_recognizer/float16/1/"
                "gesture_recognizer.task",
            ),
        ),
        Asset(
            "vision_wasm_internal.wasm",
            11_153_617,
            "6a5c64584c2ab61c763b6e204afbdbc7ce1caf7f5216187322bca8df94f646bc",
            (
                f"https://cdn.jsdelivr.net/npm/{_TASKS_VISION}",
                f"https://unpkg.com/{_TASKS_VISION}",
            ),
            "application/wasm",
        ),
        Asset(
            "ort-wasm-simd-threaded.wasm",
            14_239_897,
            "3398c10d07d229bd91b364548e130e0e51a8e5704b88c7c083ebbeb78842dee2",
            (f"https://cdn.jsdelivr.net/npm/{_ORT}", f"https://unpkg.com/{_ORT}"),
            "application/wasm",
        ),
    )
}

# Which files each feature needs.
BUNDLES: Dict[str, Tuple[str, ...]] = {
    "facelock": (
        "face_landmarker.task",
        "sface.onnx",
        "vision_wasm_internal.wasm",
        "ort-wasm-simd-threaded.wasm",
    ),
    "gestures": ("gesture_recognizer.task", "vision_wasm_internal.wasm"),
}


def model_dir() -> Path:
    from ghost.core.paths import get_config_dir  # noqa: PLC0415

    return get_config_dir() / "models" / "facelock"


def _names(bundle: str) -> Tuple[str, ...]:
    if bundle not in BUNDLES:
        raise ValueError(f"unknown bundle {bundle!r}")
    return BUNDLES[bundle]


def _present(asset: Asset, directory: Path) -> bool:
    """Size check only — hashing 70 MB on every status poll would be slow.

    The hash is checked once, when the file is downloaded, before it is moved
    into place; a file that arrived any other way and has the right size is
    re-hashed by :func:`verify` before it is first served.
    """
    path = directory / asset.name
    return path.is_file() and path.stat().st_size == asset.size


def bundle_present(bundle: str, directory: Optional[Path] = None) -> bool:
    d = directory or model_dir()
    return all(_present(ASSETS[n], d) for n in _names(bundle))


_verified: Dict[str, Tuple[float, int]] = {}


def verify(name: str, directory: Optional[Path] = None) -> bool:
    """Hash-check a file once per (mtime, size); delete it if it is wrong."""
    asset = ASSETS.get(name)
    if asset is None:
        return False
    path = (directory or model_dir()) / name
    try:
        stat = path.stat()
    except OSError:
        return False
    key = (stat.st_mtime, stat.st_size)
    if _verified.get(str(path)) == key:
        return True
    if stat.st_size != asset.size or _sha256(path) != asset.sha256:
        logger.warning("FaceLock asset %s failed verification; removing it", name)
        path.unlink(missing_ok=True)
        return False
    _verified[str(path)] = key
    return True


def asset_path(name: str, directory: Optional[Path] = None) -> Optional[Path]:
    """Path to a verified asset, or None. Unknown names are always None."""
    if name not in ASSETS:
        return None
    d = directory or model_dir()
    return d / name if verify(name, d) else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Background download
# ---------------------------------------------------------------------------

Fetcher = Callable[[str, Path, Callable[[int], None]], None]


def _fetch(url: str, dest: Path, on_bytes: Callable[[int], None]) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Ghost"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            on_bytes(len(chunk))


_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}


def _blank(bundle: str) -> Dict[str, Any]:
    total = sum(ASSETS[n].size for n in _names(bundle))
    return {"state": "idle", "downloaded": 0, "total": total, "error": "", "file": ""}


def download_status(bundle: str, directory: Optional[Path] = None) -> Dict[str, Any]:
    """``state``: missing | downloading | ready | error, with byte progress."""
    with _lock:
        status = dict(_jobs.get(bundle) or _blank(bundle))
    if status["state"] != "downloading":
        if bundle_present(bundle, directory):
            status["state"] = "ready"
        elif status["state"] != "error":
            status["state"] = "missing"
    status["files"] = list(_names(bundle))
    return status


def _download_one(
    asset: Asset, directory: Path, add: Callable[[int], None], fetch: Fetcher
) -> None:
    final = directory / asset.name
    errors: List[str] = []
    for url in asset.urls:
        tmp = final.with_suffix(final.suffix + ".part")
        got = 0

        def count(n: int) -> None:
            nonlocal got
            got += n
            add(n)

        try:
            fetch(url, tmp, count)
            if tmp.stat().st_size != asset.size:
                raise RuntimeError("downloaded incompletely")
            if _sha256(tmp) != asset.sha256:
                raise RuntimeError("checksum mismatch")
            tmp.replace(final)
            return
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            tmp.unlink(missing_ok=True)
            add(-got)
            errors.append(f"{url.split('/')[2]}: {exc}")
    raise RuntimeError(f"{asset.name}: " + "; ".join(errors))


def start_download(
    bundle: str,
    directory: Optional[Path] = None,
    *,
    fetch: Fetcher = _fetch,
    block: bool = False,
) -> Dict[str, Any]:
    """Download a bundle's missing files (no-op if present or in progress)."""
    names = _names(bundle)
    with _lock:
        job = _jobs.get(bundle)
        if job and job["state"] == "downloading":
            return dict(job)
        if bundle_present(bundle, directory):
            return {**_blank(bundle), "state": "ready"}
        job = _blank(bundle)
        job["state"] = "downloading"
        _jobs[bundle] = job

    def add(n: int) -> None:
        with _lock:
            job["downloaded"] = max(0, job["downloaded"] + n)

    def run() -> None:
        d = directory or model_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            for name in names:
                asset = ASSETS[name]
                with _lock:
                    job["file"] = name
                if _present(asset, d) and verify(name, d):
                    add(asset.size)
                    continue
                _download_one(asset, d, add, fetch)
            with _lock:
                job.update(state="ready", file="")
        except Exception as exc:  # noqa: BLE001 - network, disk full, …
            logger.warning("FaceLock model download failed: %s", exc)
            with _lock:
                job.update(state="error", error=str(exc)[:400])

    if block:
        run()
    else:
        threading.Thread(target=run, name=f"ghost-{bundle}-models", daemon=True).start()
    with _lock:
        return dict(job)
