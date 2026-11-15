"""Screen ke haath, dimagh ke tools ki shakal mein.

Beta ka dimagh (agent.py — Claude) screen par wohi karta hai jo PowerShell ke
saath karta hai: khud. Koi doosra agent nahi jise kaam "diya" jaye — jo awaaz
baat kar rahi hai wohi screen dekhti hai, wohi click karti hai, aur beech mein
bolti bhi rehti hai. Ye file us ke haath (hands.py) ko tools banati hai.

Nishana teen tarah lagta hai (`aim`):

    claude   Claude tasveer dekh kar pixel khud batata hai (`coordinate`).
    grid     Wohi, magar jagah tasveer par 0–1000 ke jaal mein — muft Gemini key
             wala dimagh (gemini_relay.py). Gemini isi shakal par sikhaya gaya hai.
    uitars   Claude cheez ko lafzon mein batata hai (`target`), aur UI-TARS —
             ek chhota model jo sirf isi kaam par sikhaya gaya hai — us ki
             jagah dhoondta hai (aim.py). Dimagh ek hi rehta hai; UI-TARS sirf
             us ki nishane wali aankh hai.

Tools ke naam aur arguments wohi hain jo Anthropic ke computer tool ke hain
(screenshot, left_click, type, key, scroll, zoom…): Claude inhi par sikhaya
gaya hai.

Ek qadam mein kai kaam: dimagh ek hi jawab mein click, type aur Enter bhej
sakta hai. Tasveer sirf aakhri kaam ke saath wapas jati hai — beech wale kaam
ruke baghair chalte hain aur har kaam ke baad "ab screen dekho" ka alag chakkar
nahi lagta.

Rokna: user mouse hilaye to agla kaam nahi hota aur dimagh ko bataya jata hai
ke control user ne le liya; Stop / "ruk jao" / Esc chalta kaam wahin rok dete hain.
User bolna shuru kare to haath thehar jate hain (us ka jumla kaam badal sakta hai).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Protocol

from claude_agent_sdk import tool
from loguru import logger

from stonic_voice import host
from stonic_voice.aim import AimError, Aimer
from stonic_voice.config import ScreenLimits
from stonic_voice.hands import (
    MAX_KEY_REPEAT,
    MAX_SCROLL,
    MAX_WAIT_SECS,
    Hands,
    HandsError,
    HandsStopped,
    mouse_taken,
    Shot,
    UserTookMouse,
    modifier_keys,
    parse_keys,
    to_fraction,
)

SERVER = "computer"
PREFIX = f"mcp__{SERVER}__"
AIMS = ("claude", "uitars", "grid")
# "grid" par har jagah is jaal mein: [0, 0] tasveer ka upar-baayan kona, [GRID, GRID] neeche-daayan.
GRID = 1000

# tool -> (mouse ka button, kitni dafa, screen par dikhne wala lafz)
CLICKS: dict[str, tuple[str, int, str]] = {
    "left_click": ("left", 1, "Clicking"),
    "right_click": ("right", 1, "Right-clicking"),
    "double_click": ("left", 2, "Double-clicking"),
    "triple_click": ("left", 3, "Selecting"),
}
_LOOKS = ("screenshot", "zoom")

class _NotLooked(HandsError):
    """Is jawab mein dimagh ne screen dekhi hi nahi aur pixel de diya (pichhli baar ki yaad se)."""


NOT_LOOKED = (
    "NOT DONE: you have not looked at the screen since they last spoke, and it may have changed. "
    "This is the screen right now — take the position from this picture and send the action again."
)
TOOK_MOUSE = (
    "NOT DONE: they moved the mouse themselves, so the controls are theirs again. Leave the screen alone now. "
    "Tell them in one short line where you stopped, and carry on only when they ask."
)
MOUSE_TAKEN = (
    "Stonic's other screen agent is using the mouse and keyboard right now (a task given from chat or from the phone), "
    "and two cannot drive at once. Tell them, and try again once that task has finished."
)
NOT_EXECUTED = "Not executed: an earlier screen action in this step did not go through."
STOPPED = "Stopped."
# User lagatar bolta rahe (ya mic shor ko awaaz samjhe) to bhi haath is se zyada nahi thehrte.
HOLD_MAX_SECS = 20.0


class TurnView(Protocol):
    """Dimagh ke chalte jawab ka wo hissa jo yahan chahiye (agent.py AgentBrain)."""

    def current_tool_id(self) -> str | None: ...
    def message_seq(self) -> int: ...
    def later_tool_names(self, tool_id: str | None) -> list[str] | None: ...


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def label_for(name: str, args: dict[str, Any]) -> str:
    """"Abhi kya ho raha hai" — screen ke liye ek chhoti line. Likhi jane wali tehreer nahi dikhti."""
    short = name[len(PREFIX):] if name.startswith(PREFIX) else name
    target = _clip(args.get("target") or args.get("start_target") or "", 48)
    if short == "screenshot":
        return "Looking at the screen"
    if short == "zoom":
        return "Looking closer"
    if short in CLICKS:
        return f"{CLICKS[short][2]} {target}".strip()
    if short == "mouse_move":
        return f"Pointing at {target}".strip() if target else "Moving the pointer"
    if short == "left_click_drag":
        return f"Dragging {target}".strip()
    if short == "scroll":
        return f"Scrolling {_clip(args.get('scroll_direction') or 'down', 8)}"
    if short == "type":
        return "Typing"
    if short == "key":
        return f"Pressing {_clip(args.get('text') or '', 30)}".strip()
    if short == "wait":
        return "Waiting for the screen"
    return short.replace("_", " ").capitalize()


# ── Tools ka naqsha ──────────────────────────────────────────────────────────

_HELD = {"type": "string", "description": "Keys to hold meanwhile: shift, ctrl, alt or win (join with +)."}
_PIXEL = {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def tool_specs(aim: str) -> list[tuple[str, str, dict[str, Any]]]:
    """(naam, kaam, arguments) — chune hue nishane ke mutabiq."""
    pointing = aim in ("claude", "grid")
    # Jagah ki zubaan: Claude pixel, Gemini 0–1000 ka jaal.
    unit = f"on the 0-{GRID} grid over the latest screenshot" if aim == "grid" else "in pixels of the latest screenshot"
    if pointing:
        where = {"coordinate": {**_PIXEL, "description": f"[x, y] {unit}."}}
        what = {"type": "string", "description": 'What it is, in a few words ("Save button").'}
        need = ["coordinate", "target"]
        start = {"start_coordinate": {**_PIXEL, "description": f"[x, y] where the drag begins, {unit}."}}
        start_need = ["start_coordinate"]
    else:
        where = {}
        what = {
            "type": "string",
            "description": (
                "The one element, described so that it cannot be mistaken: what it is, its label or icon, and where it sits "
                '("the blue Save button at the bottom right of the Save As dialog"). A pointing model finds it from these words alone.'
            ),
        }
        need = ["target"]
        start = {"start_target": {"type": "string", "description": "The element where the drag begins, described the same way."}}
        start_need = ["start_target"]

    spot = {**where, "target": what}
    specs: list[tuple[str, str, dict[str, Any]]] = [
        (
            "screenshot",
            "See the screen as it is right now."
            + (f" Positions you give to the other screen tools are on a 0-{GRID} grid over this picture ([0, 0] top-left, [{GRID}, {GRID}] bottom-right)."
               if aim == "grid" else " Positions you give to the other screen tools are pixels in this picture." if pointing else ""),
            _object({}, []),
        ),
        (
            "zoom",
            "See one part of the screen enlarged, to read small text or tell icons apart.",
            _object({"region": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                                "description": f"[x0, y0, x1, y1]: two opposite corners, {unit}."}}, ["region"]),
        ),
        ("left_click", "Click something with the left mouse button.", _object({**spot, "text": _HELD}, need)),
        ("double_click", "Double-click something (open a file or folder, select a word).", _object({**spot, "text": _HELD}, need)),
        ("right_click", "Right-click something to open its menu.", _object({**spot, "text": _HELD}, need)),
        ("triple_click", "Triple-click something to select the whole line or paragraph.", _object({**spot, "text": _HELD}, need)),
        ("mouse_move", "Move the pointer onto something without clicking (to reveal a tooltip or a hover menu).", _object(spot, need)),
        (
            "left_click_drag",
            "Press the left button on one thing, drag it to another and release.",
            _object({**start, **spot, "text": _HELD}, [*start_need, *need]),
        ),
        (
            "scroll",
            "Turn the mouse wheel. Name a place to scroll one particular list or panel.",
            _object({
                "scroll_direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "scroll_amount": {"type": "integer", "minimum": 1, "maximum": MAX_SCROLL, "description": "Wheel notches. 3 is about a third of a page."},
                **spot,
                "text": _HELD,
            }, ["scroll_direction"]),
        ),
        (
            "type",
            "Type text where the cursor is, in any language. Click the field first. A new line in the text presses Enter.",
            _object({"text": {"type": "string"}}, ["text"]),
        ),
        (
            "key",
            'Press a key or a shortcut: "Return", "Escape", "ctrl+s", "alt+Tab", "win". Separate several presses with spaces ("Down Down Return").',
            _object({"text": {"type": "string"}, "repeat": {"type": "integer", "minimum": 1, "maximum": MAX_KEY_REPEAT}}, ["text"]),
        ),
        (
            "wait",
            "Wait while the screen finishes something (an app opening, a page loading), then see it.",
            _object({"duration": {"type": "number", "minimum": 0.2, "maximum": MAX_WAIT_SECS, "description": "Seconds."}}, ["duration"]),
        ),
    ]
    return specs


def tool_names(aim: str) -> list[str]:
    return [name for name, _description, _schema in tool_specs(aim)]


# ── Haath + nishana + dimagh ka jor ──────────────────────────────────────────


class ScreenTools:
    """Ek baat-cheet ke screen wale tools."""

    def __init__(
        self,
        *,
        aim: str,
        hands: Hands,
        limits: ScreenLimits,
        aimer: Aimer | None = None,
        max_steps: int = 0,
        emit: Callable[..., None] = host.emit,
    ) -> None:
        if aim not in AIMS:
            raise ValueError(f"aim '{aim}' — {', '.join(AIMS)} mein se ek")
        self.aim = aim
        self._hands = hands
        self._aimer = aimer
        self._limits = limits
        self._max_steps = max_steps
        self._emit = emit
        self._turn: TurnView | None = None
        # Aakhri poori tasveer ka naap jo dimagh ne dekhi: us ke pixel isi mein hain.
        self._view: tuple[int, int] | None = None
        self._active = False          # is jawab mein screen ka kaam shuru ho chuka
        self._step = 0
        self._task = ""
        self._halt = False            # user ne mouse le liya: jab tak wo na bole, screen nahi chherni
        self._broken_seq: int | None = None  # is jawab ka koi kaam nakaam hua: us ke baqi kaam nahi chalte
        self._epoch = 0               # har "roko" par badalta hai
        self._settle_at = 0.0
        self._hold_until = 0.0        # user bol raha hai: is waqt tak naya click / tehreer nahi

    # ── Dimagh ki taraf se ──────────────────────────────────────────────────

    def bind(self, turn: TurnView) -> None:
        self._turn = turn

    def sdk_tools(self) -> list[Any]:
        return [self._sdk_tool(name, description, schema) for name, description, schema in tool_specs(self.aim)]

    def _sdk_tool(self, name: str, description: str, schema: dict[str, Any]) -> Any:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return await self.call(name, dict(args or {}))

        return tool(name, description, schema)(handler)

    def user_spoke(self, text: str = "") -> None:
        """User bola: mouse wala taala khula, aur ye jumla HUD par "kaam" ke taur par dikhta hai."""
        self._halt = False
        if text.strip():
            self._task = _clip(text, 200)

    def user_speaking(self, speaking: bool) -> None:
        """User bolna shuru kare to haath thehar jate hain, jaise insaan baat sunne ko rukta hai.

        Wo jo keh raha hai wo kaam rok ya badal sakta hai — is liye us ka jumla
        poora hone tak koi naya click ya tehreer nahi. Jumla pohnchte hi (agent_service.py)
        kaam ya to wahin kat jata hai, ya (sirf "hmm / theek" ho to) haath khud chal parte hain.
        """
        now = time.monotonic()
        if speaking:
            self._hold_until = now + HOLD_MAX_SECS
        else:
            self._hold_until = min(self._hold_until, now + self._limits.hold_after_ms / 1000)

    def stop(self) -> None:
        """Chalta kaam roko (Stop, "ruk jao", Esc)."""
        self._epoch += 1
        self._hands.cancel()

    def turn_ended(self, aborted: bool) -> None:
        if self._active:
            status = "stopped" if aborted else ("interrupted" if self._halt else "done")
            self._notify("end", status=status, steps=self._step)
        self._active = False
        self._step = 0
        self._view = None
        self._broken_seq = None

    async def close(self, *, cancel_hands: bool = True) -> None:
        """`cancel_hands=False`: haath (sab baat-cheeton ke shared) ko mat roko —
        sirf is ki apni ginti band. Us dimagh ke liye jis ne kabhi kaam shuru hi nahi kiya."""
        if cancel_hands:
            self.stop()
        else:
            self._epoch += 1
        self.turn_ended(aborted=True)
        if self._aimer is not None:
            await self._aimer.close()

    # ── App ko khabar (HUD, glow, Stonic ki window ka hatna) ────────────────

    def _notify(self, phase: str, **fields: Any) -> None:
        try:
            self._emit("screen", phase=phase, **fields)
        except Exception as error:  # noqa: BLE001 — khabar na pohnche to bhi kaam chalta hai
            logger.debug(f"Screen ki khabar '{phase}' nahi gayi: {error}")

    # ── Nataij ──────────────────────────────────────────────────────────────

    @staticmethod
    def _ok(text: str, shot: Shot | None = None) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if shot is not None:
            content.append({"type": "image", "data": shot.b64(), "mimeType": shot.mime})
        return {"content": content, "is_error": False}

    @staticmethod
    def _fail(text: str, shot: Shot | None = None) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if shot is not None:
            content.append({"type": "image", "data": shot.b64(), "mimeType": shot.mime})
        return {"content": content, "is_error": True}

    def _check(self, epoch: int) -> None:
        if epoch != self._epoch:
            raise HandsStopped(STOPPED)

    def _acted(self) -> None:
        self._settle_at = time.monotonic() + self._limits.settle_ms / 1000

    async def _settled(self) -> None:
        remaining = self._settle_at - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

    def _is_last(self, tool_id: str | None) -> bool:
        """Ye is jawab ka aakhri screen wala kaam hai? Pata na chale to "haan" (tasveer bhej do)."""
        later = self._turn.later_tool_names(tool_id) if self._turn is not None else None
        if later is None:
            return True
        return not any(name.startswith(PREFIX) for name in later)

    # ── Ek tool call ────────────────────────────────────────────────────────

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool_id = self._turn.current_tool_id() if self._turn is not None else None
        seq = self._turn.message_seq() if self._turn is not None else 0
        epoch = self._epoch
        if not self._hands.available:
            return self._fail("Screen control works on Windows only.")
        if self._halt:
            return self._fail(TOOK_MOUSE)
        if self._broken_seq is not None and self._broken_seq == seq:
            return self._fail(NOT_EXECUTED)
        try:
            await self._begin(epoch)
            self._step += 1
            self._notify("step", step=self._step, label=label_for(name, args))
            if name == "screenshot":
                shot = await self._look(epoch)
                if self.aim == "grid":
                    return self._ok(f"The screen right now. Give positions on the 0-{GRID} grid over this picture.", shot)
                return self._ok(f"The screen right now, {shot.width} by {shot.height} pixels.", shot)
            if name == "zoom":
                return self._ok("That part of the screen, enlarged. Positions for the other tools still come from the full screenshot.", await self._zoom(args, epoch))
            done = await self._act(name, args, epoch)
            self._acted()
            if not self._is_last(tool_id):
                await asyncio.sleep(self._limits.between_ms / 1000)
                return self._ok(done)
            return self._ok(f"{done} The screen now:", await self._look(epoch))
        except UserTookMouse:
            self._halt = True
            self._broken_seq = seq
            logger.info("Screen: user ne mouse le liya — kaam ruka")
            return self._fail(TOOK_MOUSE)
        except HandsStopped:
            logger.info(f"Screen: {name} roka gaya")
            return self._fail(STOPPED)
        except _NotLooked:
            # Naye jumle par dimagh aksar pichhli tasveer ki yaad se click bhejta
            # hai. Sirf "pehle dekho" kehne se ek poora chakkar zaya hota hai;
            # tasveer isi jawab ke saath de do.
            self._broken_seq = seq
            logger.info(f"Screen: {name} — dekhe baghair pixel aaya; taaza tasveer wapas")
            try:
                return self._fail(NOT_LOOKED, await self._look(epoch))
            except HandsError:
                return self._fail(STOPPED)
        except AimError as error:
            self._broken_seq = seq
            logger.warning(f"Screen: nishana na laga ({error.kind}) — {error}")
            return self._fail(f"NOT DONE: {error}")
        except HandsError as error:
            self._broken_seq = seq
            logger.info(f"Screen: {name} nahi hua — {error}")
            return self._fail(f"NOT DONE: {error}")
        except Exception as error:  # noqa: BLE001 — haath ki koi bhi ghalti dimagh ko saaf lafzon mein mile
            self._broken_seq = seq
            logger.exception(f"Screen: {name} gir gaya")
            return self._fail(f"NOT DONE: {name} failed ({type(error).__name__}: {error}).")

    async def _begin(self, epoch: int) -> None:
        # Pichhla "roko" yahan saaf hota hai — haathon ke apne thread par, taake
        # jo kaam abhi ruk raha hai wo pehle poora ruk le.
        await self._hands.run(self._hands.resume)
        self._check(epoch)
        if self._active:
            return
        self._active = True
        self._step = 0
        await self._hands.run(self._hands.forget_cursor)
        self._notify("start", task=self._task, max_steps=self._max_steps, aim="claude" if self.aim == "grid" else self.aim)
        self._check(epoch)

    async def _look(self, epoch: int) -> Shot:
        await self._settled()
        self._check(epoch)
        shot = await self._hands.run(self._hands.capture, self._limits.max_dim, self._limits.jpeg_quality)
        self._view = (shot.width, shot.height)
        return shot

    async def _zoom(self, args: dict[str, Any], epoch: int) -> Shot:
        if self._view is None:
            raise HandsError("Take a screenshot first: the region is given on the latest screenshot.")
        region = args.get("region")
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            raise HandsError("region must be [x0, y0, x1, y1].")
        width, height = (GRID, GRID) if self.aim == "grid" else self._view
        x0, y0, x1, y1 = (float(value) for value in region)
        box = (min(x0, x1) / width, min(y0, y1) / height, max(x0, x1) / width, max(y0, y1) / height)
        await self._settled()
        self._check(epoch)
        return await self._hands.run(self._hands.zoom, box, self._limits.max_dim)

    # ── Nishana ─────────────────────────────────────────────────────────────

    def _pixel(self, args: dict[str, Any], key: str) -> tuple[float, float]:
        if self._view is None:
            raise _NotLooked()
        value = args.get(key)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise HandsError(f"{key} must be [x, y].")
        x, y = float(value[0]), float(value[1])
        if self.aim == "grid":
            if not (0 <= x <= GRID and 0 <= y <= GRID):
                raise HandsError(f"({int(x)}, {int(y)}) is off the grid: both numbers go from 0 to {GRID}.")
            return (min(x / GRID, 1.0), min(y / GRID, 1.0))
        width, height = self._view
        if not (0 <= x < width and 0 <= y < height):
            raise HandsError(f"({int(x)}, {int(y)}) is outside the screenshot, which is {width} by {height} pixels.")
        return to_fraction(x, y, width, height)

    async def _find(self, description: Any, epoch: int) -> tuple[float, float]:
        """UI-TARS se: ye cheez screen par abhi kahan hai."""
        words = " ".join(str(description or "").split())
        if len(words) < 3:
            raise HandsError("Describe the element to act on.")
        if self._aimer is None:
            raise AimError("Screen control is set to UI-TARS, but the pointing model is not set up.", "no_key")
        await self._settled()
        self._check(epoch)
        # Taaza tasveer: is jawab ke pichhle kaam ne screen badal di ho to bhi nishana abhi wali screen par lage.
        shot = await self._hands.run(self._hands.capture, self._limits.aim_max_dim, self._limits.jpeg_quality, 28)
        x, y = await self._aimer.locate(words, shot)
        self._check(epoch)
        return to_fraction(x, y, shot.width, shot.height)

    async def _spot(self, args: dict[str, Any], epoch: int, *, start: bool = False) -> tuple[float, float]:
        if self.aim in ("claude", "grid"):
            return self._pixel(args, "start_coordinate" if start else "coordinate")
        return await self._find(args.get("start_target" if start else "target"), epoch)

    def _where(self, spot: tuple[float, float]) -> str:
        """Jagah, us tasveer ke pixel mein jo dimagh dekhta hai — taake wo agli tasveer par jaanch sake."""
        if self.aim == "grid":
            return f"({int(spot[0] * GRID)}, {int(spot[1] * GRID)})"
        width, height = self._view or (self._limits.max_dim, round(self._limits.max_dim * 9 / 16))
        return f"({int(spot[0] * width)}, {int(spot[1] * height)})"

    # ── Kaam ────────────────────────────────────────────────────────────────

    async def _act(self, name: str, args: dict[str, Any], epoch: int) -> str:
        hands = self._hands
        if name == "wait":
            await hands.run(hands.wait, float(args.get("duration") or 1.0))
            return "Waited."

        held = modifier_keys(str(args.get("text") or "")) if name not in ("type", "key") else []
        told = self.aim == "uitars"  # UI-TARS ne jagah chuni: dimagh ko batao kahan

        if name in CLICKS:
            button, count, _verb = CLICKS[name]
            spot = await self._spot(args, epoch)
            await self._guard(epoch)
            await hands.run(hands.click, spot[0], spot[1], button, count, held)
            return f"Clicked at {self._where(spot)}." if told else "Clicked."

        if name == "mouse_move":
            spot = await self._spot(args, epoch)
            await self._guard(epoch)
            await hands.run(hands.move, spot[0], spot[1])
            return f"The pointer is at {self._where(spot)}." if told else "Moved."

        if name == "left_click_drag":
            start = await self._spot(args, epoch, start=True)
            end = await self._spot(args, epoch)
            await self._guard(epoch)
            await hands.run(hands.drag, start, end, held)
            return f"Dragged from {self._where(start)} to {self._where(end)}." if told else "Dragged."

        if name == "scroll":
            direction = str(args.get("scroll_direction") or "down").lower()
            if direction not in ("up", "down", "left", "right"):
                raise HandsError("scroll_direction must be up, down, left or right.")
            has_place = bool(args.get("coordinate")) if self.aim in ("claude", "grid") else bool(str(args.get("target") or "").strip())
            spot = await self._spot(args, epoch) if has_place else None
            await self._guard(epoch)
            await hands.run(hands.scroll, direction, int(args.get("scroll_amount") or 3), spot, held)
            return f"Scrolled {direction}."

        if name == "type":
            text = str(args.get("text") or "")
            if not text:
                raise HandsError("There is no text to type.")
            await self._guard(epoch)
            await hands.run(hands.type_text, text)
            return "Typed."

        if name == "key":
            chords = parse_keys(str(args.get("text") or ""))
            await self._guard(epoch)
            await hands.run(hands.press, chords, int(args.get("repeat") or 1))
            return "Pressed."

        raise HandsError(f"{name} is not a screen tool.")

    async def _guard(self, epoch: int) -> None:
        """Haath lagane se theek pehle: user bol to nahi raha, roka to nahi gaya, mouse user ke haath mein to nahi."""
        while time.monotonic() < self._hold_until:
            self._check(epoch)
            await asyncio.sleep(0.05)
        self._check(epoch)
        if mouse_taken():
            raise HandsError(MOUSE_TAKEN)
        if await self._hands.run(self._hands.user_moved):
            raise UserTookMouse()
