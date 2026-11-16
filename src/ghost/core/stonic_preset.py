"""The "Always-Active Stonic Mode" preset — every companion feature on at once.

The individual ``[stonic]`` flags stay exactly as they were (each still
defaults to off, and each can still be set on its own). The preset is a
convenience on top: turning it on writes the seven flags below to ``true`` in
``config.toml`` and remembers what each one was before; turning it off puts
every one back the way the user had it. The rest of ``config.toml`` —
formatting and comments included — is left untouched (``tomlkit``).

Turned on, Ghost is a continuous companion: it listens for its name, talks in
realtime, notices what is going on and speaks up, stays on the desktop when
minimised, plays its UI sounds, uses its character, and remembers.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PRESET = "always_active"
#: The flags the preset turns on, in the order Settings lists them.
ALWAYS_ACTIVE_FLAGS = (
    "realtime_voice",
    "wake_word",
    "awareness",
    "desktop_presence",
    "ui_sounds",
    "characters",
    "memory_v2",
)

_lock = threading.Lock()


def _config_dir() -> Path:
    from ghost.core.paths import get_config_dir

    return get_config_dir()


def config_path() -> Path:
    return _config_dir() / "config.toml"


def _saved_path() -> Path:
    return _config_dir() / "stonic-preset.json"


def _read_doc(path: Path):
    import tomlkit

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    return tomlkit.parse(text) if text.strip() else tomlkit.document()


def current_flags(path: Optional[Path] = None) -> Dict[str, bool]:
    """The preset's flags as ``config.toml`` has them now (missing = off)."""
    doc = _read_doc(path or config_path())
    stonic = doc.get("stonic") or {}
    return {name: bool(stonic.get(name, False)) for name in ALWAYS_ACTIVE_FLAGS}


def status(path: Optional[Path] = None) -> Dict[str, Any]:
    flags = current_flags(path)
    saved = _load_saved()
    return {
        "preset": PRESET,
        "active": all(flags.values()),
        "flags": flags,
        "can_restore": bool(saved.get("previous")),
    }


def _load_saved() -> Dict[str, Any]:
    try:
        data = json.loads(_saved_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def apply(
    enabled: bool, *, path: Optional[Path] = None, config: Any = None
) -> Dict[str, Any]:
    """Turn the preset on (all seven flags true) or off (each flag back to what it was).

    ``config``: the running server's config object; its ``stonic`` section is
    updated in place so the change applies without a restart.
    """
    import tomlkit

    path = path or config_path()
    with _lock:
        doc = _read_doc(path)
        if "stonic" not in doc:
            doc["stonic"] = tomlkit.table()
        table = doc["stonic"]
        if enabled:
            before = {
                name: bool(table.get(name, False)) for name in ALWAYS_ACTIVE_FLAGS
            }
            saved = _load_saved()
            # Re-applying keeps the ORIGINAL snapshot, not the all-on state.
            if not saved.get("previous") or not all(before.values()):
                _saved_path().parent.mkdir(parents=True, exist_ok=True)
                _saved_path().write_text(
                    json.dumps({"preset": PRESET, "previous": before}, indent=2),
                    encoding="utf-8",
                )
            target = dict.fromkeys(ALWAYS_ACTIVE_FLAGS, True)
        else:
            previous = _load_saved().get("previous") or {}
            target = {
                name: bool(previous.get(name, False)) for name in ALWAYS_ACTIVE_FLAGS
            }
        for name, value in target.items():
            table[name] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tomlkit.dumps(doc), encoding="utf-8")
        if not enabled:
            try:
                _saved_path().unlink()
            except OSError:
                pass
    stonic = getattr(config, "stonic", None)
    if stonic is not None:
        for name, value in target.items():
            try:
                setattr(stonic, name, value)
            except (AttributeError, TypeError):
                logger.debug("cannot set stonic.%s in memory", name)
    logger.info(
        "Always-Active Stonic Mode %s", "on" if enabled else "off (flags restored)"
    )
    return {**status(path), "applied": target}


__all__ = [
    "ALWAYS_ACTIVE_FLAGS",
    "PRESET",
    "apply",
    "config_path",
    "current_flags",
    "status",
]
