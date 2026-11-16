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

from stonic_voice.config import Ears, Voice, openrouter_claude
from stonic_voice.tts_gemini import classify_google_error

TIMEOUT_SECS = 15.0
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# List ke upar aane wale models, aur un ke saath ek lafz ki wazahat.
_NOTES: dict[str, dict[str, str]] = {
    "anthropic": {
        "claude-haiku-5-5": "Fastest",
        "claude-sonnet-5-5": "Balanced",
        "claude-opus-5-5": "Smartest",
    },
    # 7 Nov 2026: Gemini ab PC ka kaam bhi karta hai (gemini_relay.py). Muft key par Flash-Lite
    # ki din ki hadd ~500 requests, 3.8 Flash ki ~20 (Google publish nahi karta, 429 se naapi).
    "gemini": {
        "gemini-3.5-flash-lite": "Recommended for free keys",
        "gemini-3.8-flash": "Smarter, low free limit",
    },
    "groq": {
        "openai/gpt-oss-120b": "Recommended",
        "llama-3.3-70b-versatile": "Fast",
    },
    # OpenRouter par sirf Claude (OpenRouter ke naam, 14 Oct 2026 ki list se).
    "openrouter": {
        "anthropic/claude-haiku-5.5": "Recommended",
        "anthropic/claude-sonnet-5.5": "Balanced",
        "anthropic/claude-opus-5.5": "Smartest",
    },
}

# Gemini ke baat-cheet wale models: "gemini-3.8-flash", "gemini-3.5-flash-lite",
# "gemini-3.1-pro-preview", "gemini-flash-latest". Is key par awaaz, tasveer,
# live aur doosre kaamon ke darjanon models bhi aate hain — wo dimagh nahi hain.
_GEMINI_BRAIN = re.compile(r"gemini-(\d+(\.\d+)?-)?(flash|pro)(-lite)?(-latest|-preview(-[\d-]+)?)?")
# Groq par wo cheezein jo chat model nahi (sunna, bolna, pehra, agent systems).
_GROQ_NOT_A_BRAIN = ("whisper", "tts", "guard", "playai", "orpheus", "compound", "embed", "safeguard")


def _ordered(provider: str, models: list[dict[str, str]]) -> list[dict[str, str]]:
    notes = _NOTES[provider]
    for model in models:
        model["note"] = notes.get(model["id"], "")
    featured = [m for wanted in notes for m in models if m["id"] == wanted]
    rest = [m for m in models if m["id"] not in notes]
    return featured + rest


def _refused(provider: str, suffix: str, error: BaseException) -> dict[str, Any]:
    return {"ok": False, "code": "network" if suffix == "network" else f"{provider}_{suffix}", "message": " ".join(str(error).split())[:240]}


async def _anthropic_models(key: str) -> dict[str, Any]:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=TIMEOUT_SECS)
    try:
        page = await client.models.list(limit=100)
        models = [{"id": model.id, "label": model.display_name or model.id} for model in page.data]
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
        return _refused("gemini", {"auth": "key_invalid", "rate": "rate_limit", "daily": "daily_quota"}.get(kind, "failed"), error)
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


async def _groq_models(key: str) -> dict[str, Any]:
    import openai

    client = openai.AsyncOpenAI(api_key=key, base_url=GROQ_BASE_URL, max_retries=0, timeout=TIMEOUT_SECS)
    try:
        page = await client.models.list()
        models = [
            {"id": model.id, "label": model.id}
            for model in sorted(page.data, key=lambda item: item.id)
            if not any(tag in model.id.lower() for tag in _GROQ_NOT_A_BRAIN)
        ]
    except (openai.AuthenticationError, openai.PermissionDeniedError) as error:
        return _refused("groq", "key_invalid", error)
    except openai.RateLimitError as error:
        return _refused("groq", "rate_limit", error)
    except openai.APIConnectionError as error:
        return _refused("groq", "network", error)
    except openai.APIStatusError as error:
        return _refused("groq", "failed", error)
    finally:
        await client.close()
    return {"ok": True, "models": _ordered("groq", models)}


async def _openrouter_models(key: str) -> dict[str, Any]:
    """OpenRouter: pehle key ki jaanch (`/key` — muft), phir us par Claude ke models.

    Sirf Claude (14 Oct 2026, Usman): OpenRouter par Beta wohi Claude SDK chalata
    hai, aur wo sirf Claude ke saath pakka chalta hai. List ke "~…-latest" naam
    alias hain aur ":batch" baad mein chalne wale — dono baat-cheet ke kaam ke nahi.
    """
    import openai

    client = openai.AsyncOpenAI(api_key=key, base_url=OPENROUTER_BASE_URL, max_retries=0, timeout=TIMEOUT_SECS)
    try:
        await client.get("/key", cast_to=object)
        page = await client.models.list()
        models = []
        for model in page.data:
            extra = model.model_extra or {}
            modality = (extra.get("architecture") or {}).get("output_modalities") or ["text"]
            if not openrouter_claude(model.id) or model.id.endswith(":batch") or "text" not in modality:
                continue
            # "Anthropic: Claude Haiku 5.5" -> "Claude Haiku 5.5" (list mein sab Claude hi hain).
            models.append({"id": model.id, "label": str(extra.get("name") or model.id).removeprefix("Anthropic: ")})
        models.sort(key=lambda item: item["label"].lower())
    except (openai.AuthenticationError, openai.PermissionDeniedError) as error:
        return _refused("openrouter", "key_invalid", error)
    except openai.RateLimitError as error:
        return _refused("openrouter", "rate_limit", error)
    except openai.APIConnectionError as error:
        return _refused("openrouter", "network", error)
    except openai.APIStatusError as error:
        return _refused("openrouter", "no_credit" if error.status_code == 402 else "failed", error)
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
    if provider == "groq":
        return await _groq_models(key)
    if provider == "openrouter":
        return await _openrouter_models(key)
    return {"ok": False, "code": "bad_provider", "message": provider}
