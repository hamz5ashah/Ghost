"""The digest's model call (Stonic port, phase 6).

Stonic's ``memory/llm-relay.js`` passed the request from Electron's main
process to the renderer, because only the renderer held the user's Stonic
Cloud token. Ghost has no cloud account: the backend owns the engine and the
user's own key (OS keyring), so the "relay" is a direct, time-limited call to
Ghost's engine with the active model. The error codes are Stonic's, so the
digest's retry logic reads them the same way.
"""

from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 90.0


class LlmError(RuntimeError):
    """``code``: no_llm | timeout | rate_limited | auth | failed."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or f"memory llm: {code}")
        self.code = code


@dataclass
class LlmReply:
    text: str
    cost_usd: Optional[float] = None


LlmFn = Callable[[str, str, int], LlmReply]

_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="memory-llm"
)


def make_engine_llm(
    get_engine: Callable[[], Any],
    get_model: Callable[[], str],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> LlmFn:
    """An ``llm(system, user, max_tokens)`` that uses Ghost's own engine and model."""

    def call(system: str, user: str, max_tokens: int = 1200) -> LlmReply:
        engine = get_engine()
        model = get_model()
        if engine is None or not model:
            raise LlmError("no_llm")
        from ghost.core.types import Message, Role

        def run() -> Any:
            return engine.generate(
                messages=[
                    Message(role=Role.SYSTEM, content=system),
                    Message(role=Role.USER, content=user),
                ],
                model=model,
                temperature=0.2,
                max_tokens=max_tokens,
            )

        future = _POOL.submit(run)
        try:
            result = future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError as exc:
            raise LlmError("timeout") from exc
        except LlmError:
            raise
        except Exception as exc:  # noqa: BLE001 — map any engine error to a retryable code
            text = str(exc).lower()
            code = (
                "rate_limited"
                if "429" in text or "rate" in text
                else "auth"
                if "401" in text or "api key" in text
                else "failed"
            )
            logger.debug("memory llm failed (%s)", code, exc_info=True)
            raise LlmError(code, str(exc)[:200]) from exc
        content = (
            result.get("content")
            if isinstance(result, dict)
            else getattr(result, "content", "")
        )
        return LlmReply(str(content or ""))

    return call


__all__ = ["DEFAULT_TIMEOUT_S", "LlmError", "LlmFn", "LlmReply", "make_engine_llm"]
