"""Gemini ka pul: Claude Code (Agent SDK) ki zubaan -> Google Gemini API.

Muft Gemini key wale users ke liye (7 Nov 2026, Usman): dimagh wohi Claude Code
harness rehta hai — wohi loop, wohi tools (PowerShell, files, screen ke haath),
wohi pehra (safety.py) — sirf us ke peeche model Gemini hai.

    Claude Code CLI ──Anthropic Messages──> yeh pul (isi engine ka port) ──Gemini REST──> Google
                    <──Anthropic SSE──────                              <──SSE─────────

Google ka koi Anthropic-jaisa raasta nahi, is liye tarjuma yahan hota hai. Teen
hifazatein:

  - User ki Gemini key kabhi CLI ke mahaul mein nahi jati. CLI ko sirf ek "ticket"
    milta hai (ANTHROPIC_AUTH_TOKEN); key isi process ki yaad mein rehti hai.
  - Pul sirf 127.0.0.1 par (engine ka hi server) aur ticket ke baghair 401.
  - Koi third-party proxy nahi (LiteLLM ke do PyPI version malware nikle the, Mar 2026).

Gemini 3 ke do qaide jo yahan nibhaye jate hain:

  - Thought signature: model ki har function call ke saath ek dastakhat aata hai jo
    agli request mein usi call ke saath wapas jana lazmi hai (warna 400). Dastakhat
    tool_use id ke saath yaad rakha jata hai; na mile (engine dobara utha) to Google
    ka bataya hua bypass lafz.
  - Function response ka naam wohi jo call ka tha: tool_result mein sirf id hota hai,
    naam usi request ki pichli tool_use se milta hai.

Muft key ki haddein (Google numbers publish nahi karta, 429 se naape, Sept 2026):
3.5 Flash-Lite ~15 requests / minute, ~500 / din. Minute ki hadd par pul khud
Google ke bataye waqt tak rukta hai (kaam dheema, magar chalta rehta hai); din ki
hadd par saaf angrezi paigham (Classic ka Gemini Live wahi user ko sunata hai).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
# Module ki satah par: `from __future__ import annotations` ke saath FastAPI `request: Request`
# ko isi module ke naamon se pehchanta hai (function ke andar import = "query param" samajhta).
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

# Pul ka raasta engine ke server par: CLI isay ANTHROPIC_BASE_URL samajhta hai aur
# aage khud "/v1/messages" lagata hai.
PATH = "/relay/gemini"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
# Google ke docs (Gemini 3, thought signatures): jab pichli call ka dastakhat
# hamare paas na ho (doosre model / purani baat), ye lafz validation chhor deta hai.
BYPASS_SIGNATURE = "skip_thought_signature_validator"
# Sirf aakhri itni tasveerein model ko jati hain; purani ki jagah ek line. Muft key
# ki fi-minute token hadd aur raftaar dono ke liye (har tasveer ~1,100 tokens).
KEEP_IMAGES = 3
OLD_IMAGE = "[An earlier screenshot was here.]"
MAX_OUTPUT_TOKENS = 65_536
DEFAULT_OUTPUT_TOKENS = 8_192
# Fi-minute hadd par itni der tak khud intezar (Google ka `retryDelay`), phir CLI ko 429.
MAX_WAIT_SECS = 65.0
WAIT_STEP_CAP_SECS = 30.0
# Google ke 5xx / 503 par dobara koshish (waqfa, second).
SERVER_RETRIES = (2.0, 5.0)
# Model ne toti phooti function call di (MALFORMED_FUNCTION_CALL) ya khali jawab: itni dafa dobara.
BAD_REPLY_RETRIES = 2
# Har call ki yaad (tool_use id -> Gemini ki call id + thought signature). Ek kaam ~40 qadam.
MAX_CALLS = 4096
REQUEST_TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=60.0, pool=15.0)
THINKING_LEVELS = ("low", "medium", "high")
# Google (Flash-Lite ka safha): tool chalane wale agent mein soch "medium" ya "high" —
# kam par model kaam beech mein chhor deta hai. Is liye Flash-Lite par "Low" = medium.
LITE_MIN_THINKING = "medium"

# Gemini function ke naam ka qaida: declaration . : - bhi leta hai magar FunctionCall /
# FunctionResponse ka naam sirf harf, adad, _ aur - (Google docs, Nov 2026) — is liye
# sab se mehfooz: harf / _ se shuru, phir harf, adad, _ ; 64 tak. Hamare naam
# ("mcp__computer__left_click", "PowerShell") is mein aa jate hain — baqi ke liye chhota pakka naam.
_NAME_OK = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# JSON Schema ke wo lafz jo Gemini (`parametersJsonSchema`) samajhta hai. Baqi
# chup chaap hata diye jate hain — anjaan lafz par Google poori request rad kar deta.
_SCHEMA_KEYS = frozenset({
    "type", "description", "enum", "items", "properties", "required", "additionalProperties",
    "anyOf", "oneOf", "format", "minimum", "maximum", "minItems", "maxItems", "minLength",
    "maxLength", "title", "nullable", "prefixItems", "$ref", "$defs", "propertyOrdering",
})
# Wo finishReason jin par Google ne jawab rok diya (pehre ki wajah se).
_BLOCKED = frozenset({"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "LANGUAGE"})
_BAD_CALL = frozenset({"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "TOO_MANY_TOOL_CALLS", "MISSING_THOUGHT_SIGNATURE", "MALFORMED_RESPONSE"})
# CLI ka apna taaruf system prompt mein ("You are Claude Code…") — Gemini ko nahi
# jata, warna wo khud ko Claude batata hai. Stonic ka apna prompt us ke baad hai.
_CLI_IDENTITY = ("you are claude code", "you are a claude agent", "x-anthropic-billing-header")


# ── Ticket: CLI ke paas key nahi, sirf ye ─────────────────────────────────────


@dataclass(frozen=True)
class Route:
    key: str
    model: str
    thinking: str = "low"


_port = 0
_routes: dict[str, Route] = {}
_tickets: dict[tuple[str, str, str], str] = {}


def set_port(port: int) -> None:
    """Engine ka asli port (server.py socket kholte hi batata hai)."""
    global _port
    _port = int(port)


def base_url() -> str:
    """CLI ka ANTHROPIC_BASE_URL. Port na ho = engine server ke baghair chal raha hai (ghalti)."""
    if not _port:
        raise RuntimeError("Gemini relay: engine ka port abhi maloom nahi")
    return f"http://127.0.0.1:{_port}{PATH}"


def ticket_for(key: str, model: str, thinking: str = "low") -> str:
    """Is key + model + soch ka ticket (wohi teen = wohi ticket; key isi process mein rehti hai)."""
    if not key:
        raise ValueError("Gemini relay: key khali")
    level = thinking if thinking in THINKING_LEVELS else "low"
    ident = (key, model, level)
    ticket = _tickets.get(ident)
    if ticket is None:
        ticket = f"stonic-relay-{secrets.token_urlsafe(32)}"
        _tickets[ident] = ticket
        _routes[ticket] = Route(key=key, model=model, thinking=level)
    return ticket


def route_for(headers: Any) -> Route | None:
    """Request ke ticket (Authorization: Bearer … ya x-api-key) se raasta. Ghalat = None."""
    given = ""
    auth = str(headers.get("authorization") or "")
    if auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    if not given:
        given = str(headers.get("x-api-key") or "").strip()
    if not given:
        return None
    for ticket, route in _routes.items():
        if hmac.compare_digest(ticket.encode(), given.encode()):
            return route
    return None


def forget_all() -> None:
    """Sab tickets aur calls ki yaad mita do (sirf tests)."""
    _routes.clear()
    _tickets.clear()
    _calls.clear()


def thinking_level(route: Route) -> str:
    """Gemini ka `thinkingLevel` (Flash-Lite par kam se kam medium — upar LITE_MIN_THINKING)."""
    if "flash-lite" in route.model.lower() and route.thinking == "low":
        return LITE_MIN_THINKING
    return route.thinking


# ── Har call ki yaad: Gemini ki call id + thought signature ───────────────────
#
# Gemini 3 har functionCall ke saath ek `id` deta hai; functionResponse mein wohi id
# lautana lazmi (na ho to Google chup chaap khali jawab deta hai). Aur pehli call ka
# dastakhat agli request mein usi call ke saath. CLI sirf apna tool_use id rakhta hai,
# is liye dono yahan us id ke saath yaad rehte hain.

_calls: OrderedDict[str, tuple[str, str]] = OrderedDict()


def _remember_call(tool_id: str, call_id: str, signature: str) -> None:
    _calls[tool_id] = (call_id, signature)
    _calls.move_to_end(tool_id)
    while len(_calls) > MAX_CALLS:
        _calls.popitem(last=False)


def _call_id(tool_id: str) -> str:
    """Gemini ki call id (yaad ho to), warna CLI ka apna id — call aur jawab dono par ek hi."""
    known = _calls.get(tool_id)
    return known[0] if known and known[0] else tool_id


# ── Request: Anthropic -> Gemini ─────────────────────────────────────────────


def gemini_name(name: str) -> str:
    """Tool ka naam Gemini ke qaide mein (jo pehle se theek ho wohi rehta hai)."""
    if _NAME_OK.fullmatch(name):
        return name
    slug = re.sub(r"[^A-Za-z0-9_]", "_", name)[:40] or "tool"
    return f"t_{hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]}_{slug}"[:64]


def clean_schema(node: Any) -> Any:
    """JSON Schema ko Gemini ke samajhne wale lafzon tak (anjaan lafz par Google 400 deta hai)."""
    if isinstance(node, list):
        return [clean_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "const":
            out.setdefault("enum", [value])
        elif key == "exclusiveMinimum" and isinstance(value, (int, float)) and not isinstance(value, bool):
            out.setdefault("minimum", value)
        elif key == "exclusiveMaximum" and isinstance(value, (int, float)) and not isinstance(value, bool):
            out.setdefault("maximum", value)
        elif key not in _SCHEMA_KEYS:
            continue
        elif key in ("properties", "$defs") and isinstance(value, dict):
            out[key] = {name: clean_schema(schema) for name, schema in value.items()}
        elif key in ("items", "additionalProperties"):
            out[key] = clean_schema(value) if isinstance(value, dict) else value
        elif key in ("anyOf", "oneOf", "prefixItems") and isinstance(value, list):
            out[key] = [clean_schema(item) for item in value]
        else:
            out[key] = value
    return out


def _tool_schema(raw: Any) -> dict[str, Any]:
    schema = clean_schema(raw) if isinstance(raw, dict) else {}
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": schema.get("properties") or {}}
    schema.setdefault("properties", {})
    return schema


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        blocks = [system]
    elif isinstance(system, list):
        blocks = [str(block.get("text") or "") for block in system if isinstance(block, dict) and block.get("type") == "text"]
    else:
        blocks = []
    kept = [text for text in blocks if text.strip() and not text.strip().lower().startswith(_CLI_IDENTITY)]
    return "\n\n".join(kept)


def _image_count(messages: list[Any]) -> int:
    total = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                total += 1
            elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                total += sum(1 for inner in block["content"] if isinstance(inner, dict) and inner.get("type") == "image")
    return total


_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


class _Images:
    """Sirf aakhri KEEP_IMAGES tasveerein; purani ki jagah ek line. Har tasveer ka apna naam
    (`displayName`) — function ke jawab mein tasveer isi naam se `$ref` hoti hai (Google docs)."""

    def __init__(self, total: int, keep: int) -> None:
        self._skip = max(0, total - keep)
        self._seen = 0

    def part(self, block: dict[str, Any]) -> dict[str, Any] | None:
        self._seen += 1
        if self._seen <= self._skip:
            return {"text": OLD_IMAGE}
        source = block.get("source") if isinstance(block.get("source"), dict) else {}
        if source.get("type") == "base64" and source.get("data"):
            mime = str(source.get("media_type") or "image/png")
            name = f"image-{self._seen}.{_EXT.get(mime, 'img')}"
            return {"inlineData": {"mimeType": mime, "data": str(source["data"]), "displayName": name}}
        if source.get("type") == "url" and source.get("url"):
            return {"text": f"[Image: {source['url']}]"}
        return None


def _result_parts(block: dict[str, Any], images: _Images) -> tuple[str, list[dict[str, Any]]]:
    """tool_result ka text aur us ki tasveerein (Gemini parts)."""
    content = block.get("content")
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    pictures: list[dict[str, Any]] = []
    for inner in content if isinstance(content, list) else []:
        if not isinstance(inner, dict):
            continue
        if inner.get("type") == "text":
            texts.append(str(inner.get("text") or ""))
        elif inner.get("type") == "image":
            part = images.part(inner)
            if part is None:
                continue
            if "text" in part:
                texts.append(part["text"])
            else:
                pictures.append(part)
        elif inner.get("type") == "document":
            texts.append("[A document was attached here.]")
    return "\n".join(text for text in texts if text), pictures


def _document_part(block: dict[str, Any]) -> dict[str, Any] | None:
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    if source.get("type") == "base64" and source.get("data"):
        return {"inlineData": {"mimeType": str(source.get("media_type") or "application/pdf"), "data": str(source["data"])}}
    if source.get("type") == "text" and source.get("data"):
        return {"text": str(source["data"])}
    return None


def _fold(answers: list[dict[str, Any]], texts: list[str]) -> None:
    """Function ke jawab wale turn ki baqi tehreer (CLI ki yaad-dihani waghera) aakhri jawab ke
    aakhir mein — Google: alag text part mein nahi, jawab ke text ke baad do nayi lines se."""
    joined = "\n\n".join(text for text in texts if text)
    if not answers or not joined:
        return
    response = answers[-1]["functionResponse"]["response"]
    field_name = "error" if "error" in response else "output"
    response[field_name] = f"{response.get(field_name) or ''}\n\n{joined}".strip()


def build_contents(messages: list[Any], *, keep_images: int = KEEP_IMAGES) -> list[dict[str, Any]]:
    """Anthropic `messages` -> Gemini `contents` (user / model, baari baari).

    Google ke qaide (Nov 2026 docs): parallel calls ek model turn mein, un ke saare jawab
    agle EK user turn mein, usi tarteeb se; jawab ki tasveer jawab ke andar; aakhri turn
    model ka na ho (prefill par 400).
    """
    names: dict[str, str] = {}
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "assistant" and isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    names[str(block.get("id") or "")] = gemini_name(str(block.get("name") or ""))
    images = _Images(_image_count(messages), keep_images)
    # Pehle har paigham -> (role, jawab, baqi parts); phir ek hi role wale saath.
    turns: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = "model" if message.get("role") == "assistant" else "user"
        content = message.get("content")
        answers: list[dict[str, Any]] = []
        parts: list[dict[str, Any]] = []
        if isinstance(content, str):
            if content:
                parts.append({"text": content})
        else:
            first_call = True
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    if block.get("text"):
                        parts.append({"text": str(block["text"])})
                elif kind == "image":
                    part = images.part(block)
                    if part is not None:
                        parts.append(part)
                elif kind == "document":
                    part = _document_part(block)
                    if part is not None:
                        parts.append(part)
                elif kind == "tool_use" and role == "model":
                    tool_id = str(block.get("id") or "")
                    call: dict[str, Any] = {
                        "functionCall": {
                            "id": _call_id(tool_id),
                            "name": gemini_name(str(block.get("name") or "")),
                            "args": block.get("input") if isinstance(block.get("input"), dict) else {},
                        },
                    }
                    known = _calls.get(tool_id)
                    if known and known[1]:
                        call["thoughtSignature"] = known[1]
                    elif first_call:
                        # Google: har qadam ki PEHLI call par dastakhat lazmi; hamare paas na ho to bypass.
                        call["thoughtSignature"] = BYPASS_SIGNATURE
                    first_call = False
                    parts.append(call)
                elif kind == "tool_result":
                    text, pictures = _result_parts(block, images)
                    tool_id = str(block.get("tool_use_id") or "")
                    response: dict[str, Any] = {"error": text or "The tool failed."} if block.get("is_error") else {"output": text}
                    answer: dict[str, Any] = {"functionResponse": {"id": _call_id(tool_id), "name": names.get(tool_id, "tool"), "response": response}}
                    if pictures:
                        # Gemini 3: tasveer function ke jawab ke andar (multimodal function response).
                        for number, picture in enumerate(pictures, 1):
                            response["image" if len(pictures) == 1 else f"image_{number}"] = {"$ref": picture["inlineData"]["displayName"]}
                        answer["functionResponse"]["parts"] = pictures
                    answers.append(answer)
                # thinking / redacted_thinking: Claude ki soch, Gemini ke kaam ki nahi.
        if not answers and not parts:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["answers"].extend(answers)
            turns[-1]["parts"].extend(parts)
        else:
            turns.append({"role": role, "answers": answers, "parts": parts})
    contents: list[dict[str, Any]] = []
    for turn in turns:
        answers, parts = turn["answers"], turn["parts"]
        if answers:
            _fold(answers, [part["text"] for part in parts if set(part) == {"text"}])
            parts = [part for part in parts if set(part) != {"text"}]
        contents.append({"role": turn["role"], "parts": answers + parts})
    if contents and contents[0]["role"] != "user":
        contents.insert(0, {"role": "user", "parts": [{"text": "Continue."}]})
    if contents and contents[-1]["role"] == "model":
        # Gemini 3.5+: aakhri turn model ka (prefill) = 400.
        contents.append({"role": "user", "parts": [{"text": "Continue."}]})
    return contents


def build_request(body: dict[str, Any], route: Route) -> dict[str, Any]:
    """Poori Anthropic request -> Gemini `generateContent` ki body."""
    payload: dict[str, Any] = {"contents": build_contents(list(body.get("messages") or []))}
    system = _system_text(body.get("system"))
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    declarations = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        if tool.get("type") not in (None, "custom"):
            continue  # Anthropic ke server tools (web search waghera) Gemini par nahi
        declarations.append({
            "name": gemini_name(str(tool["name"])),
            "description": str(tool.get("description") or "")[:8000],
            "parametersJsonSchema": _tool_schema(tool.get("input_schema")),
        })
    if declarations:
        payload["tools"] = [{"functionDeclarations": declarations}]
        choice = body.get("tool_choice") if isinstance(body.get("tool_choice"), dict) else {}
        mode = {"any": "ANY", "tool": "ANY", "none": "NONE"}.get(str(choice.get("type") or ""), "AUTO")
        calling: dict[str, Any] = {"mode": mode}
        if choice.get("type") == "tool" and choice.get("name"):
            calling["allowedFunctionNames"] = [gemini_name(str(choice["name"]))]
        payload["toolConfig"] = {"functionCallingConfig": calling}
    try:
        wanted = int(body.get("max_tokens") or DEFAULT_OUTPUT_TOKENS)
    except (TypeError, ValueError):
        wanted = DEFAULT_OUTPUT_TOKENS
    generation: dict[str, Any] = {
        "maxOutputTokens": max(256, min(wanted, MAX_OUTPUT_TOKENS)),
        "thinkingConfig": {"thinkingLevel": thinking_level(route)},
    }
    stops = [str(stop) for stop in (body.get("stop_sequences") or []) if str(stop)][:5]
    if stops:
        generation["stopSequences"] = stops
    # temperature / top_p jaan boojh kar nahi: Gemini 3.5+ inhein nazar-andaz karta hai aur
    # aage ke models 400 denge (Google docs).
    payload["generationConfig"] = generation
    return payload


def estimate_tokens(body: dict[str, Any]) -> int:
    """Muft andaza (CLI ka `count_tokens`): ~4 huroof = 1 token, har tasveer ~1,100.

    Google se ginti nahi karwate — har sawal muft key ki hadd mein se kat-ta.
    """
    images = _image_count(list(body.get("messages") or []))
    text = json.dumps({key: body.get(key) for key in ("system", "messages", "tools")}, ensure_ascii=False)
    # Tasveer ka base64 bhi text mein gina gaya: us ka andaza alag se.
    data = sum(len(match) for match in re.findall(r'"data":\s*"([^"]{200,})"', text))
    return max(1, (len(text) - data) // 4 + images * 1_100)


# ── Google ki ghaltiyan ──────────────────────────────────────────────────────


class RelayError(Exception):
    """Ek ghalti jo CLI ko Anthropic ki shakal mein milti hai."""

    def __init__(self, status: int, kind: str, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message
        self.retry_after = retry_after

    def body(self) -> dict[str, Any]:
        return {"type": "error", "error": {"type": self.kind, "message": self.message}}


def _delay_secs(value: Any) -> float | None:
    match = re.fullmatch(r"\s*([\d.]+)s\s*", str(value or ""))
    return float(match.group(1)) if match else None


@dataclass
class GoogleError:
    status: int
    code: str = ""          # "RESOURCE_EXHAUSTED", "INVALID_ARGUMENT" …
    message: str = ""
    reasons: list[str] = field(default_factory=list)   # "API_KEY_INVALID" …
    quotas: list[str] = field(default_factory=list)    # "GenerateRequestsPerDayPerProjectPerModel-FreeTier" …
    retry_delay: float | None = None

    @property
    def daily(self) -> bool:
        text = " ".join(self.quotas + [self.message]).lower()
        return "perday" in text or "per day" in text

    @property
    def free_tier(self) -> bool:
        return any("freetier" in quota.lower() for quota in self.quotas) or "free_tier" in self.message.lower()


def parse_google_error(status: int, raw: bytes | str) -> GoogleError:
    error = GoogleError(status=status)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        error.message = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or ""))[:400]
        return error
    if isinstance(data, list) and data:
        data = data[0]
    inner = data.get("error") if isinstance(data, dict) else None
    if not isinstance(inner, dict):
        return error
    error.code = str(inner.get("status") or "")
    error.message = " ".join(str(inner.get("message") or "").split())[:600]
    for detail in inner.get("details") or []:
        if not isinstance(detail, dict):
            continue
        kind = str(detail.get("@type") or "")
        if kind.endswith("ErrorInfo") and detail.get("reason"):
            error.reasons.append(str(detail["reason"]))
        elif kind.endswith("QuotaFailure"):
            for violation in detail.get("violations") or []:
                if isinstance(violation, dict) and violation.get("quotaId"):
                    error.quotas.append(str(violation["quotaId"]))
        elif kind.endswith("RetryInfo"):
            error.retry_delay = _delay_secs(detail.get("retryDelay"))
    return error


def relay_error(error: GoogleError, model: str) -> RelayError:
    """Google ki ghalti -> CLI ke liye Anthropic ki ghalti, saaf angrezi mein.

    Jin par dobara koshish bekaar hai (key, din ki hadd, model) wo 400 hain: CLI 400
    par dobara nahi bhejta aur matn jaisa ka taisa aage deta hai — Classic mein Gemini
    Live wahi user ko sunata hai. Google ke apne lafz ("API key not valid", "PerDay")
    saath rehte hain, taake app ka ghalti ka code (errors.py) bhi sahi bane.
    """
    said = error.message or f"HTTP {error.status}"
    if error.status == 429:
        if error.daily:
            where = "free daily limit" if error.free_tier else "daily limit"
            return RelayError(
                400, "invalid_request_error",
                f"Gemini's {where} for {model} is used up on this API key (Google: GenerateRequestsPerDay). "
                "It resets at midnight Pacific time. Until then PC tasks need another brain in Settings → AI Engine "
                "(Claude, OpenRouter, or a Gemini key on a paid Google project).",
            )
        return RelayError(429, "rate_limit_error", f"Gemini is rate-limiting this API key right now (too many requests per minute). {said}", retry_after=error.retry_delay or 10.0)
    if "API_KEY_INVALID" in error.reasons or "api key not valid" in said.lower() or error.status in (401, 403) and "key" in said.lower():
        return RelayError(400, "invalid_request_error", f"Google rejected the Gemini API key (API key not valid, deleted or blocked). Check it in Settings → AI Engine. {said}")
    if error.status == 404 or "not found" in said.lower():
        return RelayError(400, "invalid_request_error", f"The Gemini model {model} is not available on this API key (model_not_found). Choose another Gemini model in Settings → AI Engine. {said}")
    if error.status == 402:
        return RelayError(400, "invalid_request_error", f"The Google project behind this Gemini key has no prepaid credit left (insufficient credits). Add credit in Google AI Studio, or use another brain. {said}")
    if error.status == 403:
        return RelayError(400, "invalid_request_error", f"Google refused this Gemini API key ({error.code or 'PERMISSION_DENIED'}). {said}")
    if "location is not supported" in said.lower():
        return RelayError(400, "invalid_request_error", f"Google's free Gemini API is not offered in this country or region. A paid Google project or another brain is needed. {said}")
    if error.status >= 500:
        return RelayError(529, "overloaded_error", f"Google's Gemini service is busy or unavailable right now (overloaded). {said}")
    return RelayError(400, "invalid_request_error", f"Gemini refused the request ({error.code or error.status}): {said}")


# ── Google se baat ───────────────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
    return _client


async def close() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _gemini_url() -> str:
    """Google ka pata. Sirf tests (naqli Google) ke liye mahaul se badal sakta hai — aur sirf
    isi PC ke pate par, taake koi mahaul ki setting user ki key kisi aur server par na bhej sake."""
    override = os.environ.get("STONIC_VOICE_GEMINI_URL", "").strip().rstrip("/")
    if override.startswith(("http://127.0.0.1:", "http://localhost:")):
        return override
    return GEMINI_URL


async def _open(route: Route, payload: dict[str, Any]) -> httpx.Response:
    """Google ka stream kholo. Minute ki hadd / 5xx par khud ruk kar dobara; baqi ghalti RelayError."""
    url = f"{_gemini_url()}/models/{route.model}:streamGenerateContent?alt=sse"
    headers = {"x-goog-api-key": route.key, "content-type": "application/json"}
    waited = 0.0
    server_tries = 0
    while True:
        request = _http().build_request("POST", url, headers=headers, json=payload)
        try:
            response = await _http().send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise RelayError(529, "overloaded_error", "Google's Gemini service did not answer in time (timed out).") from exc
        except httpx.HTTPError as exc:
            raise RelayError(529, "overloaded_error", f"Could not reach Google's Gemini service (connection error: {type(exc).__name__}). Check the internet connection.") from exc
        if response.status_code == 200:
            return response
        raw = await response.aread()
        await response.aclose()
        error = parse_google_error(response.status_code, raw)
        logger.warning(f"Gemini relay: Google {response.status_code} {error.code} {error.quotas or error.reasons} — {error.message[:200]}")
        if response.status_code == 429 and not error.daily:
            pause = min(max(error.retry_delay or 5.0, 1.0), WAIT_STEP_CAP_SECS)
            if waited + pause <= MAX_WAIT_SECS:
                logger.info(f"Gemini relay: fi-minute hadd — {pause:.0f} s ruk kar dobara")
                await asyncio.sleep(pause)
                waited += pause
                continue
        if response.status_code >= 500 and server_tries < len(SERVER_RETRIES):
            await asyncio.sleep(SERVER_RETRIES[server_tries])
            server_tries += 1
            continue
        raise relay_error(error, route.model)


async def _chunks(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Google ke SSE ki har `data:` line ek GenerateContentResponse."""
    try:
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            text = line[5:].strip()
            if not text or text == "[DONE]":
                continue
            try:
                chunk = json.loads(text)
            except ValueError:
                logger.debug(f"Gemini relay: parhi na ja saki line — {text[:120]}")
                continue
            if isinstance(chunk, dict):
                yield chunk
    finally:
        await response.aclose()


@dataclass
class Event:
    kind: str                      # "text" | "tool" | "end"
    text: str = ""
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    signature: str = ""
    call_id: str = ""              # tool: Gemini ki apni call id
    stop: str = ""                 # end: Anthropic ka stop_reason
    usage: dict[str, int] = field(default_factory=dict)


def _usage(meta: Any) -> dict[str, int]:
    if not isinstance(meta, dict):
        return {}
    out_tokens = int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0)
    return {"input_tokens": int(meta.get("promptTokenCount") or 0), "output_tokens": out_tokens}


async def events(route: Route, payload: dict[str, Any], names: dict[str, str]) -> AsyncIterator[Event]:
    """Gemini ka jawab, Anthropic ke tukron mein: text, tool calls, aakhir.

    Toti function call ya khali jawab par dobara (BAD_REPLY_RETRIES). Agar pehli
    koshish ka text CLI tak ja chuka ho to dobara wali koshish ka sirf tool call liya
    jata hai (text do dafa na likha jaye).
    """
    said_text = False
    for attempt in range(BAD_REPLY_RETRIES + 1):
        response = await _open(route, payload)
        finish = ""
        usage: dict[str, int] = {}
        calls = 0
        produced = False
        blocked = ""
        async for chunk in _chunks(response):
            usage = _usage(chunk.get("usageMetadata")) or usage
            feedback = chunk.get("promptFeedback") if isinstance(chunk.get("promptFeedback"), dict) else {}
            if feedback.get("blockReason"):
                blocked = str(feedback["blockReason"])
            for candidate in (chunk.get("candidates") or [])[:1]:
                if not isinstance(candidate, dict):
                    continue
                finish = str(candidate.get("finishReason") or finish)
                content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
                for part in content.get("parts") or []:
                    if not isinstance(part, dict) or part.get("thought"):
                        continue
                    call = part.get("functionCall")
                    if isinstance(call, dict) and call.get("name"):
                        calls += 1
                        produced = True
                        name = str(call["name"])
                        yield Event(
                            "tool", name=names.get(name, name), args=call.get("args") if isinstance(call.get("args"), dict) else {},
                            signature=str(part.get("thoughtSignature") or ""), call_id=str(call.get("id") or ""),
                        )
                    elif part.get("text") and not (said_text and attempt > 0):
                        produced = True
                        said_text = True
                        yield Event("text", text=str(part["text"]))
        if blocked or finish in _BLOCKED:
            reason = blocked or finish
            logger.warning(f"Gemini relay: Google ne jawab roka ({reason})")
            yield Event("text", text=f"\n\n(Google's safety filter stopped this reply: {reason}.)")
            yield Event("end", stop="end_turn", usage=usage)
            return
        if finish in _BAD_CALL and not calls and attempt < BAD_REPLY_RETRIES:
            logger.info(f"Gemini relay: {finish} — dobara koshish ({attempt + 1})")
            continue
        if not produced and finish not in _BAD_CALL and attempt == 0:
            # Bilkul khali jawab (na lafz, na call): ek dafa dobara. Phir bhi khali = khali.
            logger.info(f"Gemini relay: khali jawab ({finish or 'no finishReason'}) — dobara koshish")
            continue
        if finish in _BAD_CALL and not calls:
            yield Event("text", text="\n\n(Gemini could not form the next action. Ask again to continue.)")
        stop = "tool_use" if calls else ("max_tokens" if finish == "MAX_TOKENS" else "end_turn")
        yield Event("end", stop=stop, usage=usage)
        return


# ── Jawab: Gemini -> Anthropic ───────────────────────────────────────────────


def _sse(kind: str, data: dict[str, Any]) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _tool_id() -> str:
    return f"toolu_{secrets.token_hex(12)}"


def _message_id() -> str:
    return f"msg_{secrets.token_hex(12)}"


async def stream_messages(route: Route, body: dict[str, Any], first: Event | None, rest: AsyncIterator[Event]) -> AsyncIterator[str]:
    """Anthropic ka SSE (message_start … message_stop) — CLI isi ko parhta hai."""
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": _message_id(), "type": "message", "role": "assistant", "model": str(body.get("model") or route.model),
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": estimate_tokens(body), "output_tokens": 1},
        },
    })
    index = -1
    open_text = False

    async def all_events() -> AsyncIterator[Event]:
        if first is not None:
            yield first
        async for event in rest:
            yield event

    try:
        async for event in all_events():
            if event.kind == "text":
                if not open_text:
                    index += 1
                    open_text = True
                    yield _sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": event.text}})
            elif event.kind == "tool":
                if open_text:
                    yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})
                    open_text = False
                index += 1
                tool_id = _tool_id()
                if event.signature or event.call_id:
                    _remember_call(tool_id, event.call_id, event.signature)
                yield _sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": tool_id, "name": event.name, "input": {}}})
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": json.dumps(event.args, ensure_ascii=False)}})
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})
            elif event.kind == "end":
                if open_text:
                    yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})
                    open_text = False
                usage = {"output_tokens": event.usage.get("output_tokens", 0)}
                if event.usage.get("input_tokens"):
                    usage["input_tokens"] = event.usage["input_tokens"]
                yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": event.stop, "stop_sequence": None}, "usage": usage})
                yield _sse("message_stop", {"type": "message_stop"})
                return
    except RelayError as error:
        # Jawab shuru ho chuka tha (dobara koshish mein ghalti): SSE ki apni ghalti.
        yield _sse("error", error.body())
    except Exception as error:  # noqa: BLE001 — stream adhoora na chhoro: CLI ko saaf ghalti
        logger.exception("Gemini relay: stream toot gaya")
        yield _sse("error", {"type": "error", "error": {"type": "api_error", "message": f"Gemini relay failed ({type(error).__name__})."}})


async def collect_message(route: Route, body: dict[str, Any], first: Event | None, rest: AsyncIterator[Event]) -> dict[str, Any]:
    """Bina stream wali request: wohi jawab ek JSON mein."""
    content: list[dict[str, Any]] = []
    stop = "end_turn"
    usage = {"input_tokens": estimate_tokens(body), "output_tokens": 0}

    async def all_events() -> AsyncIterator[Event]:
        if first is not None:
            yield first
        async for event in rest:
            yield event

    async for event in all_events():
        if event.kind == "text":
            if content and content[-1]["type"] == "text":
                content[-1]["text"] += event.text
            else:
                content.append({"type": "text", "text": event.text})
        elif event.kind == "tool":
            tool_id = _tool_id()
            if event.signature or event.call_id:
                _remember_call(tool_id, event.call_id, event.signature)
            content.append({"type": "tool_use", "id": tool_id, "name": event.name, "input": event.args})
        elif event.kind == "end":
            stop = event.stop
            usage.update({key: value for key, value in event.usage.items() if value})
    return {
        "id": _message_id(), "type": "message", "role": "assistant", "model": str(body.get("model") or route.model),
        "content": content, "stop_reason": stop, "stop_sequence": None, "usage": usage,
    }


def name_map(body: dict[str, Any]) -> dict[str, str]:
    """Gemini ka naam -> asal tool naam (jo CLI ne bheja)."""
    out: dict[str, str] = {}
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("name"):
            out[gemini_name(str(tool["name"]))] = str(tool["name"])
    return out


async def handle_messages(route: Route, body: dict[str, Any]) -> tuple[str, Any]:
    """("stream", async iterator) | ("json", dict) | ("error", RelayError).

    Pehla tukra stream shuru karne SE PEHLE liya jata hai: Google ki ghalti (key,
    hadd) CLI ko asli HTTP status ke saath milti hai, adhoore stream mein nahi.
    """
    started = time.monotonic()
    payload = build_request(body, route)
    stream = events(route, payload, name_map(body))
    try:
        first: Event | None = await stream.__anext__()
    except StopAsyncIteration:
        first = None
    except RelayError as error:
        return "error", error
    logger.debug(f"Gemini relay: pehla tukra {time.monotonic() - started:.1f} s mein ({route.model})")
    if body.get("stream"):
        return "stream", stream_messages(route, body, first, stream)
    return "json", await collect_message(route, body, first, stream)


# ── Engine ke server par raaste (server.py jorta hai) ────────────────────────


def install(app: Any) -> None:
    """FastAPI app par pul ke raaste. Ticket ki jaanch server.py ke middleware mein hai."""

    @app.post(f"{PATH}/v1/messages")
    async def relay_messages(request: Request):
        route = route_for(request.headers)
        if route is None:
            return JSONResponse(RelayError(401, "authentication_error", "Unknown relay ticket.").body(), status_code=401)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(RelayError(400, "invalid_request_error", "Body is not JSON.").body(), status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(RelayError(400, "invalid_request_error", "Body must be an object.").body(), status_code=400)
        try:
            kind, result = await handle_messages(route, body)
        except Exception as error:  # noqa: BLE001 — CLI ko hamesha Anthropic ki shakal ki ghalti
            logger.exception("Gemini relay: request gir gayi")
            return JSONResponse(RelayError(500, "api_error", f"Gemini relay failed ({type(error).__name__}).").body(), status_code=500)
        if kind == "error":
            headers = {"retry-after": str(int(result.retry_after))} if result.retry_after else None
            return JSONResponse(result.body(), status_code=result.status, headers=headers)
        if kind == "stream":
            return StreamingResponse(result, media_type="text/event-stream", headers={"cache-control": "no-cache"})
        return JSONResponse(result)

    @app.post(f"{PATH}/v1/messages/count_tokens")
    async def relay_count(request: Request):
        if route_for(request.headers) is None:
            return JSONResponse(RelayError(401, "authentication_error", "Unknown relay ticket.").body(), status_code=401)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        return {"input_tokens": estimate_tokens(body if isinstance(body, dict) else {})}
