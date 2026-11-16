"""Pipeline ki ghalti ko wo shakal dena jo app user ko dikha sake.

Pipecat har ghalti ke saath ek qism (`ErrorCategory`) deta hai; yahan us mein
"kis hisse mein" (sunna / dimagh / bolna) aur "kis provider par" jurta hai, aur
ek chhota code banta hai — app usi code se apna paigham chunti hai, provider ke
angrezi matn se nahi.

    gemini_key_invalid     anthropic_no_credit     deepseek_rate_limit
    gemini_daily_quota     anthropic_model_not_found     network     ...

Ghost port: Stonic's gateway-only codes (cloud_signin, cloud_no_plan,
cloud_limit, cloud_unavailable) are gone with the gateway. Every failure here
is now the user's own provider account.
"""

from __future__ import annotations

import re
from typing import Any

from pipecat.frames.frames import ErrorFrame
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.errors import ErrorCategory

from ghost_voice.config import KEY_NAMES, Settings

# tts_gemini.py apni ghalti ke shuru mein code likhta hai: "ghost:gemini_rate_limit: ..."
_CODED = re.compile(r"ghost:([a-z_]+):\s*(.*)", re.DOTALL)

_TEXT_HINTS: list[tuple[str, tuple[str, ...]]] = [
    (
        "key_invalid",
        (
            "api key not valid",
            "api_key_invalid",
            "invalid x-api-key",
            "authentication_error",
            "invalid api key",
            "incorrect api key",
            "invalid_api_key",
            "unauthenticated",
            # OpenRouter: ghalat / mitayi hui key.
            "no auth credentials found",
            "user not found",
        ),
    ),
    # "billing" akela yahan nahi: Google ki aam rate-limit wali ghalti bhi
    # "check your plan and billing details" kehti hai.
    (
        "no_credit",
        (
            "credit balance is too low",
            "insufficient_quota",
            "insufficient credits",
            "insufficient balance",
            "payment required",
            "requires more credits",
        ),
    ),
    ("daily_quota", ("perday", "per day", "daily limit")),
    (
        "rate_limit",
        (
            "resource_exhausted",
            "rate_limit",
            "rate limit",
            "too many requests",
            "quota",
        ),
    ),
    (
        "model_not_found",
        (
            "not_found_error",
            "model_not_found",
            "is not found for api version",
            "does not exist",
            "unknown model",
        ),
    ),
    ("overloaded", ("overloaded", "unavailable", "internal server error")),
    (
        "network",
        (
            "getaddrinfo",
            "name resolution",
            "connection error",
            "connecterror",
            "timed out",
            "network is unreachable",
        ),
    ),
]

_CATEGORY_SUFFIX = {
    ErrorCategory.AUTHENTICATION: "key_invalid",
    ErrorCategory.AUTHORIZATION: "key_invalid",
    ErrorCategory.RATE_LIMIT: "rate_limit",
    ErrorCategory.QUOTA: "no_credit",
    ErrorCategory.CONNECTIVITY: "network",
    ErrorCategory.SERVER: "overloaded",
}

# In par dobara koshish bekaar hai: user ko Settings mein kuch badalna hoga.
PERMANENT = frozenset(
    {"key_invalid", "key_missing", "no_credit", "model_not_found", "daily_quota"}
)

CATEGORY_OF_CODE = {
    "key_invalid": ErrorCategory.AUTHENTICATION,
    "key_missing": ErrorCategory.AUTHENTICATION,
    "rate_limit": ErrorCategory.RATE_LIMIT,
    "daily_quota": ErrorCategory.QUOTA,
}


def _part(frame: ErrorFrame, settings: Settings) -> tuple[str, str]:
    """(hissa, provider): ghalti kahan hui. Sunna aur bolna hamesha Gemini par."""
    processor = frame.processor
    if isinstance(processor, STTService):
        return "ears", "gemini"
    if isinstance(processor, TTSService):
        return "voice", "gemini"
    if isinstance(processor, LLMService):
        return "brain", settings.brain.provider
    return "session", ""


def suffix_of(text: str, category: ErrorCategory | None = None) -> str:
    lowered = (text or "").lower()
    for suffix, hints in _TEXT_HINTS:
        if any(hint in lowered for hint in hints):
            return suffix
    return _CATEGORY_SUFFIX.get(category, "failed") if category else "failed"


def is_permanent(text: str) -> bool:
    """Is ghalti par dobara jurna bekaar hai? (ghalat key, credits khatam …)

    ears.py: Google ki line toote to kaan chup chaap dobara jurte hain — magar in
    wajahon par foran ruk kar user ko saaf paigham (dobara koshish se theek nahi hoti).
    """
    return suffix_of(text) in PERMANENT


def is_rate_limited(text: str) -> bool:
    """Key ki fi-minute / ek-saath hadd (ears.py: do lines ek saath na khulein to ek ek kar ke)."""
    return suffix_of(text) == "rate_limit"


def scrub(text: str, settings: Settings) -> str:
    """Provider ke paigham se chabi hata do (kuch providers ghalti mein key dohra dete hain)."""
    for name in KEY_NAMES:
        key = getattr(settings.keys, name)
        if key:
            text = text.replace(key, "<key>")
    return " ".join(text.split())[:300]


def describe(frame: ErrorFrame, settings: Settings) -> dict[str, Any]:
    """ErrorFrame -> app ke liye paigham."""
    text = str(frame.error or "")
    part, provider = _part(frame, settings)
    coded = _CODED.search(text)
    if coded:
        code, detail = coded.group(1), coded.group(2)
        suffix = code.split("_", 1)[1] if "_" in code else code
    else:
        suffix = suffix_of(text, frame.category)
        code = (
            "network"
            if suffix == "network"
            else f"{provider}_{suffix}"
            if provider
            else f"session_{suffix}"
        )
        detail = text
    return {
        "type": "ghost-error",
        "code": code,
        "part": part,
        "provider": provider,
        "permanent": suffix in PERMANENT,
        # Kaan ke baghair baat-cheet chal hi nahi sakti (sunne wali service
        # jur na saki ya toot gayi): session band, user dobara shuru kare.
        # Dimagh ya awaaz ki lamhe bhar ki ghalti agli baat par theek ho jati hai.
        "fatal": part == "ears",
        "detail": scrub(detail, settings),
    }
