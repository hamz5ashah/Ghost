"""Pipeline ki ghalti ko wo shakal dena jo app user ko dikha sake.

Pipecat har ghalti ke saath ek qism (`ErrorCategory`) deta hai; yahan us mein
"kis hisse mein" (sunna / dimagh / bolna) aur "kis provider par" jurta hai, aur
ek chhota code banta hai — app usi code se apna paigham chunti hai, provider ke
angrezi matn se nahi.

    gemini_key_invalid     anthropic_no_credit     groq_rate_limit
    gemini_daily_quota     anthropic_model_not_found     network     ...

Stonic Cloud par provider ka naam "cloud" hai aur ghaltiyan gateway ki apni hain:

    cloud_signin    login qabool nahi          cloud_no_plan   plan nahi / khatam
    cloud_limit     plan ki hadd (khud reset)  cloud_unavailable   gateway ya Google ka masla
"""

from __future__ import annotations

import re
from typing import Any

from pipecat.frames.frames import ErrorFrame
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.errors import ErrorCategory

from stonic_voice.config import CLOUD_PROVIDER, Settings

# tts_gemini.py apni ghalti ke shuru mein code likhta hai: "stonic:gemini_rate_limit: ..."
_CODED = re.compile(r"stonic:([a-z_]+):\s*(.*)", re.DOTALL)

_TEXT_HINTS: list[tuple[str, tuple[str, ...]]] = [
    # Gateway (Stonic Cloud) ke apne alfaaz — sab se pehle, kyunke un ke matn mein
    # "limit" aur "quota" jaise lafz bhi hote hain jo neeche kisi aur maani mein hain.
    # Plan ki hadd par gateway kaan ka socket apne code se band karta hai (4002),
    # aur hadd ka matn "... usage limit reached, quota resets at ..." hota hai.
    ("limit", ("usage limit reached", "stonic_rate_limit", "daily_limit", "monthly_limit", "plan_limit")),
    ("no_plan", ("insufficient_balance", "stonic_billing", "plan_unavailable", "subscription_")),
    ("signin", ("invalid_token", "account_inactive", "stonic_auth")),
    # Google ne hamari (gateway ki) key rad ki ya gateway adhoora laga hai: user ka qusoor nahi.
    ("unavailable", ("prepayment credits", "voice_beta_not_ready", "upstream_not_configured", "upstream_unavailable")),
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
    ("no_credit", ("credit balance is too low", "insufficient_quota", "insufficient credits", "payment required", "requires more credits")),
    ("daily_quota", ("perday", "per day", "daily limit")),
    ("rate_limit", ("resource_exhausted", "rate_limit", "rate limit", "too many requests", "quota")),
    ("model_not_found", ("not_found_error", "model_not_found", "is not found for api version", "does not exist", "unknown model")),
    ("overloaded", ("overloaded", "unavailable", "internal server error")),
    ("network", ("getaddrinfo", "name resolution", "connection error", "connecterror", "timed out", "network is unreachable")),
]

_CATEGORY_SUFFIX = {
    ErrorCategory.AUTHENTICATION: "key_invalid",
    ErrorCategory.AUTHORIZATION: "key_invalid",
    ErrorCategory.RATE_LIMIT: "rate_limit",
    ErrorCategory.QUOTA: "no_credit",
    ErrorCategory.CONNECTIVITY: "network",
    ErrorCategory.SERVER: "overloaded",
}

# In par dobara koshish bekaar hai: user ko Settings mein kuch badalna hoga
# (ya, Cloud ki hadd par, us ke khulne ka intezar).
PERMANENT = frozenset(
    {"key_invalid", "key_missing", "no_credit", "model_not_found", "daily_quota", "signin", "no_plan", "limit"}
)
# Gateway ke alfaaz sirf Stonic Cloud par maani rakhte hain.
_CLOUD_ONLY = frozenset({"limit", "no_plan", "signin", "unavailable"})

CATEGORY_OF_CODE = {
    "key_invalid": ErrorCategory.AUTHENTICATION,
    "key_missing": ErrorCategory.AUTHENTICATION,
    "rate_limit": ErrorCategory.RATE_LIMIT,
    "daily_quota": ErrorCategory.QUOTA,
}


def _part(frame: ErrorFrame, settings: Settings) -> tuple[str, str]:
    """(hissa, provider): ghalti kahan hui."""
    processor = frame.processor
    google = CLOUD_PROVIDER if settings.is_cloud else "gemini"
    if isinstance(processor, STTService):
        return "ears", google
    if isinstance(processor, TTSService):
        return "voice", google
    if isinstance(processor, LLMService):
        return "brain", settings.brain.provider
    return "session", ""


def _suffix(text: str, category: ErrorCategory | None, cloud: bool = False) -> str:
    lowered = text.lower()
    for suffix, hints in _TEXT_HINTS:
        if suffix in _CLOUD_ONLY and not cloud:
            continue
        if any(hint in lowered for hint in hints):
            return suffix
    return _CATEGORY_SUFFIX.get(category, "failed") if category else "failed"


def is_permanent(text: str, *, cloud: bool) -> bool:
    """Is ghalti par dobara jurna bekaar hai? (ghalat key, plan ki hadd, credits khatam …)

    ears.py: Google ki line toote to kaan chup chaap dobara jurte hain — magar in
    wajahon par foran ruk kar user ko saaf paigham (dobara koshish se theek nahi hoti).
    """
    return _suffix(text, None, cloud=cloud) in PERMANENT


def is_rate_limited(text: str, *, cloud: bool) -> bool:
    """Key ki fi-minute / ek-saath hadd (ears.py: do lines ek saath na khulein to ek ek kar ke)."""
    return _suffix(text, None, cloud=cloud) == "rate_limit"


def scrub(text: str, settings: Settings) -> str:
    """Provider ke paigham se chabi hata do (kuch providers ghalti mein key dohra dete hain)."""
    secrets = [getattr(settings.keys, name) for name in ("gemini", "gemini_backup", "anthropic", "groq", "openrouter")]
    for key in [*secrets, settings.cloud.token]:
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
        suffix = _suffix(text, frame.category, cloud=settings.is_cloud)
        code = "network" if suffix == "network" else f"{provider}_{suffix}" if provider else f"session_{suffix}"
        detail = text
    return {
        "type": "stonic-error",
        "code": code,
        "part": part,
        "provider": provider,
        "permanent": suffix in PERMANENT,
        # Kaan ke baghair baat-cheet chal hi nahi sakti (sunne wali service
        # jur na saki ya toot gayi): session band, user dobara shuru kare.
        # Dimagh ya awaaz ki lamhe bhar ki ghalti agli baat par theek ho jati hai.
        # Agent ka dimagh ek zinda process hai: wo hi na rahe ("engine_…") to
        # baat-cheet aage nahi chal sakti.
        "fatal": part == "ears" or code.startswith("engine_"),
        "detail": scrub(detail, settings),
    }
