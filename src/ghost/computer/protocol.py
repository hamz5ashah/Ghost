"""Provider, coordinate and OCR rules for the computer-use sidecar.

Pure standard library on purpose: this module is imported by Ghost's backend
*and* by the sidecar runner, which runs in its own venv (``gui-agents``,
``pyautogui``) where the rest of Ghost is not installed. Nothing here may
import from ``ghost``.

Ported from Stonic's ``stonic-computer`` (Phase 2 of ``/STONIC_PORT_PLAN.md``),
with Stonic's cloud gateway removed: every model call goes straight to a
provider with the user's own key.

Agent S3 needs two models:

* a **planner** that looks at the screen and decides the next action, and
* a **grounding** model that turns "the Save button" into pixel coordinates.

Grounding is the fragile half. ``gui-agents`` asks the model for "the
coordinate of one point" and reads the first two integers it sees as
``(x, y)``, then scales them by ``screen / grounding_size``. That only lands
on the right spot if Ghost knows which coordinate space the model answers in.
They differ:

* **UI-TARS** (via OpenRouter) answers in pixels of the screenshot it was
  sent. This is the path Stonic tuned and shipped.
* **Claude** answers in pixels of the image it *receives*. Anthropic downsizes
  large images, so screenshots are capped at 1280 px here so the image Claude
  sees is the one Ghost sent, and the pixels line up.
* **Gemini** answers in ``[y, x]`` order, normalized to 0-1000 — its trained
  convention. Read naively as pixel ``(x, y)`` every click would land
  transposed and mis-scaled, so grounding through Gemini asks for that
  convention explicitly and swaps the pair.
* **DeepSeek** has vision and can plan, but documents no pointing convention,
  so it is never used for grounding.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

PIXELS = "pixels"
NORMALIZED = "normalized"


@dataclass(frozen=True, slots=True)
class GroundingConvention:
    """How a model reports a point on a screenshot."""

    space: str  # PIXELS | NORMALIZED
    order: str  # "xy" | "yx"
    scale: int = 0  # NORMALIZED range, e.g. 1000; unused for PIXELS


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """One model provider the computer-use agent can call directly."""

    id: str
    label: str
    # Env var(s) Ghost's desktop shell injects from the OS keyring, in order.
    key_envs: Tuple[str, ...]
    # gui-agents ``engine_type``.
    engine_type: str
    # OpenAI-compatible endpoint; None where gui-agents uses a native SDK.
    base_url: Optional[str]
    default_planner_model: str
    # None = this provider must not be used for grounding.
    default_grounding_model: Optional[str]
    grounding: Optional[GroundingConvention]
    # Longest screenshot edge sent to this provider.
    screenshot_max_dim: int


PROVIDERS: Dict[str, ProviderSpec] = {
    "openrouter": ProviderSpec(
        id="openrouter",
        label="OpenRouter",
        key_envs=("OPENROUTER_API_KEY",),
        engine_type="open_router",
        base_url="https://openrouter.ai/api/v1",
        # Stonic's own BYOK defaults (computer-manager.js).
        default_planner_model="openai/gpt-5.6-luna",
        default_grounding_model="bytedance/ui-tars-1.5-7b",
        grounding=GroundingConvention(space=PIXELS, order="xy"),
        # Stonic's SCREENSHOT_MAX_DIM: UI-TARS is accurate at 1920x1080.
        screenshot_max_dim=1920,
    ),
    "anthropic": ProviderSpec(
        id="anthropic",
        label="Claude",
        key_envs=("ANTHROPIC_API_KEY",),
        engine_type="anthropic",
        base_url=None,
        default_planner_model="claude-sonnet-5-5",
        default_grounding_model="claude-sonnet-5-5",
        grounding=GroundingConvention(space=PIXELS, order="xy"),
        # Pre-downscale so the API never resizes the image under the model.
        screenshot_max_dim=1280,
    ),
    "gemini": ProviderSpec(
        id="gemini",
        label="Gemini",
        key_envs=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        engine_type="gemini",
        # gui-agents' Gemini engine is an OpenAI client and requires this.
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_planner_model="gemini-3.8-flash",
        default_grounding_model="gemini-3.8-flash",
        grounding=GroundingConvention(space=NORMALIZED, order="yx", scale=1000),
        screenshot_max_dim=1920,
    ),
    "deepseek": ProviderSpec(
        id="deepseek",
        label="DeepSeek",
        key_envs=("DEEPSEEK_API_KEY",),
        engine_type="openai",
        base_url="https://api.deepseek.com",
        # The vision-capable model; text-only models reject images with a 400.
        default_planner_model="deepseek-flash",
        default_grounding_model=None,
        grounding=None,
        screenshot_max_dim=1920,
    ),
}

# Auto-selection order when the user has not picked a provider.
PLANNER_PREFERENCE = ("anthropic", "gemini", "openrouter", "deepseek")
# UI-TARS first: it is the only grounding model Stonic validated end to end.
GROUNDING_PREFERENCE = ("openrouter", "anthropic", "gemini")


def provider(provider_id: str) -> ProviderSpec:
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        raise ValueError(
            f"unknown provider {provider_id!r}; expected one of "
            f"{', '.join(sorted(PROVIDERS))}"
        ) from None


def can_ground(provider_id: str) -> bool:
    spec = PROVIDERS.get(provider_id)
    return bool(spec and spec.grounding and spec.default_grounding_model)


def api_key_for(provider_id: str, env: Mapping[str, str]) -> str:
    """The user's key for a provider, or empty."""
    for name in provider(provider_id).key_envs:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """A resolved provider + model for one role."""

    provider: str
    model: str
    api_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AgentChoice:
    planner: ModelChoice
    grounding: ModelChoice

    @property
    def screenshot_max_dim(self) -> int:
        """The smaller cap of the two, so neither model is sent oversized images."""
        return min(
            provider(self.planner.provider).screenshot_max_dim,
            provider(self.grounding.provider).screenshot_max_dim,
        )


class ProviderError(ValueError):
    """No usable provider configuration. The message is shown to the user."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _split_spec(spec: str) -> Tuple[str, str]:
    """``"anthropic"`` or ``"anthropic:claude-opus-5-5"`` -> (provider, model)."""
    spec = (spec or "").strip()
    if not spec:
        return "", ""
    head, _, tail = spec.partition(":")
    return head.strip().lower(), tail.strip()


def choose(
    env: Mapping[str, str],
    planner_spec: str = "",
    grounding_spec: str = "",
) -> AgentChoice:
    """Pick planner and grounding models from what the user has keys for.

    An explicit spec (``provider`` or ``provider:model``) wins; otherwise the
    first provider in the preference order with a key is used. Raises
    :class:`ProviderError` with a user-facing message when nothing fits.
    """
    planner = _choose_role(env, planner_spec, PLANNER_PREFERENCE, "planner")
    grounding = _choose_role(env, grounding_spec, GROUNDING_PREFERENCE, "grounding")
    return AgentChoice(planner=planner, grounding=grounding)


def _choose_role(
    env: Mapping[str, str],
    spec: str,
    preference: Iterable[str],
    role: str,
) -> ModelChoice:
    wanted, model = _split_spec(spec)
    if wanted:
        provider(wanted)  # validates the id
        if role == "grounding" and not can_ground(wanted):
            raise ProviderError(
                f"{PROVIDERS[wanted].label} cannot locate things on screen, so it "
                "cannot be the grounding model. Pick Claude, Gemini or OpenRouter.",
                "grounding_unsupported",
            )
        key = api_key_for(wanted, env)
        if not key:
            raise ProviderError(
                f"Computer use is set to use {PROVIDERS[wanted].label}, but no "
                f"{PROVIDERS[wanted].label} API key is saved. Add it in "
                "Settings → Cloud providers.",
                "no_key",
            )
        return ModelChoice(wanted, model or _default_model(wanted, role), key)

    for candidate in preference:
        key = api_key_for(candidate, env)
        if key:
            return ModelChoice(candidate, _default_model(candidate, role), key)

    names = ", ".join(PROVIDERS[p].label for p in preference)
    raise ProviderError(
        f"Computer use needs an API key for the {role} model ({names}). "
        "Add one in Settings → Cloud providers.",
        "no_key",
    )


def _default_model(provider_id: str, role: str) -> str:
    spec = provider(provider_id)
    if role == "grounding":
        assert spec.default_grounding_model is not None
        return spec.default_grounding_model
    return spec.default_planner_model


def engine_params(choice: ModelChoice) -> Dict[str, object]:
    """``gui-agents`` engine params for one role (without grounding size)."""
    spec = provider(choice.provider)
    params: Dict[str, object] = {
        "engine_type": spec.engine_type,
        "model": choice.model,
        "api_key": choice.api_key,
        "temperature": None,
    }
    if spec.base_url:
        params["base_url"] = spec.base_url
    return params


def grounding_size(
    choice: ModelChoice, screenshot_w: int, screenshot_h: int
) -> Tuple[int, int]:
    """The coordinate space ``resize_coordinates`` must divide by.

    Pixel models answer in the screenshot's own size; normalized models answer
    in their fixed range regardless of the image.
    """
    convention = provider(choice.provider).grounding
    if convention is None:
        raise ValueError(f"{choice.provider} cannot ground")
    if convention.space == NORMALIZED:
        return convention.scale, convention.scale
    return screenshot_w, screenshot_h


def grounding_prompt(choice: ModelChoice, ref_expr: str) -> str:
    """The grounding request, phrased in the model's own convention.

    The pixel prompt is gui-agents' own, verbatim, so the UI-TARS path behaves
    exactly as Stonic shipped it.
    """
    convention = provider(choice.provider).grounding
    if convention is not None and convention.space == NORMALIZED:
        return (
            f"Query:{ref_expr}\n"
            "Point to the single location to act on. Answer with one point as "
            f"[y, x], normalized to 0-{convention.scale}, and nothing else.\n"
        )
    return (
        f"Query:{ref_expr}\nOutput only the coordinate of one point in your response.\n"
    )


_INT = re.compile(r"-?\d+")


def parse_point(choice: ModelChoice, response: str) -> List[int]:
    """Read one point from a grounding reply, returned as ``[x, y]``.

    Raises ``ValueError`` when the reply holds fewer than two numbers, which
    gui-agents treats as a failed step rather than a click at (0, 0).
    """
    numbers = [int(n) for n in _INT.findall(response or "")]
    if len(numbers) < 2:
        raise ValueError(f"no point in grounding reply: {response!r:.120}")
    first, second = numbers[0], numbers[1]
    convention = provider(choice.provider).grounding
    if convention is not None and convention.order == "yx":
        x, y = second, first
    else:
        x, y = first, second
    if convention is not None and convention.space == NORMALIZED:
        hi = convention.scale
        x = min(max(x, 0), hi)
        y = min(max(y, 0), hi)
    return [max(x, 0), max(y, 0)]


def scaled_size(width: int, height: int, max_dim: int) -> Tuple[int, int]:
    """Fit a screen into ``max_dim`` on its longest edge, keeping its aspect.

    Exactly Stonic's ``_scaled_size`` (truncating, not rounding) — the sizes it
    produced are the ones its clicks were validated against. A screen already
    within the cap is left as it is.
    """
    if width <= 0 or height <= 0:
        raise ValueError("screen size must be positive")
    factor = min(max_dim / width, max_dim / height, 1)
    return int(width * factor), int(height * factor)


# ---------------------------------------------------------------------------
# Tesseract OCR
# ---------------------------------------------------------------------------
#
# The Stonic bug this fixes: its runtime manifest claimed ``tesseract: true``
# but the installer shipped only the pytesseract *wrapper*. The runner then
# skipped configuration whenever no path was given, so pytesseract fell back to
# ``tesseract`` on PATH and failed only when an agent step needed OCR — deep in
# a task, as a stack trace. Here the binary is located up front, probed, and
# reported, so a missing OCR engine is visible in Settings before any task.

TESSERACT_EXE = "tesseract.exe" if sys.platform == "win32" else "tesseract"

# Where the UB Mannheim Windows installer puts it, per-machine and per-user.
_WINDOWS_INSTALL_DIRS = (
    r"C:\Program Files\Tesseract-OCR",
    r"C:\Program Files (x86)\Tesseract-OCR",
)


@dataclass(frozen=True, slots=True)
class TesseractLocation:
    path: Optional[str]
    tessdata: Optional[str]
    # explicit | bundled | path | system | missing
    source: str


def _is_file(path: str) -> bool:
    try:
        return bool(path) and Path(path).is_file()
    except OSError:
        return False


def _tessdata_for(exe: str, explicit: str, env: Mapping[str, str]) -> Optional[str]:
    if explicit and Path(explicit).is_dir():
        return explicit
    beside = Path(exe).parent / "tessdata"
    if (beside / "eng.traineddata").is_file():
        return str(beside)
    prefix = (env.get("TESSDATA_PREFIX") or "").strip()
    if prefix and Path(prefix).is_dir():
        return prefix
    return None


def locate_tesseract(
    explicit: str = "",
    tessdata: str = "",
    env: Optional[Mapping[str, str]] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    platform: str = sys.platform,
) -> TesseractLocation:
    """Find the Tesseract binary. Never raises.

    Order: an explicit path from Settings, the copy bundled with the desktop
    app (passed in as ``GHOST_TESSERACT``), ``PATH``, then the standard Windows
    install folders.
    """
    env = os.environ if env is None else env
    explicit = (explicit or "").strip()
    if explicit:
        if _is_file(explicit):
            return TesseractLocation(
                explicit, _tessdata_for(explicit, tessdata, env), "explicit"
            )
        # A configured-but-wrong path is reported as missing, not silently
        # replaced: the user asked for that binary.
        return TesseractLocation(None, None, "missing")

    bundled = (env.get("GHOST_TESSERACT") or "").strip()
    if _is_file(bundled):
        return TesseractLocation(
            bundled, _tessdata_for(bundled, tessdata, env), "bundled"
        )

    on_path = which("tesseract")
    if on_path:
        return TesseractLocation(on_path, _tessdata_for(on_path, tessdata, env), "path")

    if platform == "win32":
        local = env.get("LOCALAPPDATA", "")
        dirs = list(_WINDOWS_INSTALL_DIRS)
        if local:
            dirs.append(str(Path(local) / "Programs" / "Tesseract-OCR"))
        for directory in dirs:
            candidate = str(Path(directory) / "tesseract.exe")
            if _is_file(candidate):
                return TesseractLocation(
                    candidate, _tessdata_for(candidate, tessdata, env), "system"
                )

    return TesseractLocation(None, None, "missing")


@dataclass(slots=True)
class OcrStatus:
    available: bool
    source: str
    path: Optional[str] = None
    tessdata: Optional[str] = None
    version: str = ""
    languages: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def has_english(self) -> bool:
        return "eng" in self.languages

    def to_dict(self) -> Dict[str, object]:
        return {
            "available": self.available,
            "source": self.source,
            "path": self.path,
            "tessdata": self.tessdata,
            "version": self.version,
            "languages": list(self.languages),
            "has_english": self.has_english,
            "error": self.error,
        }


def _tesseract_env(
    location: TesseractLocation, base: Mapping[str, str]
) -> Dict[str, str]:
    env = dict(base)
    if location.tessdata:
        # Tesseract 4/5 read TESSDATA_PREFIX as the tessdata folder itself.
        env["TESSDATA_PREFIX"] = location.tessdata
    return env


def _hidden_window_flags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def probe_tesseract(
    location: TesseractLocation,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout: float = 15.0,
) -> OcrStatus:
    """Run the binary to prove OCR actually works, and list its languages.

    "Available" means the binary ran *and* has English data, because the
    agent's text-selection action OCRs in English. Never raises.
    """
    if not location.path:
        return OcrStatus(
            available=False,
            source=location.source,
            error=(
                "Tesseract OCR was not found. Text selection in computer use "
                "will not work until it is installed or bundled."
            ),
        )
    env = _tesseract_env(location, os.environ)
    try:
        version_run = run(
            [location.path, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            creationflags=_hidden_window_flags(),
        )
        langs_run = run(
            [location.path, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            creationflags=_hidden_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return OcrStatus(
            available=False,
            source=location.source,
            path=location.path,
            tessdata=location.tessdata,
            error=f"Tesseract would not start: {exc}"[:400],
        )

    # Tesseract prints its version to stdout or stderr depending on build.
    banner = (version_run.stdout or "") + (version_run.stderr or "")
    match = re.search(r"tesseract\s+v?(\d+(?:\.\d+)+)", banner, re.IGNORECASE)
    version = match.group(1) if match else ""
    languages = parse_languages((langs_run.stdout or "") + (langs_run.stderr or ""))

    if version_run.returncode != 0 or not version:
        return OcrStatus(
            available=False,
            source=location.source,
            path=location.path,
            tessdata=location.tessdata,
            error=f"Tesseract failed to run: {banner.strip()[:300]}",
        )
    status = OcrStatus(
        available="eng" in languages,
        source=location.source,
        path=location.path,
        tessdata=location.tessdata,
        version=version,
        languages=languages,
    )
    if not status.available:
        status.error = (
            "Tesseract runs but has no English language data (eng.traineddata)."
        )
    return status


def parse_languages(output: str) -> List[str]:
    """Parse ``tesseract --list-langs``.

    The first line is a header ("List of available languages in ...") and the
    rest are codes, one per line.
    """
    langs: List[str] = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of"):
            continue
        if re.fullmatch(r"[A-Za-z0-9_]+", line):
            langs.append(line)
    return sorted(set(langs))


# ---------------------------------------------------------------------------
# Sidecar environment
# ---------------------------------------------------------------------------

# What the runner reads. Keys are passed per role so planner and grounding can
# come from different providers.
ENV_PREFIX = "GHOST_COMPUTER_"


def sidecar_env(
    choice: AgentChoice,
    ocr: TesseractLocation,
    base: Mapping[str, str],
    max_trajectory: int = 8,
) -> Dict[str, str]:
    """Environment for the runner process.

    Starts from ``base`` minus every provider key, then adds back only the two
    the agent uses, so the sidecar never holds keys it has no business with.
    """
    all_key_names = {name for spec in PROVIDERS.values() for name in spec.key_envs}
    env = {k: v for k, v in base.items() if k not in all_key_names}
    env.update(
        {
            f"{ENV_PREFIX}PLANNER_PROVIDER": choice.planner.provider,
            f"{ENV_PREFIX}PLANNER_MODEL": choice.planner.model,
            f"{ENV_PREFIX}PLANNER_API_KEY": choice.planner.api_key,
            f"{ENV_PREFIX}GROUNDING_PROVIDER": choice.grounding.provider,
            f"{ENV_PREFIX}GROUNDING_MODEL": choice.grounding.model,
            f"{ENV_PREFIX}GROUNDING_API_KEY": choice.grounding.api_key,
            f"{ENV_PREFIX}SCREENSHOT_MAX_DIM": str(choice.screenshot_max_dim),
            f"{ENV_PREFIX}MAX_TRAJECTORY": str(max_trajectory),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    if ocr.path:
        env[f"{ENV_PREFIX}TESSERACT"] = ocr.path
    if ocr.tessdata:
        env[f"{ENV_PREFIX}TESSDATA"] = ocr.tessdata
        env["TESSDATA_PREFIX"] = ocr.tessdata
    return env


def fingerprint(choice: AgentChoice, ocr: TesseractLocation) -> str:
    """Identity of a sidecar configuration.

    The supervisor restarts the sidecar when this changes (a new key, model or
    OCR path). Keys are hashed, never stored or logged.
    """
    parts = [
        choice.planner.provider,
        choice.planner.model,
        hashlib.sha256(choice.planner.api_key.encode()).hexdigest()[:16],
        choice.grounding.provider,
        choice.grounding.model,
        hashlib.sha256(choice.grounding.api_key.encode()).hexdigest()[:16],
        ocr.path or "",
        ocr.tessdata or "",
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Errors, in words the user can act on
# ---------------------------------------------------------------------------
#
# Replaces Stonic's gateway messages ("Stonic Cloud sign-in expired", "plan
# renew karein"), which no longer apply: every failure here is the user's own
# provider account, and the fix is there too. The OpenRouter wording is
# Stonic's own BYOK text, re-pointed at Ghost's settings.

_PERMANENT_STATUS = frozenset({400, 401, 402, 403, 404})
_KEY_LIMIT_MARKERS = (
    "key limit exceeded",
    "limit exceeded for this key",
    "key spending limit",
)
_NO_ENDPOINT_MARKERS = ("no endpoints found", "data policy", "no allowed providers")
_NO_CREDIT_MARKERS = (
    "insufficient_balance",
    "insufficient balance",
    "credit balance",
    "billing",
    "quota",
)


_HOST_PROVIDERS = (
    ("openrouter.ai", "openrouter"),
    ("anthropic.com", "anthropic"),
    ("googleapis.com", "gemini"),
    ("deepseek.com", "deepseek"),
)


def provider_from_url(url: object, fallback: str = "") -> str:
    """Which provider a failed request went to, from its URL.

    Planner and grounding can be different providers, and an SDK error does not
    say which engine raised it — but it carries the request, whose host does.
    """
    text = str(url or "").lower()
    for host, provider_id in _HOST_PROVIDERS:
        if host in text:
            return provider_id
    return fallback


def is_permanent(status: Optional[int], body: str) -> bool:
    """Whether retrying a failed call is pointless.

    gui-agents retries every API error for 60 s; a bad key or an empty account
    will still be bad in 60 s, so those give up at once.
    """
    if status in _PERMANENT_STATUS:
        return True
    if status == 429:
        lowered = (body or "").lower()
        return any(m in lowered for m in _NO_CREDIT_MARKERS)
    return False


def explain_error(
    provider_id: str, status: Optional[int], body: str
) -> Tuple[Optional[str], Optional[str]]:
    """``(kind, message)`` for a failed model call; ``(None, None)`` if unknown."""
    if status is None:
        return None, None
    label = PROVIDERS[provider_id].label if provider_id in PROVIDERS else "The provider"
    lowered = (body or "").lower()

    if provider_id == "openrouter":
        if any(m in lowered for m in _KEY_LIMIT_MARKERS):
            return "key_limit", (
                "This OpenRouter API key has reached the spending limit set on it. "
                "Raise the key's limit on openrouter.ai (Settings → Keys), or use "
                "another key"
            )
        if any(m in lowered for m in _NO_ENDPOINT_MARKERS):
            return "model_unavailable", (
                "OpenRouter has no provider for the computer-control model with "
                "this account's privacy settings. Allow it on openrouter.ai "
                "(Settings → Privacy), then try again"
            )

    if status == 401:
        return "invalid_key", (
            f"{label} rejected the saved API key (wrong, deleted or disabled). "
            f"Paste a working {label} key in Settings → Cloud providers"
        )
    if status == 402 or (
        status == 429 and any(m in lowered for m in _NO_CREDIT_MARKERS)
    ):
        return "no_credits", (
            f"The {label} key works, but the account has no credit or quota left. "
            f"Add credit on the {label} account, then try again"
        )
    if status == 403:
        return "forbidden", (
            f"{label} refused the request (a safety, privacy or permission setting "
            "on the account). Check the account settings"
        )
    if status == 404:
        return "model_unavailable", (
            f"{label} does not offer the selected model to this key. Pick another "
            "model for computer use in Settings"
        )
    if status == 429:
        return "rate_limit", (
            f"{label} is rate-limiting this key right now. Nothing is wrong with "
            "the key — wait a minute and try again"
        )
    if status >= 500:
        return None, f"{label} did not answer. Try again in a moment"
    return None, None
