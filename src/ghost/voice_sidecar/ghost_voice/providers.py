"""Provider se poochhna: ye chabi chalti hai? aur is par kaun se models hain?

App ki Settings ka "Check" button aur model ki list yahin se aati hai. Har
provider ki apni sarkari library se (anthropic, google-genai, openai), wohi jo
pipeline khud istemal karti hai — is liye jo yahan chale wohi baat-cheet mein
chalta hai.

Ye sawal muft hain: models ki list maangna kisi provider par bill nahi banata
aur awaaz ke requests ki hadd mein nahi ginta.

`anthropic` aur `openai` ki libraries sirf us provider ke "Check" par import
hoti hain, engine ke boot par nahi: dono mil kar ~1,500 modules hain (naapa,
20 Oct 2026 — boot ka ~5 s), aur ye sawal kabhi kabhar Settings se hi aata hai.
Google ki library sunne / bolne ke liye waise bhi load hoti hai.
"""

from __future__ import annotations

import re
from typing import Any

from google import genai
from google.genai import errors as genai_errors

from ghost_voice.config import Ears, Voice
from ghost_voice.tts_gemini import classify_google_error

TIMEOUT_SECS = 15.0
# Ghost: DeepSeek replaces Groq — it is a key Ghost's OS keyring manages
# (DEEPSEEK_API_KEY); Groq's is not.
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# List ke upar aane wale models, aur un ke saath ek lafz ki wazahat.
_NOTES: dict[str, dict[str, str]] = {
    "anthropic": {
        "claude-haiku-5-5": "Fastest",
        "claude-sonnet-5-5": "Balanced",
        "claude-opus-5-5": "Smartest",
    },
    # Muft key par Flash-Lite ki din ki hadd ~500 requests, 3.8 Flash ki ~20
    # (Google publish nahi karta, 429 se naapi).
    "gemini": {
        "gemini-3.5-flash-lite": "Recommended for free keys",
        "gemini-3.8-flash": "Smarter, low free limit",
    },
    "deepseek": {
        "deepseek-flash": "Recommended",
        "deepseek-v4-pro": "Smartest",
    },
    "openrouter": {
        "anthropic/claude-haiku-5.5": "Recommended",
        "anthropic/claude-sonnet-5.5": "Balanced",
        "anthropic/claude-opus-5.5": "Smartest",
    },
}

# Gemini ke baat-cheet wale models: "gemini-3.8-flash", "gemini-3.5-flash-lite",
# "gemini-3.1-pro-preview", "gemini-flash-latest". Is key par awaaz, tasveer,
# live aur doosre kaamon ke darjanon models bhi aate hain — wo dimagh nahi hain.
_GEMINI_BRAIN = re.compile(
    r"gemini-(\d+(\.\d+)?-)?(flash|pro)(-lite)?(-latest|-preview(-[\d-]+)?)?"
)
# Models on an OpenAI-compatible list that are not chat brains.
_NOT_A_BRAIN = ("whisper", "tts", "guard", "embed", "safeguard", "moderation")


def _ordered(provider: str, models: list[dict[str, str]]) -> list[dict[str, str]]:
    notes = _NOTES[provider]
    for model in models:
        model["note"] = notes.get(model["id"], "")
    featured = [m for wanted in notes for m in models if m["id"] == wanted]
    rest = [m for m in models if m["id"] not in notes]
    return featured + rest


def _refused(provider: str, suffix: str, error: BaseException) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "network" if suffix == "network" else f"{provider}_{suffix}",
        "message": " ".join(str(error).split())[:240],
    }


async def _anthropic_models(key: str) -> dict[str, Any]:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=TIMEOUT_SECS)
    try:
        page = await client.models.list(limit=100)
        models = [
            {"id": model.id, "label": model.display_name or model.id}
            for model in page.data
        ]
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as error:
        return _refused("anthropic", "key_invalid", error)
    except anthropic.RateLimitError as error:
        return _refused("anthropic", "rate_limit", error)
    except anthropic.APIConnectionError as error:
        return _refused("anthropic", "network", error)
    except anthropic.APIStatusError as error:
        return _refused("anthropic", "failed", error)
    finally:
        await client.close()
    return {"ok": True, "models": _ordered("anthropic", models)}


async def _gemini_models(key: str) -> dict[str, Any]:
    client = genai.Client(api_key=key)
    names: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    try:
        async for model in await client.aio.models.list():
            name = (model.name or "").removeprefix("models/")
            if name:
                names[name] = list(model.supported_actions or [])
                labels[name] = (model.display_name or "").strip() or name
    except genai_errors.APIError as error:
        kind = classify_google_error(error)
        return _refused(
            "gemini",
            {"auth": "key_invalid", "rate": "rate_limit", "daily": "daily_quota"}.get(
                kind, "failed"
            ),
            error,
        )
    except Exception as error:  # noqa: BLE001 — network ki ghalti library ke apne naam se nahi aati
        return _refused("gemini", "network", error)
    finally:
        try:
            await client.aio.aclose()
        except Exception:  # noqa: BLE001
            pass

    brains = [
        {"id": name, "label": labels[name]}
        for name, actions in sorted(names.items(), reverse=True)
        if _GEMINI_BRAIN.fullmatch(name) and "generateContent" in actions
    ]
    return {
        "ok": True,
        "models": _ordered("gemini", brains),
        # Sunna aur bolna isi key par hain: ye models na hon to baat-cheet nahi chalegi.
        "ears": Ears().model in names,
        "voice": Voice().model in names,
    }


async def _deepseek_models(key: str) -> dict[str, Any]:
    import openai

    client = openai.AsyncOpenAI(
        api_key=key, base_url=DEEPSEEK_BASE_URL, max_retries=0, timeout=TIMEOUT_SECS
    )
    try:
        page = await client.models.list()
        models = [
            {"id": model.id, "label": model.id}
            for model in sorted(page.data, key=lambda item: item.id)
            if not any(tag in model.id.lower() for tag in _NOT_A_BRAIN)
        ]
    except (openai.AuthenticationError, openai.PermissionDeniedError) as error:
        return _refused("deepseek", "key_invalid", error)
    except openai.RateLimitError as error:
        return _refused("deepseek", "rate_limit", error)
    except openai.APIConnectionError as error:
        return _refused("deepseek", "network", error)
    except openai.APIStatusError as error:
        return _refused(
            "deepseek", "no_credit" if error.status_code == 402 else "failed", error
        )
    finally:
        await client.close()
    return {"ok": True, "models": _ordered("deepseek", models)}


async def _openrouter_models(key: str) -> dict[str, Any]:
    """OpenRouter: pehle key ki jaanch (`/key` — muft), phir us par chat models.

    Stonic listed only Claude here, because its agent brain ran the Claude SDK
    and that only works with Claude. Ghost's engine is talk-only, so any text
    model will do. ":batch" models run later, not in a conversation.
    """
    import openai

    client = openai.AsyncOpenAI(
        api_key=key, base_url=OPENROUTER_BASE_URL, max_retries=0, timeout=TIMEOUT_SECS
    )
    try:
        await client.get("/key", cast_to=object)
        page = await client.models.list()
        models = []
        for model in page.data:
            extra = model.model_extra or {}
            modality = (extra.get("architecture") or {}).get("output_modalities") or [
                "text"
            ]
            if model.id.endswith(":batch") or "text" not in modality:
                continue
            models.append({"id": model.id, "label": str(extra.get("name") or model.id)})
        models.sort(key=lambda item: item["label"].lower())
    except (openai.AuthenticationError, openai.PermissionDeniedError) as error:
        return _refused("openrouter", "key_invalid", error)
    except openai.RateLimitError as error:
        return _refused("openrouter", "rate_limit", error)
    except openai.APIConnectionError as error:
        return _refused("openrouter", "network", error)
    except openai.APIStatusError as error:
        return _refused(
            "openrouter", "no_credit" if error.status_code == 402 else "failed", error
        )
    finally:
        await client.close()
    return {"ok": True, "models": _ordered("openrouter", models)}


async def list_models(provider: str, key: str) -> dict[str, Any]:
    """{ok: True, models: [{id, label, note}]} ya {ok: False, code, message}."""
    if not key:
        return {"ok": False, "code": f"{provider}_key_missing", "message": ""}
    if provider == "anthropic":
        return await _anthropic_models(key)
    if provider == "gemini":
        return await _gemini_models(key)
    if provider == "deepseek":
        return await _deepseek_models(key)
    if provider == "openrouter":
        return await _openrouter_models(key)
    return {"ok": False, "code": "bad_provider", "message": provider}
