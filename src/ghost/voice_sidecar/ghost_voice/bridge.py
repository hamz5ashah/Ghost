"""Engine aur app ke beech ka pul.

Ghost port (talk-only): only AppChannel is kept — it carries status messages
("ghost-latency", "ghost-error", "ghost-idle") to the app over the WebRTC data
channel. Stonic's ToolBridge, which ran the agent brain's tools inside the app,
is not ported: the talk-only brain has no tools.

Stonic ka asal bayan, tareekh ke liye:

Stonic ke apne tools (yaadash, web search, screenshot, notes…) app ke andar
chalte hain — wohi code jo asal voice assistant ka hai (src/ai-tools). Engine
unhein dobara nahi likhta; sirf hukm bhejta hai aur natija leta hai:

    engine ──"ghost-tool-call" {id, name, args}──> app   (awaaz wale data channel par)
    app    ──POST /beta/tool-result {id, ok, text, image?}──> engine

Isi channel par "abhi kya ho raha hai" ki khabrein bhi jati hain
("ghost-activity"), taake screen par kaam nazar aaye.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame


class AppChannel:
    """App ko paigham bhejne ka raasta. Pipeline ban jane ke baad `attach` hota hai."""

    def __init__(self) -> None:
        self._worker: Any = None

    def attach(self, worker: Any) -> None:
        self._worker = worker

    async def send(self, data: dict[str, Any]) -> bool:
        worker = self._worker
        if worker is None:
            return False
        try:
            await worker.queue_frame(RTVIServerMessageFrame(data=data))
            return True
        except Exception as error:  # noqa: BLE001 — session band ho rahi ho to bhi dimagh na gire
            logger.debug(f"App ko paigham nahi gaya ({data.get('type')}): {error}")
            return False
