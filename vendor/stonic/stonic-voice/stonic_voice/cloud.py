"""Stonic Cloud ka raasta: kaan aur munh hamare gateway se, user ke plan ke andar.

Engine ke paas yahan kisi provider ki key nahi hoti — sirf Stonic ka token
(device key ya login token) jo app deti hai. Gateway
(stonic-cloud-gateway/src/voice-beta.js) us token se user pehchanta hai, plan
ki hadd dekhta hai, Google se baat karta hai aur har hisse ka hisaab alag
likhta hai:

    kaan    WS   /v1/voice-beta/listen/…   Google ki Live transcription ka relay
    dimagh  POST /v1/chat/completions      (pipeline.py — Pipecat ka OpenAI service)
    munh    POST /v1/voice-beta/speech     Gemini 3.8 TTS, awaaz stream hoti hai
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from google.genai import types
from loguru import logger

from stonic_voice.tts_gemini import SpeechUnavailable

SPEECH_PATH = "/v1/voice-beta/speech"
LISTEN_PATH = "/v1/voice-beta/listen"

# Pehli awaaz aam taur par ~1.1-1.6 s mein (naapa hua, 12 Oct 2026); ek tukre
# ki poori awaaz chand second. Is se lamba intezar = gateway ya Google atka hai.
CONNECT_TIMEOUT_SECS = 10.0
READ_TIMEOUT_SECS = 30.0
KEEP_ALIVE_SECS = 300.0


def describe_denial(status: int, body: bytes) -> SpeechUnavailable:
    """Gateway ka inkaar -> wo code jo app user ko dikha sake.

        401 / 403  login nahi / account band        cloud_signin
        402        plan nahi                        cloud_no_plan
        429        plan ki hadd (khud reset hogi)   cloud_limit
        422        Google ne ye line bolne se inkar kiya
        baqi       gateway ya Google ka masla       cloud_unavailable
    """
    message, resets_at = "", ""
    try:
        error = (json.loads(body or b"{}") or {}).get("error") or {}
        if isinstance(error, dict):
            message = str(error.get("message") or "")
            resets_at = str(error.get("resets_at") or "")
        else:
            message = str(error)
    except (ValueError, AttributeError):
        message = ""
    detail = " ".join(message.split())[:200]

    if status in (401, 403):
        return SpeechUnavailable("cloud_signin", detail or "Stonic Cloud did not accept this sign-in.")
    if status == 402:
        return SpeechUnavailable("cloud_no_plan", detail or "No active Stonic Cloud plan.")
    if status == 429:
        if message in ("too_many_requests", "queue_timeout"):
            return SpeechUnavailable("cloud_unavailable", "Too many requests at once.")
        # `resets_at` alag se: app isi se "kab khulega" likhti hai.
        return SpeechUnavailable("cloud_limit", f"resets_at={resets_at}" if resets_at else detail)
    if status == 422:
        return SpeechUnavailable("cloud_voice_rejected", "The voice model would not speak this line.")
    return SpeechUnavailable("cloud_unavailable", detail or f"Stonic Cloud returned {status}.")


class CloudSpeech:
    """Munh, Stonic Cloud par: text aur ada jati hai, awaaz wapas stream hoti hai."""

    def __init__(self, *, url: str, token: str, voice: str, transport: httpx.AsyncBaseTransport | None = None):
        self._voice = voice
        # `transport` sirf test ke liye (gateway ke baghair jawab dena).
        self._http = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(READ_TIMEOUT_SECS, connect=CONNECT_TIMEOUT_SECS),
            # httpx khali connection 5 second mein band kar deta hai; do jawabon ke
            # beech is se zyada waqfa aam hai, aur har jawab ka pehla tukra naye TLS
            # handshake ka intezar karta. Connection poori baat-cheet khula rahe.
            limits=httpx.Limits(keepalive_expiry=KEEP_ALIVE_SECS),
            transport=transport,
        )

    async def check(self) -> None:
        """Gateway tak connection garam kar lo: pehla jawab TLS ka intezar na kare.

        Plan aur login app pehle hi pooch chuki hoti hai (/v1/voice-beta/session),
        is liye yahan sirf connection khulta hai.
        """
        try:
            await self._http.get("/health")
        except httpx.HTTPError as error:
            logger.debug(f"Stonic Cloud: connection garam na ho saka ({error}) — pehli line par pata chalega")

    async def new_reply(self) -> None:
        return None

    async def speak(self, text: str, style: str) -> AsyncGenerator[bytes, None]:
        payload: dict[str, Any] = {"text": text, "voice": self._voice}
        if style:
            payload["style"] = style
        try:
            async with self._http.stream("POST", SPEECH_PATH, json=payload) as response:
                if response.status_code != 200:
                    raise describe_denial(response.status_code, await response.aread())
                got_audio = False
                async for chunk in response.aiter_bytes():
                    if chunk:
                        got_audio = True
                        yield chunk
                if not got_audio:
                    raise SpeechUnavailable("cloud_voice_failed", "Stonic Cloud returned no audio for this line.")
        except httpx.HTTPError as error:
            raise SpeechUnavailable("cloud_unavailable", f"{type(error).__name__}: {error}"[:200]) from error

    async def close(self) -> None:
        try:
            await self._http.aclose()
        except Exception:  # noqa: BLE001 — band hote waqt ki ghalti kisi kaam ki nahi
            pass


def listen_http_options(url: str) -> types.HttpOptions:
    """Kaan ka pata: Google ka SDK wohi rehta hai, sirf us ka base URL gateway
    par mora jata hai. SDK is ke aage Live ka apna raasta jorta hai aur token
    ko apni "key" samajh kar `x-goog-api-key` header mein bhejta hai."""
    return types.HttpOptions(base_url=f"{url}{LISTEN_PATH}")


def allow_plain_gateway(stt: Any, url: str) -> None:
    """Sirf development: `http://` gateway (apni machine par chalta hua).

    SDK Live ke liye hamesha `wss://` banata hai; local gateway par TLS nahi
    hota. Asli gateway `https://` hai aur ye function us par kuch nahi karta.
    """
    if not url.startswith("http://"):
        return
    api_client = stt._client._api_client
    api_client._websocket_base_url = lambda: f"{url.replace('http://', 'ws://', 1)}{LISTEN_PATH}"
    api_client._websocket_ssl_ctx = {}
