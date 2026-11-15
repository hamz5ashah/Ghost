"""Nishana: "ye cheez screen par kahan hai?" — UI-TARS se.

Beta ka dimagh (Claude) screen dekh kar faisla karta hai ke kya dabana hai aur
use lafzon mein batata hai ("the blue Save button at the bottom right of the
dialog"). Ye file wohi jumla aur screen ki taaza tasveer UI-TARS ko deti hai —
ek chhota model jo sirf isi kaam par sikhaya gaya hai — aur wo pixel batata hai.

Wohi sawal aur wohi jawab ka tareeqa jo asal computer agent mein hai (Agent S3
ka `generate_coords`): model ko tasveer ke baad ek line, jawab mein pehle do
adad = x, y, usi tasveer ke paimane mein jo bheji gayi.

Raasta OpenAI-jaisa `/chat/completions` hai: user ki apni OpenRouter key par
seedha OpenRouter. Ghalti ka matn angrezi mein hai — wo dimagh ko jata hai, jo
use user ki zubaan mein bolta hai. Chup chaap kisi doosre raaste par girna nahi:
nishana na lage to kaam rukta hai aur wajah boli jati hai.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
from loguru import logger

from stonic_voice.hands import Shot

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# Jawab tasveer se itna bahar ho to wo nishana nahi, model ki ghalti hai.
_SLACK = 1.02


class AimError(Exception):
    """Nishana na lag saka. `kind` wohi naam hain jo app ka card pehchanta hai (src/lib/api-issue.ts)."""

    def __init__(self, message: str, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


def prompt_for(description: str) -> str:
    return f"Query:{description}\nOutput only the coordinate of one point in your response.\n"


def parse_point(text: str, width: int, height: int) -> tuple[int, int]:
    """Model ke jawab se x, y (tasveer ke pixel)."""
    numbers = _NUMBER.findall(str(text or ""))
    if len(numbers) < 2:
        raise AimError("The pointing model did not return a position. Describe the element differently and try again.", "bad_answer")
    x, y = float(numbers[0]), float(numbers[1])
    if x > width * _SLACK or y > height * _SLACK:
        raise AimError("The pointing model returned a position outside the screen. Describe the element differently and try again.", "bad_answer")
    return min(int(round(x)), width - 1), min(int(round(y)), height - 1)


def explain_status(status: int, body: str) -> AimError:
    """OpenRouter ki rukawat -> saaf wajah (wohi matn jo asal computer agent ka hai, runner.py)."""
    text = body.lower()
    if any(marker in text for marker in ("key limit exceeded", "limit exceeded for this key", "key spending limit")):
        return AimError("The OpenRouter API key has reached the spending limit set on it. Raise the key's limit on openrouter.ai, or use another key.", "key_limit")
    if status == 401:
        return AimError("OpenRouter rejected the API key saved in Beta settings (wrong, deleted or disabled). A working OpenRouter key is needed there.", "invalid_key")
    if status == 402:
        return AimError("The OpenRouter key is fine, but the OpenRouter account has no credits left. Credits are added on openrouter.ai.", "no_credits")
    if any(marker in text for marker in ("no endpoints found", "data policy", "no allowed providers")):
        return AimError("OpenRouter has no provider for the pointing model with this account's privacy settings. It is allowed on openrouter.ai under Settings, Privacy.", "model_unavailable")
    if status == 403:
        return AimError("OpenRouter refused the request (a guardrail or privacy setting on the account, or the key lacks permission).", "forbidden")
    if status == 429:
        return AimError("OpenRouter is rate-limiting this key right now. Nothing is wrong with the key; it works again in a minute.", "rate_limit")
    if status >= 500:
        return AimError("OpenRouter did not answer. It usually works again in a moment.", "unavailable")
    return AimError(f"The pointing model refused the request (HTTP {status}).", "refused")


class Aimer:
    """UI-TARS se ek nuqta. Ek baat-cheet ke liye ek connection, garam rakha hua."""

    def __init__(self, *, url: str, key: str, model: str, timeout_secs: float = 20.0) -> None:
        self._url = url.rstrip("/") + "/chat/completions"
        self._key = key
        self._model = model
        self._timeout = httpx.Timeout(timeout_secs, connect=8.0)
        self._client: httpx.AsyncClient | None = None

    @property
    def ready(self) -> bool:
        return bool(self._key)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, limits=httpx.Limits(keepalive_expiry=300))
        return self._client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def locate(self, description: str, shot: Shot) -> tuple[int, int]:
        """`description` wali cheez `shot` mein kahan hai — us tasveer ke pixel."""
        if not self._key:
            raise AimError(
                "Screen control is set to UI-TARS, and there is no OpenRouter API key in Beta settings. "
                "They can paste one there, or switch Screen control to Claude.",
                "no_key",
            )
        body: dict[str, Any] = {
            "model": self._model,
            "temperature": 0,
            "max_tokens": 128,
            "stream": False,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{shot.mime};base64,{shot.b64()}", "detail": "high"}},
                    {"type": "text", "text": prompt_for(description)},
                ],
            }],
        }
        started = time.perf_counter()
        try:
            response = await self._http().post(self._url, json=body, headers={"Authorization": f"Bearer {self._key}"})
        except httpx.TimeoutException as error:
            raise AimError("The pointing model took too long to answer. It usually works on a second try.", "timeout") from error
        except httpx.HTTPError as error:
            raise AimError(f"The pointing model could not be reached ({type(error).__name__}). Check the internet connection.", "network") from error
        if response.status_code != 200:
            raise explain_status(response.status_code, response.text[:2000])
        try:
            data = response.json()
        except ValueError as error:
            raise AimError("The pointing model sent an answer that could not be read.", "bad_answer") from error
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            # OpenRouter kabhi 200 ke andar ghalti bhejta hai (provider ki rukawat).
            code = data["error"].get("code")
            raise explain_status(code if isinstance(code, int) else 502, str(data["error"].get("message") or ""))
        try:
            answer = str(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as error:
            raise AimError("The pointing model sent an empty answer.", "bad_answer") from error
        point = parse_point(answer, shot.width, shot.height)
        logger.info(f"Nishana: {round((time.perf_counter() - started) * 1000)} ms, {point} / {shot.width}x{shot.height}")
        logger.debug(f"Nishana: '{description}' -> {answer!r}")
        return point
