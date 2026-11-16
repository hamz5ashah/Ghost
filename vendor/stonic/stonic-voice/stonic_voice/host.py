"""Engine se app (main process) ko khabar: stdout par ek JSON line.

    {"event": "ready", "port": 51234}
    {"event": "screen", "phase": "start"}

App (electron/services/voice-beta-manager.js) engine ka stdout line ba line
parhta hai. Jo line JSON nahi (kisi library ka chhapa hua kuch) wo wahan
nazar-andaz hoti hai, is liye yahan har khabar poori ek line mein jati hai.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

_lock = threading.Lock()


def emit(event: str, **fields: Any) -> None:
    line = json.dumps({"event": event, **fields}, ensure_ascii=False)
    with _lock:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass  # app ja chuki (pipe band): khabar sunne wala koi nahi
