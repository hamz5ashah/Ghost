"""Classic voice ka kaam — usi Claude Agent SDK dimagh par jo New chalata hai (agent.py).

Classic mein baat Gemini Live karta hai (app ke andar: awaaz sunna, sochna,
bolna). PC ka har kaam — command, files, screen — wo `agent_task` tool se yahan
bhejta hai. Yahan ek "desk" hai: ek `AgentBrain` (`voice=False`) — wohi Claude
Code harness, wohi pehra (safety.py), wohi screen ke haath (screen.py) — jo kaam
karta hai aur aakhir mein chhoti si report likhta hai. Report app ke zariye
Gemini Live tak jati hai, aur wo user ko apne lafzon mein sunata hai.

    Gemini Live ──agent_task──> app ──POST /work/task──> Desk ──> AgentBrain (Claude Code)
    AgentBrain ki khabrein ──stdout {"event": "work", …}──> app ──> Gemini Live ko report

Loop, tools, context, compaction, peeche ke command (`run_in_background`) — sab
Claude Code ke apne. Yahan sirf jor hai: kaam ka shuru, beech ke qadam (screen par
dikhane ke liye), aur khatam hone par report.

Ek desk = Classic ki ek baat-cheet ka "haath": agla kaam usi dimagh ko jata hai
(pichla kaam yaad rehta hai — "ab isay rename bhi kar do"). Kaam chal raha ho aur
naya hukm aaye to wo usi kaam mein jata hai (Claude Code ki apni qatar: agle qadam
par model dekhta hai). Itni der koi kaam na ho to dimagh band (RAM wapas); agla
kaam naye dimagh par.

Teen pehre (6 Nov 2026 — Gemini ne "karo" sun kar wohi kaam baar baar bheja):
  - Chalte kaam mein wohi hukm dobara, ya sirf "karo / jari rakho": dimagh tak nahi
    jata (`duplicate`). Warna Claude wohi kaam do teen dafa karta (email, delete).
  - Roko jab dimagh abhi uth hi raha ho: kaam shuru hi nahi hota ("stopped").
  - Roka hua kaam band ho raha ho aur naya hukm aaye: us ke band hone ke baad naya
    kaam (roke hue kaam mein ghul kar "background job" nahi banta).
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

from loguru import logger

from stonic_voice import host
from stonic_voice.config import ConfigError, Settings, agent_home, agent_thinking, agent_workspace, work_missing, work_settings, work_system_prompt

# Khali desk ka dimagh (Claude Code ka process) itni der baad band.
IDLE_CLOSE_SECS = 15 * 60
# Report / qadam ki lambai (app tak jane wali khabar).
MAX_REPORT_CHARS = 6000
MAX_TASK_CHARS = 8000
MAX_LABEL_CHARS = 160
# Desk ka naam (app deti hai).
_DESK_CHARS = 64
# Roka hua kaam itni der mein band na ho to dimagh hi naya (naya hukm atka na rahe).
STOP_SETTLE_SECS = 10.0
# Kaam ke aakhri qadam ke baad joda gaya hukm: Claude Code us par alag jawab shuru
# kare (itni der ke andar) to wo usi hukm ka jawab hai, "peeche ka command" nahi.
FOLLOWUP_SECS = 30.0
STOPPED_BEFORE_START = "Stopped before it started — nothing was done on the PC."
# User ne kaam roka: itni der wohi kaam dobara aaye (Gemini ka dohrana) to nahi chalta.
RESTART_GUARD_SECS = 30.0
WORK_INTENTS = frozenset({"task", "update", "resume"})


def _clip(text: Any, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── Dohraya hukm ─────────────────────────────────────────────────────────────
#
# Chalte kaam mein naya hukm tab hi dimagh tak jata hai jab us mein kuch naya ho.
# Pehra jaan boojh kar ehtiyat wala hai: shak ho to hukm jata hai (zyada se zyada
# Claude ek baat dobara parhta hai), kyunke asli tabdeeli ka gum hona us se bura hai.
_WORD = re.compile(r"[a-z0-9']+")
# Sirf "karo / jari rakho / jaldi" — kaam ke baare mein koi nayi baat nahi.
_NUDGE_WORDS = frozenset(
    "continue continuing keep keeping going go ahead carry on proceed resume finish finishing complete "
    "please pls now just quickly fast faster hurry up still again same task work job it this that "
    "the a an and to with in on of for do doing make sure ok okay yes yeah sure".split()
)
# "Jari rakho" wala jumla — sirf in mein "purane lafz hi dobara" ko dohraya maana jata hai.
_CONTINUE_WORDS = frozenset("continue continuing keep carry proceed resume finish go".split())
# Inkaar / qaid / badal: hamesha nayi baat ("not that one", "only", "instead").
_CHANGE_WORDS = frozenset(
    "no not don't dont never instead only except without but rather actually other another different "
    "stop cancel undo".split()
)


def _words(text: str) -> list[str]:
    return _WORD.findall(str(text or "").lower())


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def adds_nothing(text: str, said: list[str]) -> bool:
    """Chalte kaam ke liye is hukm mein kuch naya nahi: wohi hukm dobara, ya sirf "karo / jari rakho"."""
    words = _words(text)
    if not words:
        return True
    if any(words == _words(earlier) for earlier in said):
        return True
    if any(word in _CHANGE_WORDS for word in words):
        return False
    content = {_stem(word) for word in words if word not in _NUDGE_WORDS}
    if not content:
        return True
    if not any(word in _CONTINUE_WORDS for word in words):
        # "Jari rakho" nahi kaha: purane lafz bhi hon to shayad tabdeeli hai — dimagh faisla kare.
        return False
    known = {_stem(word) for earlier in said for word in _words(earlier)}
    return content <= known


def _route_key(settings: Settings) -> tuple[str, ...]:
    """Raasta / chabi / model badle to purana dimagh is kaam ke liye nahi (naya uthta hai)."""
    cloud = settings.cloud
    return (
        settings.mode, settings.brain.provider, settings.brain.model, settings.brain.thinking,
        settings.keys.for_brain(settings.brain.provider), cloud.url, cloud.token, cloud.brain_model, settings.screen_aim,
        # User ne skill banayi / badli / band ki: Claude Code skills session ke shuru mein
        # parhta hai — agla kaam naye dimagh par, taaki wohi skills chalein jo Skills card mein hain.
        ",".join(settings.skills.qualified), settings.skills.version,
    )


class Desk:
    """Classic ki ek baat-cheet ka kaam karne wala dimagh."""

    def __init__(self, desk_id: str) -> None:
        self.id = desk_id
        self.brain: Any = None
        self._route: tuple[str, ...] | None = None
        self._task: str = ""          # chalta kaam (app ka id)
        self._origin = "task"         # "task" (Gemini ne diya) | "notice" (peeche ka command khatam) | "followup" (aakhir mein joda hukm) — dono mein CLI ne khud jawab shuru kiya
        self._steps = 0
        self._text: list[str] = []     # aakhri tool ke baad ka text = report
        self._before: list[str] = []   # tool se pehle ka text (report khali ho to yehi)
        self._error = ""
        self._error_kind = ""
        self._jobs: dict[str, str] = {}
        self._said: list[str] = []                 # is kaam ke hukm (pehla + beech ke) — dohrane ki pehchan
        self._added: list[tuple[str, int]] = []    # beech mein jode hukm + us waqt tak ke qadam
        self._stop_asked = False                   # roko aaya jab kaam abhi dimagh tak pohncha hi nahi tha
        self._stopping = False                     # roka gaya; dimagh ke "turn khatam" ka intezar
        self._settled = asyncio.Event()            # koi kaam nahi chal raha (roke hue kaam ka intezar isi par)
        self._settled.set()
        self._late_adds: tuple[float, list[str]] | None = None  # aakhri qadam ke baad jode hukm (khatam kaam ke)
        self._stopped: tuple[float, list[str]] | None = None    # user ka roka hua aakhri kaam (waqt, hukm)
        self._last_activity = time.monotonic()
        self._idle: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        # Dimagh muft Gemini key par (gemini_relay.py): CLI ka "kharcha" Claude ki qeemat se
        # andaza hai — Gemini par ghalat, is liye report mein nahi jata.
        self._free = False

    # ── Haal ────────────────────────────────────────────────────────────────

    @property
    def busy(self) -> bool:
        return bool(self.brain is not None and not self.brain.closed and (self.brain.working or self._task))

    @property
    def _brain_working(self) -> bool:
        return bool(self.brain is not None and not self.brain.closed and self.brain.working)

    def public(self) -> dict[str, Any]:
        return {
            "desk": self.id,
            "open": self.brain is not None and not self.brain.closed,
            "busy": self.busy,
            "task": self._task,
            "steps": self._steps,
            "jobs": list(self._jobs.values()),
            # Kaam qubool, dimagh abhi uth raha hai (pehla qadam aane wala hai).
            "starting": bool(self._task) and not self._brain_working and not self._stop_asked,
            "stopping": self._stopping or self._stop_asked,
        }

    def _emit(self, kind: str, **fields: Any) -> None:
        host.emit("work", desk=self.id, kind=kind, **fields)

    # ── Kaam ────────────────────────────────────────────────────────────────

    async def submit(self, settings: Settings, text: str, task_id: str, intent: str = "task") -> dict[str, Any]:
        """Naya kaam, ya chalte kaam mein naya hukm. Foran lautta hai; natija "work" khabron mein.

        Jawab: `joined` = chalte kaam mein gaya; `duplicate` = us mein kuch naya nahi tha,
        dimagh tak nahi bheja (kaam pehle se wohi kar raha hai). `intent` = "task" |
        "update" | "resume" (Gemini ka tool) — "resume" hi roke hue kaam ko wapas chala sakta hai.
        """
        async with self._lock:
            self._last_activity = time.monotonic()
            if self._task and self._stopping:
                # Roka hua kaam abhi band ho raha hai: naya hukm us mein nahi — us ke baad naya kaam.
                await self._await_stopped()
            route = _route_key(settings)
            brain = self.brain
            if brain is not None and (brain.closed or (route != self._route and not self.busy)):
                # Dimagh band ho chuka, ya raasta badla (aur kuch chal nahi raha): naya dimagh.
                await self._close_brain()
                brain = None
            if brain is not None and self.busy and not self._stop_asked:
                if adds_nothing(text, self._said):
                    logger.info(f"Work[{self.id}]: chalte kaam mein wohi hukm dobara — dimagh tak nahi bheja")
                    return {"ok": True, "task": self._task, "joined": True, "duplicate": True}
                # Kaam chal raha hai: naya hukm usi kaam mein (Claude Code ki apni qatar).
                logger.info(f"Work[{self.id}]: chalte kaam mein naya hukm")
                self._said.append(text)
                self._added.append((text, self._steps))
                await brain.say(text, mid_job=True)
                return {"ok": True, "task": self._task, "joined": True}
            stopped = self._said if (self._task and self._stop_asked) else self._recently_stopped()
            if intent != "resume" and stopped and adds_nothing(text, stopped):
                # User ne abhi roka tha aur ye wohi kaam hai (Gemini ka dohrana): dobara nahi
                # chalta. User sach mein wapas chahe to Gemini `agent_task_resume` deta hai.
                logger.info(f"Work[{self.id}]: roka hua kaam dobara aaya — nahi chalaya (user ne roka tha)")
                return {"ok": False, "code": "stopped_by_user"}
            if self._task and self._stop_asked:
                # Shuru hone se pehle roka gaya kaam kabhi nahi chalega; ye naya kaam us ki jagah.
                self._finish("stopped", STOPPED_BEFORE_START)
            if intent == "resume":
                self._stopped = None
            if brain is None:
                brain = self._new_brain(settings)
                self.brain = brain
                self._route = route
                asyncio.create_task(brain.start(), name=f"stonic-work-start-{self.id}")
            self._begin(task_id, "task")
            self._said = [text]
            self._emit("started", task=task_id, text=_clip(text, 300))
            asyncio.create_task(self._send(brain, text, task_id), name=f"stonic-work-say-{self.id}")
            self._watch_idle()
            return {"ok": True, "task": task_id, "joined": False}

    def _recently_stopped(self) -> list[str] | None:
        """Abhi abhi user ka roka hua kaam (RESTART_GUARD_SECS ke andar) — us ke hukm."""
        last = self._stopped
        if last and time.monotonic() - last[0] <= RESTART_GUARD_SECS:
            return last[1]
        return None

    async def _await_stopped(self) -> None:
        """Roke hue kaam ke band hone ka intezar; der lage to dimagh hi naya (atka na rahe)."""
        try:
            await asyncio.wait_for(self._settled.wait(), timeout=STOP_SETTLE_SECS)
        except asyncio.TimeoutError:
            logger.warning(f"Work[{self.id}]: roka hua kaam {STOP_SETTLE_SECS:.0f} s mein band na hua — naya dimagh")
            await self._close_brain()

    async def _send(self, brain: Any, text: str, task_id: str) -> None:
        try:
            await brain.wait_ready()
            if self._task != task_id:
                return  # dimagh uthte uthte kaam khatam (uth na saka) ya badal gaya
            if self._stop_asked:
                self._finish("stopped", STOPPED_BEFORE_START)
                return
            await brain.say(text)
        except Exception as error:  # noqa: BLE001 — dimagh ki ghalti kaam ki nakaami ban kar app tak
            await self._on_event("error", {"detail": f"{type(error).__name__}: {error}", "fatal": True})

    def _new_brain(self, settings: Settings) -> Any:
        from stonic_voice.agent import AgentBrain, AgentConfig
        from stonic_voice.pipeline import agent_env, build_screen

        brain_settings = settings.brain
        config = AgentConfig(
            model=brain_settings.model,
            system_prompt=work_system_prompt(settings),
            env=agent_env(settings, agent_home()),
            cwd=agent_workspace(),
            effort=brain_settings.thinking,
            thinking=agent_thinking(brain_settings.model, brain_settings.thinking),
            builtin_tools=list(settings.agent_limits.tools),
            max_turns=settings.agent_limits.max_steps,
            # Classic: Gemini bolta hai, yaadash bhi wohi rakhta hai (`memory` tool).
            voice=False,
            persist=False,
            # User ki skills — wohi jo New ke paas hain (Claude Code ka plugin + Skill tool).
            skills_plugin=settings.skills.plugin if settings.skills.on else "",
            skills=settings.skills.qualified,
        )
        self._free = not settings.is_cloud and brain_settings.provider == "gemini"
        logger.info(f"Work[{self.id}]: naya dimagh — {brain_settings.model} ({settings.mode}{', Gemini pul' if self._free else ''})")
        return AgentBrain(config, listener=self._on_event, screen=build_screen(settings))

    def _begin(self, task_id: str, origin: str) -> None:
        self._task = task_id
        self._origin = origin
        self._steps = 0
        self._text = []
        self._before = []
        self._error = ""
        self._error_kind = ""
        self._said = []
        self._added = []
        self._stop_asked = False
        self._stopping = False
        self._late_adds = None
        self._settled.clear()

    async def stop(self) -> bool:
        """Chalta kaam roko (dimagh zinda rehta hai — agla kaam usi par).

        Kaam qubool ho chuka magar dimagh abhi uth raha ho (pehla jumla us tak gaya hi
        nahi): wo jumla jata hi nahi, kaam "stopped" — "koi kaam nahi chal raha" kehna
        jhoot hota aur kaam phir bhi chal parta.
        """
        brain = self.brain
        if brain is None or brain.closed:
            return False
        if brain.working:
            self._stopping = bool(self._task)
            await brain.interrupt()
            return True
        if self._task:
            self._stop_asked = True
            return True
        return False

    async def close(self) -> None:
        async with self._lock:
            await self._close_brain()
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    async def _close_brain(self) -> None:
        brain, self.brain = self.brain, None
        self._route = None
        if brain is not None:
            if self._task:
                self._finish("stopped", "The work stopped because the desk closed.")
            await brain.stop()

    # ── Dimagh ki khabrein ──────────────────────────────────────────────────

    async def _on_event(self, event: str, data: dict[str, Any]) -> None:
        self._last_activity = time.monotonic()
        if event in ("tool_start", "text", "reply_start") and not self._task:
            # Koi kaam nahi chal raha magar dimagh ne khud jawab shuru kiya. Do wajahein:
            #   - pichle kaam ke aakhri qadam ke BAAD joda gaya hukm — Claude Code ne us
            #     par alag jawab shuru kiya ("followup", wohi hukm saath);
            #   - peeche wala command khatam hua (Claude Code ki task-notification, "notice").
            late, self._late_adds = self._late_adds, None
            if late and time.monotonic() - late[0] <= FOLLOWUP_SECS:
                origin, text = "followup", " / ".join(late[1])
            else:
                origin, text = "notice", ""
            self._begin(f"{origin}-{uuid.uuid4().hex[:10]}", origin)
            self._said = [text] if text else []
            self._emit("started", task=self._task, origin=origin, text=_clip(text, 300))
        if event == "tool_start" and self._stopping and self.brain is not None:
            # Roko ke baad bhi naya qadam (roko CLI tak turn shuru hone se pehle pohncha): dobara roko.
            asyncio.create_task(self.brain.interrupt(), name=f"stonic-work-restop-{self.id}")
        if event == "tool_start":
            self._steps += 1
            if self._text:
                self._before = self._text
                self._text = []
            self._emit("step", task=self._task, step=self._steps, label=_clip(data.get("label"), MAX_LABEL_CHARS), tool=str(data.get("tool") or ""))
        elif event == "tool_end":
            if data.get("ok") is False:
                self._emit("step_failed", task=self._task, step=self._steps, output=_clip(data.get("output"), 400))
        elif event == "text":
            self._text.append(str(data.get("text") or ""))
        elif event == "job_start":
            self._jobs[str(data.get("id") or "")] = _clip(data.get("label"), MAX_LABEL_CHARS)
            self._emit("job", task=self._task, status="started", label=_clip(data.get("label"), MAX_LABEL_CHARS), running=len(self._jobs))
        elif event == "job_end":
            self._jobs.pop(str(data.get("id") or ""), None)
            self._emit("job", task=self._task, status=str(data.get("status") or ""), label=_clip(data.get("label"), MAX_LABEL_CHARS), running=len(self._jobs))
        elif event == "error":
            self._error = _clip(data.get("detail"), 600)
            self._error_kind = str(data.get("kind") or data.get("subtype") or "")
            if data.get("fatal"):
                # Dimagh uth na saka / toot gaya: kaam nakaam, agla kaam naye dimagh par.
                logger.warning(f"Work[{self.id}]: dimagh ki ghalti — {self._error[:200]}")
                if self._task:
                    self._finish("failed", "")
                brain = self.brain
                self.brain = None
                self._route = None
                if brain is not None:
                    asyncio.create_task(brain.stop(), name=f"stonic-work-close-{self.id}")
        elif event == "turn_end":
            aborted = bool(data.get("aborted"))
            status = "stopped" if aborted else ("failed" if self._error else "completed")
            self._finish(status, "", cost=None if self._free else data.get("cost_usd"))

    def _finish(self, status: str, fallback: str, *, cost: Any = None) -> None:
        if not self._task:
            return
        report = "".join(self._text).strip() or "".join(self._before).strip() or fallback
        payload: dict[str, Any] = {
            "task": self._task,
            "origin": self._origin,
            "status": status,
            "report": _clip(report, MAX_REPORT_CHARS),
            "steps": self._steps,
            "jobs": list(self._jobs.values()),
        }
        if self._error:
            payload["error"] = self._error
            payload["error_kind"] = self._error_kind
        if isinstance(cost, (int, float)):
            payload["cost_usd"] = cost
        logger.info(f"Work[{self.id}]: kaam {status} — {self._steps} qadam")
        self._emit("done", **payload)
        # Aakhri qadam ke baad jode hukm: shayad Claude Code un par alag jawab shuru kare.
        late = [text for text, at_step in self._added if at_step >= self._steps]
        # User ka roka hua kaam: thori der wohi hukm dobara aaye to chalta nahi (RESTART_GUARD_SECS).
        self._stopped = (time.monotonic(), list(self._said)) if status == "stopped" and self._said else None
        self._task = ""
        self._text = []
        self._before = []
        self._error = ""
        self._error_kind = ""
        self._said = []
        self._added = []
        self._stop_asked = False
        self._stopping = False
        self._late_adds = (time.monotonic(), late) if late and status == "completed" else None
        self._settled.set()

    # ── Khali desk ──────────────────────────────────────────────────────────

    def _watch_idle(self) -> None:
        if self._idle is None or self._idle.done():
            self._idle = asyncio.create_task(self._idle_loop(), name=f"stonic-work-idle-{self.id}")

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            brain = self.brain
            if brain is None or brain.closed:
                return
            if self.busy or self._jobs:
                continue
            if time.monotonic() - self._last_activity >= IDLE_CLOSE_SECS:
                logger.info(f"Work[{self.id}]: {IDLE_CLOSE_SECS // 60} minute koi kaam nahi — dimagh band")
                async with self._lock:
                    if not self.busy and not self._jobs:
                        await self._close_brain()
                return


_desks: dict[str, Desk] = {}


def _desk_id(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text or len(text) > _DESK_CHARS or not all(ch.isalnum() or ch in "-_" for ch in text):
        raise ConfigError("bad_request", "desk: chhota naam chahiye (harf, adad, - _)")
    return text


async def submit(payload: Any) -> dict[str, Any]:
    """App ka kaam (POST /work/task): {desk, task, text, config, intent?}."""
    if not isinstance(payload, dict):
        raise ConfigError("bad_request", "JSON object chahiye")
    desk_id = _desk_id(payload.get("desk"))
    text = _clip(payload.get("text"), MAX_TASK_CHARS)
    if not text:
        raise ConfigError("bad_request", "text chahiye")
    task_id = str(payload.get("task") or "").strip()[:64] or f"task-{uuid.uuid4().hex[:10]}"
    settings = work_settings(payload.get("config"))
    missing = work_missing(settings)
    if missing:
        return {"ok": False, "code": missing}
    intent = str(payload.get("intent") or "task")
    desk = _desks.get(desk_id)
    if desk is None:
        desk = _desks[desk_id] = Desk(desk_id)
    return await desk.submit(settings, text, task_id, intent if intent in WORK_INTENTS else "task")


async def stop(desk_id: Any) -> bool:
    desk = _desks.get(_desk_id(desk_id))
    return await desk.stop() if desk is not None else False


async def close(desk_id: Any) -> bool:
    desk = _desks.pop(_desk_id(desk_id), None)
    if desk is None:
        return False
    await desk.close()
    return True


def status(desk_id: Any) -> dict[str, Any]:
    desk = _desks.get(_desk_id(desk_id))
    return desk.public() if desk is not None else {
        "desk": str(desk_id), "open": False, "busy": False, "task": "", "steps": 0, "jobs": [], "starting": False, "stopping": False,
    }


async def close_all() -> None:
    for desk_id in list(_desks):
        desk = _desks.pop(desk_id, None)
        if desk is not None:
            await desk.close()
