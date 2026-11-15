"""Engine aur app ke beech ka pul.

Stonic ke apne tools (yaadash, web search, screenshot, notes…) app ke andar
chalte hain — wohi code jo asal voice assistant ka hai (src/ai-tools). Engine
unhein dobara nahi likhta; sirf hukm bhejta hai aur natija leta hai:

    engine ──"stonic-tool-call" {id, name, args}──> app   (awaaz wale data channel par)
    app    ──POST /beta/tool-result {id, ok, text, image?}──> engine

Isi channel par "abhi kya ho raha hai" ki khabrein bhi jati hain
("stonic-activity"), taake screen par kaam nazar aaye.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from loguru import logger
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame

# App se aane wale natije ki haddein (yaad-dasht aur model ke context ki hifazat).
MAX_RESULT_CHARS = 24_000
MAX_IMAGE_BASE64_CHARS = 6_000_000


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


class ToolBridge:
    """App ke tool ko bulana aur us ke natije ka intezar."""

    def __init__(self, channel: AppChannel, timeout_secs: float = 90.0) -> None:
        self._channel = channel
        self._timeout = timeout_secs
        self._waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        call_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[call_id] = future
        try:
            if not await self._channel.send({"type": "stonic-tool-call", "id": call_id, "name": name, "args": args}):
                return {"ok": False, "text": f"{name} could not run: the Stonic app is not connected right now."}
            return await asyncio.wait_for(future, self._timeout)
        except asyncio.TimeoutError:
            logger.warning(f"App ka tool {name} {self._timeout:.0f} s mein jawab na de saka")
            return {"ok": False, "text": f"{name} did not answer in time. Do not retry it right now; tell them it did not work."}
        finally:
            self._waiting.pop(call_id, None)

    def resolve(self, call_id: str, payload: dict[str, Any]) -> bool:
        """App ka jawab. False = aisi koi call intezar mein nahi (der se aaya, ya ghalat id)."""
        future = self._waiting.get(str(call_id))
        if future is None or future.done():
            return False
        result: dict[str, Any] = {
            "ok": bool(payload.get("ok")),
            "text": str(payload.get("text") or "")[:MAX_RESULT_CHARS],
        }
        image = payload.get("image")
        if isinstance(image, dict) and isinstance(image.get("data"), str) and 0 < len(image["data"]) <= MAX_IMAGE_BASE64_CHARS:
            result["image"] = {"data": image["data"], "mimeType": str(image.get("mimeType") or "image/jpeg")[:40]}
        future.set_result(result)
        return True

    def close(self) -> None:
        for future in self._waiting.values():
            if not future.done():
                future.set_result({"ok": False, "text": "The conversation ended before this finished."})
        self._waiting.clear()
