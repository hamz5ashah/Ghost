"""Create and locate the on-demand sidecar venvs.

Each runtime is a plain venv under ``<config dir>/runtimes/<name>`` plus a
marker file recording which requirements file produced it. When the
requirements change the marker no longer matches and the runtime reports itself
as stale, so an upgraded Ghost reinstalls rather than running an engine against
the previous pin set.

Installation is deliberately conservative: it shells out to ``uv`` when
available and falls back to the stdlib ``venv`` module plus ``pip``. It never
touches Ghost's own environment.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

MARKER_NAME = ".ghost-runtime"

# Install states reported to the UI.
MISSING = "missing"
INSTALLING = "installing"
READY = "ready"
STALE = "stale"
ERROR = "error"


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    """A sidecar runtime Ghost can install on demand.

    ``requirements`` is the file name under ``configs/runtimes/``, which ships
    with the desktop app via the Tauri ``configs/`` resource mapping.
    """

    name: str
    requirements: str
    # Module run with ``-m`` to start the engine, for the phase that wires it.
    entry_module: str
    # Interpreter the upstream pins were resolved against, for diagnostics.
    pinned_python: str
    description: str


SPECS: Dict[str, RuntimeSpec] = {
    "voice": RuntimeSpec(
        name="voice",
        requirements="voice-requirements.txt",
        # A script, not a module: src/ghost/voice_sidecar/server.py.
        entry_module="server",
        pinned_python="3.11",
        description="Realtime voice engine (Pipecat, Silero VAD, smart-turn)",
    ),
    "computer": RuntimeSpec(
        name="computer",
        requirements="computer-requirements.txt",
        entry_module="runner",
        pinned_python="3.11",
        description="Computer use sidecar (Agent S3, pyautogui, OCR)",
    ),
}


def runtimes_dir() -> Path:
    from ghost.core.paths import get_config_dir

    return get_config_dir() / "runtimes"


def runtime_dir(name: str) -> Path:
    return runtimes_dir() / name


def _spec(name: str) -> RuntimeSpec:
    try:
        return SPECS[name]
    except KeyError:
        raise ValueError(
            f"unknown sidecar runtime {name!r}; expected one of "
            f"{', '.join(sorted(SPECS))}"
        ) from None


def requirements_path(name: str) -> Optional[Path]:
    """Locate the requirements file, preferring the packaged copy.

    Returns ``None`` when it cannot be found, which callers report as an
    install error rather than guessing at a pin set.
    """
    spec = _spec(name)
    candidates = []
    env_dir = os.environ.get("GHOST_RUNTIME_REQUIREMENTS_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    # Walk up from this module to the repo root (or the installed backend root,
    # where Tauri lands configs/ as backend/configs/). The source tree puts the
    # package under src/, so the root is one level above that — don't stop at
    # src/ itself or the repo-root copy is never seen.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(parent / "configs" / "runtimes")
        if parent.name == "src":
            candidates.append(parent.parent / "configs" / "runtimes")
            break
    for directory in candidates:
        candidate = directory / spec.requirements
        if candidate.is_file():
            return candidate
    return None


def _venv_python_in(root: Path) -> Path:
    """Interpreter path inside a venv rooted at ``root``."""
    if sys.platform == "win32":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def venv_python(name: str, base: Optional[Path] = None) -> Path:
    """Path to the interpreter inside a runtime's venv.

    The path is returned whether or not it exists, so callers can report a
    missing runtime with the location it is expected at.
    """
    return _venv_python_in(base if base is not None else runtime_dir(name))


def _requirements_digest(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest()[:16]


def _marker_path(name: str, base: Optional[Path] = None) -> Path:
    root = base if base is not None else runtime_dir(name)
    return root / MARKER_NAME


def _read_marker(name: str, base: Optional[Path] = None) -> str:
    try:
        return _marker_path(name, base).read_text().strip()
    except OSError:
        return ""


def is_installed(name: str, base: Optional[Path] = None) -> bool:
    """True when the venv exists and matches the current requirements file."""
    if not venv_python(name, base).is_file():
        return False
    marker = _read_marker(name, base)
    if not marker:
        return False
    requirements = requirements_path(name)
    if requirements is None:
        # Cannot verify freshness; a present venv is better than none.
        return True
    return marker == _requirements_digest(requirements)


_lock = threading.Lock()
_state: Dict[str, Dict[str, Any]] = {}


def _blank_state() -> Dict[str, Any]:
    return {"state": MISSING, "error": "", "log": ""}


def install_status(name: str, base: Optional[Path] = None) -> Dict[str, Any]:
    """Current install state for a runtime, for polling from the UI.

    ``state`` is one of ``missing``, ``installing``, ``ready``, ``stale`` or
    ``error``.
    """
    spec = _spec(name)
    with _lock:
        status = dict(_state.get(name) or _blank_state())
    if status["state"] == INSTALLING:
        status["name"] = spec.name
        status["description"] = spec.description
        return status
    if venv_python(name, base).is_file():
        status["state"] = READY if is_installed(name, base) else STALE
    elif status["state"] != ERROR:
        status["state"] = MISSING
    status["name"] = spec.name
    status["description"] = spec.description
    status["python"] = str(venv_python(name, base))
    return status


def _uv_executable() -> Optional[str]:
    return shutil.which("uv")


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    creationflags = 0
    if sys.platform == "win32":  # keep helper consoles hidden, as Ghost does
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=creationflags,
    )


def _create_venv(python: str, target: Path) -> None:
    uv = _uv_executable()
    if uv:
        result = _run([uv, "venv", "--python", python, str(target)])
        if result.returncode == 0:
            return
    result = _run([python, "-m", "venv", str(target)])
    if result.returncode != 0:
        raise RuntimeError(
            f"could not create venv at {target}: {result.stderr.strip()[:400]}"
        )


def _install_requirements(target: Path, requirements: Path) -> str:
    interpreter = str(_venv_python_in(target))
    uv = _uv_executable()
    if uv:
        result = _run(
            [uv, "pip", "install", "--python", interpreter, "-r", str(requirements)]
        )
        if result.returncode == 0:
            return result.stdout[-2000:]
    result = _run([interpreter, "-m", "pip", "install", "-r", str(requirements)])
    if result.returncode != 0:
        raise RuntimeError(f"dependency install failed: {result.stderr.strip()[-600:]}")
    return result.stdout[-2000:]


def start_install(
    name: str,
    python: str = "",
    base: Optional[Path] = None,
    block: bool = False,
) -> Dict[str, Any]:
    """Install a runtime in the background (no-op when already installing).

    ``block=True`` runs synchronously, which the tests use.
    """
    spec = _spec(name)
    with _lock:
        current = _state.get(name) or _blank_state()
        if current["state"] == INSTALLING:
            return dict(current)
        _state[name] = {"state": INSTALLING, "error": "", "log": ""}

    target = base if base is not None else runtime_dir(name)
    interpreter = python or spec.pinned_python or sys.executable

    def run() -> None:
        try:
            requirements = requirements_path(name)
            if requirements is None:
                raise RuntimeError(
                    f"requirements file {spec.requirements!r} not found; "
                    "expected it under configs/runtimes/"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            _create_venv(interpreter, target)
            log = _install_requirements(target, requirements)
            _marker_path(name, target).write_text(_requirements_digest(requirements))
            with _lock:
                _state[name] = {"state": READY, "error": "", "log": log}
        except Exception as exc:  # surfaced to the UI, never raised at import
            with _lock:
                _state[name] = {"state": ERROR, "error": str(exc)[:600], "log": ""}

    if block:
        run()
    else:
        threading.Thread(target=run, name=f"ghost-runtime-{name}", daemon=True).start()
    with _lock:
        return dict(_state.get(name) or _blank_state())


def _reset_state_for_tests() -> None:
    with _lock:
        _state.clear()
