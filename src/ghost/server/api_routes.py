"""Extended API routes for agents, workflows, memory, traces, etc."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---- Request/Response models ----


class AgentCreateRequest(BaseModel):
    agent_type: str
    tools: Optional[List[str]] = None
    agent_id: Optional[str] = None


class AgentMessageRequest(BaseModel):
    message: str


class SpeechSynthesizeRequest(BaseModel):
    text: str
    voice_id: Optional[str] = None
    speed: Optional[float] = None
    # Explicit engine chosen in the app ("kokoro_onnx", "elevenlabs", …).
    # Omitted: the server's configured/discovered backend.
    backend: Optional[str] = None


class MemoryStoreRequest(BaseModel):
    content: str
    metadata: Optional[Dict[str, Any]] = None


class MemorySearchRequest(BaseModel):
    query: str
    top_k: int = 5


class MemoryIndexRequest(BaseModel):
    path: str


class BudgetLimitsRequest(BaseModel):
    max_tokens_per_day: Optional[int] = None
    max_requests_per_hour: Optional[int] = None


class FeedbackScoreRequest(BaseModel):
    trace_id: str
    score: float
    source: str = "api"


class OptimizeRunRequest(BaseModel):
    benchmark: str
    max_trials: int = 20
    optimizer_model: str = "claude-sonnet-4-6"
    max_samples: int = 50


# ---- Agent routes ----

agents_router = APIRouter(prefix="/v1/agents", tags=["agents"])


def _execute_agent_admin_tool(request: Request, tool: Any, params: Dict[str, Any]):
    """Execute an agent lifecycle operation through server security gates."""
    from ghost.security.runtime import execute_secured_tool

    state = request.app.state
    return execute_secured_tool(
        tool,
        params,
        bus=getattr(state, "bus", None),
        capability_policy=getattr(state, "capability_policy", None),
        rate_limiter=getattr(state, "rate_limiter", None),
        agent_id="server:api",
    )


def _raise_agent_tool_failure(result: Any, *, not_found: bool = False) -> None:
    if result.success:
        return
    if "Capability '" in result.content and " denied " in result.content:
        raise HTTPException(status_code=403, detail=result.content)
    if result.content.startswith("Rate limit exceeded"):
        raise HTTPException(status_code=429, detail=result.content)
    raise HTTPException(status_code=404 if not_found else 400, detail=result.content)


@agents_router.get("")
async def list_agents(request: Request):
    """List available agent types and running agents."""
    try:
        from ghost.tools.agent_tools import AgentListTool

        # Registry names and live agent metadata are administrative state,
        # just like spawn/send/kill.  Authorize before reading either source
        # so default-deny and rate-limit policies cannot be bypassed by GET.
        result = _execute_agent_admin_tool(request, AgentListTool(), {})
        _raise_agent_tool_failure(result)
    except ImportError:
        raise HTTPException(status_code=501, detail="Agent tools not available")

    registered = []
    try:
        import ghost.agents  # noqa: F401 — side-effect registration
        from ghost.core.registry import AgentRegistry

        for key in sorted(AgentRegistry.keys()):
            cls = AgentRegistry.get(key)
            registered.append(
                {
                    "key": key,
                    "class": cls.__name__,
                    "accepts_tools": getattr(cls, "accepts_tools", False),
                }
            )
    except Exception as exc:
        logger.warning("Failed to list registered agents: %s", exc)

    running = []
    try:
        from ghost.tools.agent_tools import _SPAWNED_AGENTS

        running = [{"id": k, **v} for k, v in _SPAWNED_AGENTS.items()]
    except ImportError:
        pass

    return {"registered": registered, "running": running}


@agents_router.post("")
async def create_agent(req: AgentCreateRequest, request: Request):
    """Spawn a new agent."""
    try:
        from ghost.tools.agent_tools import AgentSpawnTool

        tool = AgentSpawnTool()
        params = {"agent_type": req.agent_type}
        if req.tools:
            params["tools"] = ",".join(req.tools)
        if req.agent_id:
            params["agent_id"] = req.agent_id
        result = _execute_agent_admin_tool(request, tool, params)
        _raise_agent_tool_failure(result)
        return {
            "status": "created",
            "content": result.content,
            "metadata": result.metadata,
        }
    except ImportError:
        raise HTTPException(status_code=501, detail="Agent tools not available")


@agents_router.delete("/{agent_id}")
async def kill_agent(agent_id: str, request: Request):
    """Kill a running agent."""
    try:
        from ghost.tools.agent_tools import AgentKillTool

        tool = AgentKillTool()
        result = _execute_agent_admin_tool(request, tool, {"agent_id": agent_id})
        _raise_agent_tool_failure(result, not_found=True)
        return {"status": "stopped", "agent_id": agent_id}
    except ImportError:
        raise HTTPException(status_code=501, detail="Agent tools not available")


@agents_router.post("/{agent_id}/message")
async def message_agent(agent_id: str, req: AgentMessageRequest, request: Request):
    """Send a message to a running agent."""
    try:
        from ghost.tools.agent_tools import AgentSendTool

        tool = AgentSendTool()
        result = _execute_agent_admin_tool(
            request,
            tool,
            {"agent_id": agent_id, "message": req.message},
        )
        _raise_agent_tool_failure(result, not_found=True)
        return {"status": "sent", "content": result.content}
    except ImportError:
        raise HTTPException(status_code=501, detail="Agent tools not available")


# ---- Memory routes ----

memory_router = APIRouter(prefix="/v1/memory", tags=["memory"])


def _get_memory_backend(request: Request):
    """Return the app-level memory backend, falling back to a fresh SQLiteMemory.

    Raises ``HTTPException(503)`` with an actionable message when the backend
    cannot be built because the mandatory ``ghost_rust`` extension is not
    installed in the serving venv. This is deliberately distinct from a benign
    "memory not configured" case (which returns ``None``): a missing native
    extension must fail loudly, never silently degrade (#502).
    """
    backend = getattr(request.app.state, "memory_backend", None)
    if backend is None:
        from ghost.tools.storage._stubs import MemoryBackendUnavailable

        try:
            from ghost.tools.storage.sqlite import SQLiteMemory

            backend = SQLiteMemory()
        except MemoryBackendUnavailable as exc:
            # The native extension is missing — surface a loud, actionable error
            # rather than a misleading "no backend" / silent no-op.
            logger.error("%s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception:
            # Memory is genuinely unconfigured for a benign reason — preserve
            # the existing graceful "no backend" behaviour.
            return None
    return backend


@memory_router.post("/store")
def memory_store(req: MemoryStoreRequest, request: Request):
    """Store content in memory."""
    backend = _get_memory_backend(request)
    if backend is None:
        # Memory is intentionally disabled; report it honestly instead of a
        # 200 that silently discards the write (#502).
        raise HTTPException(status_code=503, detail="Memory is not configured")
    try:
        backend.store(req.content, metadata=req.metadata or {})
        return {"status": "stored"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@memory_router.post("/search")
def memory_search(req: MemorySearchRequest, request: Request):
    """Search memory for relevant content."""
    backend = _get_memory_backend(request)
    if backend is None:
        return {"results": []}
    try:
        results = backend.retrieve(req.query, top_k=req.top_k)
        items = [
            {
                "content": r.content,
                "score": getattr(r, "score", 0.0),
                "metadata": getattr(r, "metadata", {}),
            }
            for r in results
        ]
        return {"results": items}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@memory_router.get("/stats")
def memory_stats(request: Request):
    """Get memory backend statistics."""
    backend = _get_memory_backend(request)
    if backend is None:
        return {"entries": 0, "backend": "none", "status": "not_configured"}
    try:
        return {
            "entries": backend.count(),
            "backend": getattr(backend, "backend_id", "unknown"),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@memory_router.get("/config")
async def memory_config(request: Request):
    """Return current memory configuration.

    Reports memory as *unavailable* (rather than falsely claiming
    ``backend_type: sqlite``) when the native ``ghost_rust`` extension is
    missing, so the UI can show the real cause instead of a healthy-looking
    config that backs a silent no-op (#502).
    """
    try:
        config = getattr(request.app.state, "config", None)
        if config is None:
            from ghost.core.config import load_config

            config = load_config()
        backend = getattr(request.app.state, "memory_backend", None)
        available = True
        detail: Optional[str] = None
        if backend is None:
            from ghost.tools.storage._stubs import MemoryBackendUnavailable

            try:
                from ghost.tools.storage.sqlite import SQLiteMemory

                backend = SQLiteMemory()
            except MemoryBackendUnavailable as exc:
                available = False
                detail = str(exc)
            except Exception:
                # Benign: cannot construct a probe backend here, but the
                # configured default is still what would be used.
                pass
        return {
            "backend_type": (
                backend.backend_id
                if backend is not None
                else config.memory.default_backend
            ),
            "available": available,
            "detail": detail,
            "context_top_k": config.memory.context_top_k,
            "context_min_score": config.memory.context_min_score,
            "context_max_tokens": config.memory.context_max_tokens,
            "context_from_memory": config.agent.context_from_memory,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@memory_router.post("/index")
def memory_index(req: MemoryIndexRequest, request: Request):
    """Index files from a path into memory."""
    try:
        import os
        from pathlib import Path

        from ghost.security.file_policy import is_sensitive_file
        from ghost.tools.storage.ingest import ingest_path

        target = Path(req.path).expanduser().resolve()
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {req.path}")

        # Sandbox: when workspace roots are configured via GHOST_WORKSPACE
        # (os.pathsep-separated), only allow indexing inside them. This endpoint
        # must not become an arbitrary-filesystem read primitive over the API.
        workspace = os.environ.get("GHOST_WORKSPACE", "").strip()
        if workspace:
            roots = [
                Path(d).expanduser().resolve()
                for d in workspace.split(os.pathsep)
                if d.strip()
            ]
            if not any(target == root or root in target.parents for root in roots):
                raise HTTPException(
                    status_code=403,
                    detail="Path is outside the allowed workspace directories.",
                )
        # Never ingest sensitive files (.env, private keys, credentials, ...).
        if target.is_file() and is_sensitive_file(target):
            raise HTTPException(
                status_code=403, detail="Refusing to index a sensitive file."
            )

        backend = _get_memory_backend(request)
        if backend is None:
            raise HTTPException(status_code=503, detail="Memory is not configured")

        chunks = ingest_path(target)
        stored = 0
        for chunk in chunks:
            metadata = {"source": getattr(chunk, "source", str(target))}
            if hasattr(chunk, "metadata") and chunk.metadata:
                metadata.update(chunk.metadata)
            backend.store(chunk.content, metadata=metadata)
            stored += 1

        result = {"status": "indexed", "chunks_indexed": stored}
        if stored == 0:
            # "indexed" must never silently mean "stored nothing". Surface why
            # so a folder of short notes doesn't look like a successful no-op
            # (#502 follow-up).
            result["note"] = (
                "no content was indexed — the path contained no readable "
                "documents with indexable text"
            )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Traces routes ----

traces_router = APIRouter(prefix="/v1/traces", tags=["traces"])


def _serialise_trace(trace) -> dict:
    """Convert a Trace dataclass to a frontend-friendly dict."""
    import datetime
    from dataclasses import asdict

    d = asdict(trace)
    d["id"] = d.pop("trace_id", "")
    started = d.pop("started_at", 0.0)
    d["created_at"] = (
        datetime.datetime.fromtimestamp(started, tz=datetime.timezone.utc).isoformat()
        if started
        else None
    )
    dur = d.pop("total_latency_seconds", 0.0)
    d["duration_ms"] = round(dur * 1000)
    for step in d.get("steps", []):
        st = step.get("step_type")
        if hasattr(st, "value"):
            step["step_type"] = st.value
    return d


@traces_router.get("")
async def list_traces(request: Request, limit: int = 20):
    """List recent traces."""
    try:
        store = getattr(request.app.state, "trace_store", None)
        if store is None:
            return {"traces": []}
        traces = store.list_traces(limit=limit)
        items = [_serialise_trace(t) for t in traces]
        return {"traces": items}
    except Exception as exc:
        return {"traces": [], "error": str(exc)}


@traces_router.get("/{trace_id}")
async def get_trace(trace_id: str, request: Request):
    """Get a specific trace by ID."""
    try:
        store = getattr(request.app.state, "trace_store", None)
        if store is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        trace = store.get(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return _serialise_trace(trace)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Telemetry routes ----

telemetry_router = APIRouter(prefix="/v1/telemetry", tags=["telemetry"])


def _telemetry_db_path(request: Request) -> Path:
    """Resolve telemetry storage from the active app configuration.

    ``DEFAULT_CONFIG_DIR`` is fixed when :mod:`ghost.core.config` is
    imported, so it cannot honor a later ``GHOST_HOME`` override.  The
    running app's config is authoritative; lightweight apps that include these
    routes directly fall back to the env-aware path resolver.
    """
    config = getattr(request.app.state, "config", None)
    telemetry = getattr(config, "telemetry", None)
    configured_path = getattr(telemetry, "db_path", None)
    if configured_path:
        return Path(configured_path).expanduser()

    from ghost.core.paths import get_config_dir

    return get_config_dir() / "telemetry.db"


@telemetry_router.get("/stats")
async def telemetry_stats(request: Request):
    """Get aggregated telemetry statistics."""
    try:
        from dataclasses import asdict

        from ghost.telemetry.aggregator import TelemetryAggregator

        db_path = _telemetry_db_path(request)
        if not db_path.exists():
            return {"total_requests": 0, "total_tokens": 0}

        session_start = getattr(request.app.state, "session_start", None)
        agg = TelemetryAggregator(db_path)
        try:
            stats = agg.summary(since=session_start)
            d = asdict(stats)
            d.pop("per_model", None)
            d.pop("per_engine", None)
            d["total_requests"] = d.pop("total_calls", 0)
            return d
        finally:
            agg.close()
    except Exception as exc:
        return {"error": str(exc)}


@telemetry_router.get("/energy")
async def telemetry_energy(request: Request):
    """Get energy monitoring data."""
    try:
        from ghost.telemetry.aggregator import TelemetryAggregator

        db_path = _telemetry_db_path(request)
        if not db_path.exists():
            return {
                "total_energy_j": 0,
                "energy_per_token_j": 0,
                "avg_power_w": 0,
                "cpu_temp_c": None,
                "gpu_temp_c": None,
            }

        session_start = getattr(request.app.state, "session_start", None)
        agg = TelemetryAggregator(db_path)
        try:
            stats = agg.summary(since=session_start)
            total_energy = stats.total_energy_joules
            total_tokens = stats.total_tokens
            total_latency = stats.total_latency
            return {
                "total_energy_j": total_energy,
                "energy_per_token_j": (
                    total_energy / total_tokens if total_tokens > 0 else 0
                ),
                "avg_power_w": (
                    total_energy / total_latency if total_latency > 0 else 0
                ),
                "cpu_temp_c": None,
                "gpu_temp_c": None,
            }
        finally:
            agg.close()
    except Exception as exc:
        return {"error": str(exc)}


# ---- Skills routes ----

skills_router = APIRouter(prefix="/v1/skills", tags=["skills"])


@skills_router.get("")
async def list_skills(request: Request):
    """List installed skills."""
    try:
        from ghost.core.registry import SkillRegistry

        skills = []
        for key in sorted(SkillRegistry.keys()):
            skills.append({"name": key})
        return {"skills": skills}
    except Exception as exc:
        logger.warning("Failed to list skills: %s", exc)
        return {"skills": []}


@skills_router.post("")
async def install_skill(request: Request):
    """Install a skill (placeholder)."""
    return {
        "status": "not_implemented",
        "message": "Use TOML files in ~/.ghost/skills/",
    }


@skills_router.delete("/{skill_name}")
async def remove_skill(skill_name: str, request: Request):
    """Remove a skill (placeholder)."""
    return {
        "status": "not_implemented",
        "message": "Skill removal not yet supported via API",
    }


# ---- Sessions routes ----

sessions_router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@sessions_router.get("")
async def list_sessions(request: Request, limit: int = 20):
    """List active sessions."""
    try:
        from ghost.sessions.store import SessionStore

        store = SessionStore()
        sessions = store.recent(limit=limit)
        items = [s.to_dict() if hasattr(s, "to_dict") else str(s) for s in sessions]
        return {"sessions": items}
    except Exception as exc:
        return {"sessions": [], "error": str(exc)}


@sessions_router.get("/{session_id}")
async def get_session(session_id: str, request: Request):
    """Get a specific session."""
    try:
        from ghost.sessions.store import SessionStore

        store = SessionStore()
        session = store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session.to_dict() if hasattr(session, "to_dict") else {"id": session_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Budget routes ----

budget_router = APIRouter(prefix="/v1/budget", tags=["budget"])

_budget_limits: Dict[str, Any] = {
    "max_tokens_per_day": None,
    "max_requests_per_hour": None,
}
_budget_usage: Dict[str, int] = {
    "tokens_today": 0,
    "requests_this_hour": 0,
}


@budget_router.get("")
async def get_budget(request: Request):
    """Get current budget usage and limits."""
    return {"limits": _budget_limits, "usage": _budget_usage}


@budget_router.put("/limits")
async def set_budget_limits(req: BudgetLimitsRequest, request: Request):
    """Update budget limits."""
    if req.max_tokens_per_day is not None:
        _budget_limits["max_tokens_per_day"] = req.max_tokens_per_day
    if req.max_requests_per_hour is not None:
        _budget_limits["max_requests_per_hour"] = req.max_requests_per_hour
    return {"status": "updated", "limits": _budget_limits}


# ---- Prometheus metrics ----

metrics_router = APIRouter(tags=["metrics"])


@metrics_router.get("/metrics")
async def prometheus_metrics(request: Request):
    """Prometheus-compatible metrics endpoint."""
    try:
        from ghost.telemetry.aggregator import TelemetryAggregator

        db_path = _telemetry_db_path(request)
        if not db_path.exists():
            from starlette.responses import PlainTextResponse

            return PlainTextResponse("# no telemetry data\n", media_type="text/plain")

        agg = TelemetryAggregator(db_path)
        try:
            stats = agg.summary()
        finally:
            agg.close()

        avg_latency_ms = (
            (stats.total_latency / stats.total_calls) * 1000
            if stats.total_calls
            else 0.0
        )

        lines = [
            "# HELP ghost_requests_total Total requests processed",
            "# TYPE ghost_requests_total counter",
            f"ghost_requests_total {stats.total_calls}",
            "# HELP ghost_tokens_total Total tokens generated",
            "# TYPE ghost_tokens_total counter",
            f"ghost_tokens_total {stats.total_tokens}",
            "# HELP ghost_latency_avg_ms Average latency in milliseconds",
            "# TYPE ghost_latency_avg_ms gauge",
            f"ghost_latency_avg_ms {avg_latency_ms}",
        ]
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")
    except Exception as exc:
        logger.warning("Failed to collect Prometheus metrics: %s", exc)
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("# No metrics available\n", media_type="text/plain")


# ---- WebSocket streaming routes ----

websocket_router = APIRouter(tags=["websocket"])
_SYNC_STREAM_END = object()


async def _next_sync_stream(iterator: Any) -> Any:
    """Fetch one sync-stream item without blocking or racing cancellation."""
    next_task = asyncio.create_task(asyncio.to_thread(next, iterator, _SYNC_STREAM_END))
    try:
        return await asyncio.shield(next_task)
    except asyncio.CancelledError:
        # ``to_thread`` cannot stop a running ``next()``. Wait for it before
        # allowing the iterator to be closed so cancellation cannot race a
        # generator that is still executing in the worker thread.
        try:
            await next_task
        except Exception:
            pass
        raise


async def _iterate_sync_stream(iterator: Any):
    """Adapt a blocking iterator to an async generator, closing it safely."""
    try:
        while True:
            item = await _next_sync_stream(iterator)
            if item is _SYNC_STREAM_END:
                return
            yield item
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            try:
                await asyncio.to_thread(close)
            except Exception as exc:
                logger.warning("Failed to close synchronous engine stream: %s", exc)


def _record_ws_trace(
    trace_store,
    *,
    query: str,
    result: str,
    model: str,
    started_at: float,
    ended_at: float,
) -> None:
    """Record a trace for a completed WebSocket chat (best-effort)."""
    if trace_store is None or not result:
        return
    from ghost.traces.collector import record_response_trace

    record_response_trace(
        trace_store,
        query=query,
        result=result,
        model=model,
        started_at=started_at,
        ended_at=ended_at,
    )


@websocket_router.websocket("/v1/chat/stream")
async def websocket_chat_stream(websocket: WebSocket):
    """Stream chat responses over a WebSocket connection.

    Accepts JSON messages of the form::

        {"message": "...", "model": "...", "agent": "..."}

    Sends back JSON chunks::

        {"type": "chunk", "content": "..."}   -- per-token streaming
        {"type": "done",  "content": "..."}   -- final assembled response
        {"type": "error", "detail": "..."}    -- on failure
    """
    from ghost.server.auth_middleware import authenticate_websocket

    expected_key = getattr(websocket.app.state, "api_key", "")
    authorized, subprotocol = authenticate_websocket(websocket, expected_key)
    if not authorized:
        # Closing before accept rejects the HTTP upgrade request.
        await websocket.close(code=1008)
        return
    await websocket.accept(subprotocol=subprotocol)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                await websocket.send_json(
                    {"type": "error", "detail": "Invalid JSON"},
                )
                continue

            message = data.get("message")
            if not message:
                await websocket.send_json(
                    {"type": "error", "detail": "Missing 'message' field"},
                )
                continue

            model = data.get("model") or getattr(
                websocket.app.state,
                "model",
                "default",
            )
            engine = getattr(websocket.app.state, "engine", None)
            if engine is None:
                await websocket.send_json(
                    {"type": "error", "detail": "No engine configured"},
                )
                continue

            messages = [{"role": "user", "content": message}]

            # This WS path streams straight from the engine (no agent /
            # TraceCollector), so record the interaction directly once it
            # finishes — otherwise WebSocket chats never reach traces.db.
            import time as _time

            trace_store = getattr(websocket.app.state, "trace_store", None)
            _ws_started_at = _time.time()

            try:
                # Prefer streaming if the engine supports it
                stream_fn = getattr(engine, "stream", None)
                if stream_fn is not None and (
                    inspect.isasyncgenfunction(stream_fn) or callable(stream_fn)
                ):
                    full_content = ""
                    try:
                        gen = stream_fn(messages, model=model)
                        # Handle both async and sync generators
                        if inspect.isasyncgen(gen):
                            async for token in gen:
                                full_content += token
                                await websocket.send_json(
                                    {"type": "chunk", "content": token},
                                )
                        else:
                            # Each ``next()`` can perform a blocking upstream
                            # read, so offload iteration one item at a time.
                            async for token in _iterate_sync_stream(iter(gen)):
                                full_content += token
                                await websocket.send_json(
                                    {"type": "chunk", "content": token},
                                )
                    except TypeError:
                        # stream() didn't return an iterable; fall back to
                        # generate(). It makes a blocking upstream call, so run
                        # it in a worker thread to keep the event loop free.
                        result = await asyncio.to_thread(
                            engine.generate, messages, model=model
                        )
                        content = (
                            result.get("content", "")
                            if isinstance(
                                result,
                                dict,
                            )
                            else str(result)
                        )
                        full_content = content
                        await websocket.send_json(
                            {"type": "chunk", "content": content},
                        )
                    await websocket.send_json(
                        {"type": "done", "content": full_content},
                    )
                    _record_ws_trace(
                        trace_store,
                        query=message,
                        result=full_content,
                        model=model,
                        started_at=_ws_started_at,
                        ended_at=_time.time(),
                    )
                else:
                    # No stream method — single-shot generate. Blocking upstream
                    # call, so run in a worker thread to keep the event loop free.
                    result = await asyncio.to_thread(
                        engine.generate, messages, model=model
                    )
                    content = (
                        result.get("content", "")
                        if isinstance(
                            result,
                            dict,
                        )
                        else str(result)
                    )
                    await websocket.send_json(
                        {"type": "chunk", "content": content},
                    )
                    await websocket.send_json(
                        {"type": "done", "content": content},
                    )
                    _record_ws_trace(
                        trace_store,
                        query=message,
                        result=content,
                        model=model,
                        started_at=_ws_started_at,
                        ended_at=_time.time(),
                    )
            except WebSocketDisconnect:
                raise
            except Exception as exc:
                await websocket.send_json(
                    {"type": "error", "detail": str(exc)},
                )
    except WebSocketDisconnect:
        pass  # Client disconnected — nothing to clean up


# ---- Learning routes ----

learning_router = APIRouter(prefix="/v1/learning", tags=["learning"])


@learning_router.get("/stats")
async def learning_stats(request: Request):
    """Return learning system statistics across all sub-policies."""
    result: Dict[str, Any] = {}

    # Skill discovery
    try:
        from ghost.learning.agents.skill_discovery import SkillDiscovery

        discovery = SkillDiscovery()
        result["skill_discovery"] = {
            "available": True,
            "discovered_count": len(discovery.discovered_skills),
        }
    except Exception as exc:
        logger.warning("Failed to load skill discovery stats: %s", exc)
        result["skill_discovery"] = {"available": False}

    return result


@learning_router.get("/policy")
async def learning_policy(request: Request):
    """Return current routing policy configuration."""
    result: Dict[str, Any] = {}

    # Load config and extract learning section
    try:
        from ghost.core.config import load_config

        config = load_config()
        lc = config.learning
        result["enabled"] = lc.enabled
        result["update_interval"] = lc.update_interval
        result["auto_update"] = lc.auto_update
        result["routing"] = {
            "policy": lc.routing.policy,
            "min_samples": lc.routing.min_samples,
        }
        result["intelligence"] = {
            "policy": lc.intelligence.policy,
        }
        result["agent"] = {
            "policy": lc.agent.policy,
        }
        result["metrics"] = {
            "accuracy_weight": lc.metrics.accuracy_weight,
            "latency_weight": lc.metrics.latency_weight,
            "cost_weight": lc.metrics.cost_weight,
            "efficiency_weight": lc.metrics.efficiency_weight,
        }
    except Exception as exc:
        logger.warning("Failed to load learning config: %s", exc)
        result["enabled"] = False
        result["routing"] = {"policy": "heuristic", "min_samples": 5}
        result["intelligence"] = {"policy": "none"}
        result["agent"] = {"policy": "none"}
        result["metrics"] = {}

    return result


# ---- Speech routes ----

speech_router = APIRouter(prefix="/v1/speech", tags=["speech"])


# Whisper sizes the app may request. "small" is the minimum that handles
# Urdu well; "large-v3-turbo" is the most accurate (~1.6 GB).
_STT_MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3-turbo")
_stt_prepare: Dict[str, Dict[str, str]] = {}


@speech_router.get("/stt/status")
async def stt_status(request: Request, model: str = "small"):
    """Whether the speech-recognition model *model* is downloaded and loaded."""
    backend = getattr(request.app.state, "speech_backend", None)
    if backend is None:
        return {"available": False, "state": "unavailable", "model": model}
    if hasattr(backend, "has_model_size") and backend.has_model_size(model):
        return {"available": True, "state": "ready", "model": model}
    job = _stt_prepare.get(model, {})
    return {"available": True, "state": job.get("state", "missing"), "error": job.get("error", ""), "model": model}


@speech_router.post("/stt/prepare")
async def stt_prepare(request: Request, model: str = "small"):
    """Download/load a speech-recognition model in the background."""
    if model not in _STT_MODEL_SIZES:
        raise HTTPException(status_code=400, detail=f"Unknown speech model '{model}'")
    backend = getattr(request.app.state, "speech_backend", None)
    if backend is None or not hasattr(backend, "ensure_model_size"):
        raise HTTPException(status_code=501, detail="Speech backend not configured")
    if _stt_prepare.get(model, {}).get("state") == "downloading":
        return {"state": "downloading", "model": model}
    _stt_prepare[model] = {"state": "downloading"}

    def load() -> None:
        try:
            backend.ensure_model_size(model)
            _stt_prepare[model] = {"state": "ready"}
        except Exception as exc:  # network, disk, …
            _stt_prepare[model] = {"state": "error", "error": str(exc)[:300]}

    asyncio.get_running_loop().run_in_executor(None, load)
    return {"state": "downloading", "model": model}


@speech_router.post("/transcribe")
async def transcribe_speech(request: Request):
    """Transcribe uploaded audio to text."""
    backend = getattr(request.app.state, "speech_backend", None)
    if backend is None:
        raise HTTPException(status_code=501, detail="Speech backend not configured")

    form = await request.form()
    audio_file = form.get("file")
    if audio_file is None:
        raise HTTPException(status_code=400, detail="Missing 'file' field")

    audio_bytes = await audio_file.read()
    language = form.get("language")
    model_size = form.get("model") or None
    if model_size and model_size not in _STT_MODEL_SIZES:
        raise HTTPException(status_code=400, detail=f"Unknown speech model '{model_size}'")

    # Detect format from filename
    filename = getattr(audio_file, "filename", "audio.wav")
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "wav"

    try:
        kwargs = {"format": ext, "language": language or None}
        if model_size and hasattr(backend, "ensure_model_size"):
            kwargs["model_size"] = model_size
        result = await asyncio.to_thread(backend.transcribe, audio_bytes, **kwargs)
    except Exception as exc:
        logger.exception("Speech transcription failed")
        raise HTTPException(
            status_code=500,
            detail=f"Speech transcription failed: {exc}",
        ) from exc

    return {
        "text": result.text,
        "language": result.language,
        "confidence": result.confidence,
        "duration_seconds": result.duration_seconds,
    }


@speech_router.get("/health")
async def speech_health(request: Request):
    """Check if a speech backend is available."""
    backend = getattr(request.app.state, "speech_backend", None)
    if backend is None:
        return {"available": False, "reason": "No speech backend configured"}
    try:
        available = backend.health()
        reason = None
    except Exception as exc:
        logger.exception("Speech health check failed")
        available = False
        reason = str(exc)

    if not available and reason is None:
        last_error = getattr(backend, "last_error", None)
        if callable(last_error):
            reason = last_error()

    return {
        "available": available,
        "backend": backend.backend_id,
        **({"reason": reason} if reason else {}),
    }


# Synthesis is CPU-bound and runs in a worker thread; cap the input so one
# request cannot occupy that thread for minutes.
_MAX_TTS_CHARS = 5000


async def _resolve_tts_backend(request: Request):
    """Return a healthy TTS backend, resolved once and cached on app state.

    Backends load models on construction, so this stays lazy: a server whose
    users never ask for audio never pays for it.
    """
    app = request.app
    if getattr(app.state, "tts_resolved", False):
        return getattr(app.state, "tts_backend", None)

    # The first health probe can load Kokoro's model. A shared task keeps that
    # work off the event loop and coalesces concurrent probes, including when
    # the backend is unavailable. A later request may retry that failure.
    task = getattr(app.state, "tts_resolution_task", None)
    if task is None:

        def discover():
            from ghost.speech._tts_discovery import (
                get_tts_backend,
                voice_preferences,
            )

            config = getattr(app.state, "config", None)
            if config is None:
                from ghost.core.config import load_config

                config = load_config()

            preferred, _, _ = voice_preferences(config)
            return get_tts_backend(preferred)

        task = asyncio.create_task(asyncio.to_thread(discover))
        app.state.tts_resolution_task = task

    try:
        backend = await asyncio.shield(task)
    finally:
        if task.done() and getattr(app.state, "tts_resolution_task", None) is task:
            app.state.tts_resolution_task = None

    if backend is not None:
        app.state.tts_backend = backend
        app.state.tts_resolved = True
    return backend


def _tts_voice_and_speed(request: Request, backend) -> tuple[str, float]:
    """Resolve the voice ID and speed to use with *backend*.

    Voice IDs are not portable between backends, so a configured ``voice_id``
    applies only when the resolved backend is the configured one; otherwise the
    fallback backend's own default is used.
    """
    from ghost.speech._tts_discovery import default_voice_for, voice_preferences

    config = getattr(request.app.state, "config", None)
    if config is None:
        return "", 1.0

    preferred, voice_id, speed = voice_preferences(config)
    active = getattr(backend, "backend_id", "")
    if active and active != preferred:
        voice_id = default_voice_for(active)
    return voice_id, speed


# Engines the app can pick explicitly (Settings → Assistant → Voice).
_SELECTABLE_TTS = ("kokoro_onnx", "elevenlabs")


def _named_tts_backend(request: Request, name: str):
    """Return a ready instance of the TTS backend *name*, or None.

    Instances are cached per engine; a cached one is re-checked each time so a
    just-finished model download or a newly saved API key takes effect.
    """
    if name not in _SELECTABLE_TTS:
        return None
    import ghost.speech  # noqa: F401  (registers built-in backends)
    from ghost.core.registry import TTSRegistry

    if not TTSRegistry.contains(name):
        return None
    cache = getattr(request.app.state, "tts_engines", None)
    if cache is None:
        cache = request.app.state.tts_engines = {}
    backend = cache.get(name)
    try:
        if backend is None or not backend.health():
            backend = TTSRegistry.get(name)()
            if not backend.health():
                return None
            cache[name] = backend
    except Exception:
        logger.debug("TTS engine %s unavailable", name, exc_info=True)
        return None
    return backend


@speech_router.get("/voices")
async def list_voice_engines(request: Request):
    """Which voice engines are ready, plus the offline model's download state."""
    from ghost.speech import kokoro_onnx_tts as kokoro

    def probe():
        return {name: _named_tts_backend(request, name) is not None for name in _SELECTABLE_TTS}

    ready = await asyncio.to_thread(probe)
    return {
        "kokoro_onnx": {
            "available": ready.get("kokoro_onnx", False),
            "download": kokoro.download_status(),
            "voices": kokoro.VOICES,
        },
        "elevenlabs": {"available": ready.get("elevenlabs", False)},
    }


@speech_router.get("/voices/elevenlabs")
async def list_elevenlabs_voices(request: Request):
    """Voices in the user's ElevenLabs account, for the app's voice picker."""
    backend = await asyncio.to_thread(_named_tts_backend, request, "elevenlabs")
    if backend is None:
        raise HTTPException(status_code=503, detail="ElevenLabs is not connected")
    try:
        voices = await asyncio.to_thread(backend.voice_catalog)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {"voices": voices}


@speech_router.post("/voices/kokoro_onnx/download")
async def download_offline_voice():
    """Start the one-time offline voice model download (~350 MB)."""
    from ghost.speech import kokoro_onnx_tts as kokoro

    return kokoro.start_model_download()


@speech_router.post("/synthesize")
async def synthesize_speech(request: Request, body: SpeechSynthesizeRequest):
    """Synthesize text to speech and return the audio as WAV."""
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing 'text'")
    if len(text) > _MAX_TTS_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Text exceeds {_MAX_TTS_CHARS} characters",
        )

    if body.backend:
        backend = await asyncio.to_thread(_named_tts_backend, request, body.backend)
        if backend is None:
            raise HTTPException(
                status_code=503,
                detail=f"Voice engine '{body.backend}' is not ready",
            )
    else:
        backend = await _resolve_tts_backend(request)
    if backend is None:
        raise HTTPException(
            status_code=501, detail="No text-to-speech backend available"
        )

    voice_id, speed = _tts_voice_and_speed(request, backend)
    if body.voice_id:
        voice_id = body.voice_id
    if body.speed is not None:
        speed = body.speed

    try:
        result = await asyncio.to_thread(
            backend.synthesize,
            text,
            voice_id=voice_id,
            speed=speed,
            output_format="wav",
        )
    except Exception as exc:
        logger.exception("Speech synthesis failed")
        raise HTTPException(status_code=500, detail="Speech synthesis failed") from exc

    return Response(
        content=result.audio,
        media_type="audio/wav",
        headers={
            "X-Voice-Id": result.voice_id or voice_id,
            "X-Sample-Rate": str(result.sample_rate),
        },
    )


@speech_router.get("/tts/health")
async def tts_health(request: Request):
    """Report whether voice output is available, and with which voice."""
    backend = await _resolve_tts_backend(request)
    if backend is None:
        return {"available": False, "reason": "No text-to-speech backend available"}

    voice_id, speed = _tts_voice_and_speed(request, backend)
    return {
        "available": True,
        "backend": getattr(backend, "backend_id", ""),
        "voice_id": voice_id,
        "speed": speed,
    }


# ---- Feedback routes ----

feedback_router = APIRouter(prefix="/v1/feedback", tags=["feedback"])


@feedback_router.post("")
async def submit_feedback(req: FeedbackScoreRequest, request: Request):
    """Submit feedback for a trace."""
    try:
        from ghost.core.config import DEFAULT_CONFIG_DIR
        from ghost.traces.store import TraceStore

        db_path = DEFAULT_CONFIG_DIR / "traces.db"
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="No trace database")

        store = TraceStore(db_path)
        updated = store.update_feedback(req.trace_id, req.score)
        store.close()

        if not updated:
            raise HTTPException(
                status_code=404, detail=f"Trace '{req.trace_id}' not found"
            )
        return {"status": "recorded", "trace_id": req.trace_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@feedback_router.get("/stats")
async def feedback_stats(request: Request):
    """Get feedback statistics."""
    return {"total": 0, "mean_score": 0.0}


# ---- Optimize routes ----

optimize_router = APIRouter(prefix="/v1/optimize", tags=["optimize"])


@optimize_router.get("/runs")
async def list_optimize_runs(request: Request):
    """List optimization runs."""
    try:
        from ghost.core.config import DEFAULT_CONFIG_DIR
        from ghost.learning.optimize.store import OptimizationStore

        db_path = DEFAULT_CONFIG_DIR / "optimize.db"
        if not db_path.exists():
            return {"runs": []}

        store = OptimizationStore(db_path)
        runs = store.list_runs()
        store.close()
        return {"runs": runs}
    except Exception as exc:
        logger.warning("Failed to list optimization runs: %s", exc)
        return {"runs": []}


# ---------------------------------------------------------------------------
# Stonic port feature flags
# ---------------------------------------------------------------------------

stonic_router = APIRouter(prefix="/v1/stonic", tags=["stonic"])

# Flags the UI is allowed to read. Listed explicitly rather than reflected off
# the dataclass so that adding an internal field to StonicConfig cannot widen
# the public surface by accident.
_STONIC_FLAG_NAMES = (
    "ui_sounds",
    "characters",
    "computer_use",
    "computer_overlay",
    "realtime_voice",
    "wake_word",
    "facelock",
    "gestures",
    "awareness",
    "desktop_presence",
    "memory_v2",
)


@stonic_router.get("/flags")
async def stonic_flags(request: Request):
    """Report which ported Stonic features are enabled.

    Every flag is off unless the user turned it on, so a failure to read the
    config must report *off* rather than erroring: the UI treats this endpoint
    as advisory and renders Ghost's own behaviour when it is unavailable.
    """
    flags = dict.fromkeys(_STONIC_FLAG_NAMES, False)
    try:
        config = getattr(request.app.state, "config", None)
        if config is None:
            from ghost.core.config import load_config  # noqa: PLC0415

            config = load_config()
        stonic = getattr(config, "stonic", None)
        if stonic is not None:
            for name in _STONIC_FLAG_NAMES:
                flags[name] = bool(getattr(stonic, name, False))
    except Exception as exc:
        logger.warning("Failed to read Stonic feature flags: %s", exc)
        return {"flags": dict.fromkeys(_STONIC_FLAG_NAMES, False), "available": False}
    return {"flags": flags, "available": True}


@stonic_router.get("/characters")
async def stonic_characters(request: Request):
    """List the character delivery profiles available to the voice engine.

    Reports an empty list when the `characters` flag is off, so the UI needs no
    separate flag check before rendering the picker. Like the flags endpoint,
    this is advisory and never raises.
    """
    try:
        config = getattr(request.app.state, "config", None)
        if config is None:
            from ghost.core.config import load_config  # noqa: PLC0415

            config = load_config()
        enabled = bool(getattr(getattr(config, "stonic", None), "characters", False))
        if not enabled:
            return {"characters": [], "enabled": False}

        from ghost.speech.characters import (  # noqa: PLC0415
            DEFAULT_CHARACTER,
            selectable_characters,
        )

        found = selectable_characters()
        return {
            "characters": [c.to_dict() for c in found],
            "enabled": True,
            "default": DEFAULT_CHARACTER,
        }
    except Exception as exc:
        logger.warning("Failed to list Stonic characters: %s", exc)
        return {"characters": [], "enabled": False}


# ---------------------------------------------------------------------------
# Computer use (Stonic port, Phase 2)
# ---------------------------------------------------------------------------

computer_router = APIRouter(prefix="/v1/computer", tags=["computer"])


def _ocr_status_for(config: Any) -> Dict[str, Any]:
    """Locate and run Tesseract the way the sidecar will. Never raises."""
    from ghost.computer import protocol  # noqa: PLC0415

    stonic = getattr(config, "stonic", None)
    location = protocol.locate_tesseract(
        explicit=str(getattr(stonic, "tesseract_path", "") or ""),
        tessdata=str(getattr(stonic, "tessdata_path", "") or ""),
    )
    return protocol.probe_tesseract(location).to_dict()


@computer_router.get("/ocr/status")
async def computer_ocr_status(request: Request):
    """Whether Tesseract OCR is usable for computer use, and why not if not.

    Reported regardless of the `computer_use` flag, so Settings can show the
    OCR state before the user turns the feature on. The probe actually runs
    the binary (``--version`` and ``--list-langs``), so "available" means OCR
    works, not merely that a file exists — the gap Stonic's installer fell into.
    """
    try:
        config = getattr(request.app.state, "config", None)
        if config is None:
            from ghost.core.config import load_config  # noqa: PLC0415

            config = load_config()
        enabled = bool(getattr(getattr(config, "stonic", None), "computer_use", False))
        status = await asyncio.to_thread(_ocr_status_for, config)
        return {**status, "computer_use_enabled": enabled}
    except Exception as exc:
        logger.warning("OCR status probe failed: %s", exc)
        return {
            "available": False,
            "source": "missing",
            "path": None,
            "tessdata": None,
            "version": "",
            "languages": [],
            "has_english": False,
            "error": f"OCR status could not be determined: {exc}"[:300],
            "computer_use_enabled": False,
        }


# ---------------------------------------------------------------------------
# Realtime voice engine (Stonic port, Phase 3)
# ---------------------------------------------------------------------------
#
# A second, opt-in voice engine. Ghost's default Kokoro + faster-whisper path
# is not touched by any of these routes. Every route except /status refuses
# with 403 unless [stonic].realtime_voice is on.

realtime_router = APIRouter(prefix="/v1/speech/realtime", tags=["speech"])


class RealtimeStartBody(BaseModel):
    # "provider" or "provider:model"; empty picks from the saved keys.
    brain: str = ""
    # "tts" (directed Gemini TTS, the bracket performance) or "live".
    voice_mode: str = "tts"
    character: Dict[str, Any] = {}


def _realtime_settings(request: Request) -> Any:
    config = getattr(request.app.state, "config", None)
    if config is None:
        from ghost.core.config import load_config  # noqa: PLC0415

        config = load_config()
    return getattr(config, "stonic", None)


def _realtime_enabled(request: Request) -> bool:
    try:
        return bool(getattr(_realtime_settings(request), "realtime_voice", False))
    except Exception as exc:  # noqa: BLE001 - unreadable config means off
        logger.warning("Failed to read realtime_voice flag: %s", exc)
        return False


def _realtime_refusal(exc: Any) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.to_dict())


def _realtime_guard(request: Request) -> Optional[JSONResponse]:
    if _realtime_enabled(request):
        return None
    return JSONResponse(
        status_code=403,
        content={
            "ok": False,
            "error": "disabled",
            "message": "Turn on [stonic].realtime_voice to use the realtime engine.",
        },
    )


@realtime_router.get("/status")
async def realtime_status(request: Request):
    """Flag, runtime install state, engine state and which keys are saved.

    Reports key *presence* only, never values. Advisory: never raises.
    """
    from ghost.runtimes import install_status  # noqa: PLC0415
    from ghost.speech import realtime  # noqa: PLC0415

    keys = realtime.keys_from_env()
    manager = realtime.get_manager()
    stonic = None
    try:
        stonic = _realtime_settings(request)
    except Exception:  # noqa: BLE001
        pass
    try:
        runtime = install_status("voice")
    except Exception as exc:  # noqa: BLE001
        runtime = {"state": "error", "error": str(exc)[:300]}
    return {
        "enabled": _realtime_enabled(request),
        "wake_word": bool(getattr(stonic, "wake_word", False)),
        "wake_word_name": str(getattr(stonic, "wake_word_name", "") or "") or "Ghost",
        "runtime": runtime,
        "engine": manager.describe(),
        "keys": {p: p in keys for p in realtime.PROVIDER_ENV},
    }


@realtime_router.post("/install")
async def realtime_install(request: Request):
    """Install the voice runtime (about 375 MB) in the background."""
    if (refused := _realtime_guard(request)) is not None:
        return refused
    from ghost.runtimes import start_install  # noqa: PLC0415

    return await asyncio.to_thread(start_install, "voice")


@realtime_router.post("/start")
async def realtime_start(request: Request, body: RealtimeStartBody):
    """Start the engine (if needed) and push keys from the OS keyring env."""
    if (refused := _realtime_guard(request)) is not None:
        return refused
    from ghost.speech import realtime  # noqa: PLC0415

    try:
        stonic = _realtime_settings(request)
        memory = ""
        if _stonic_flag(request, "memory_v2"):
            # Phase 6: the voice remembers — its memory card goes to the engine.
            from ghost.memory.v2 import bridge  # noqa: PLC0415

            char_id = str((body.character or {}).get("id") or "") or None
            memory = await asyncio.to_thread(bridge.voice_memory_card, char_id)
        payload = realtime.build_engine_config(
            brain=body.brain,
            voice_mode=body.voice_mode,
            character=body.character,
            memory=memory,
            wake_word_name=str(getattr(stonic, "wake_word_name", "") or ""),
        )
        missing = realtime.missing_for(payload)
        if missing:
            raise realtime.RealtimeError(
                "key_missing",
                "Missing keys for: " + ", ".join(missing),
            )
        manager = realtime.get_manager()
        engine = await manager.start()
        result = await manager.configure(payload)
    except realtime.RealtimeError as exc:
        return _realtime_refusal(exc)
    return {
        "ok": True,
        "engine": engine,
        "brain": payload["brain"],
        "voice_mode": payload["voice"],
        "missing": result.get("missing", []),
    }


@realtime_router.post("/stop")
async def realtime_stop(request: Request):
    """Stop the engine and make it forget the keys. Allowed even when off."""
    from ghost.speech import realtime  # noqa: PLC0415

    return await realtime.get_manager().stop()


@realtime_router.post("/hangup")
async def realtime_hangup(request: Request):
    from ghost.speech import realtime  # noqa: PLC0415

    try:
        return await realtime.get_manager().hangup()
    except realtime.RealtimeError as exc:
        return _realtime_refusal(exc)


@realtime_router.api_route("/offer", methods=["POST", "PATCH"])
async def realtime_offer(request: Request):
    """Proxy the SmallWebRTC offer / ICE candidates to the engine."""
    if (refused := _realtime_guard(request)) is not None:
        return refused
    from ghost.speech import realtime  # noqa: PLC0415

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "bad_offer"}
        )
    try:
        status, answer = await realtime.get_manager().offer(request.method, body)
    except realtime.RealtimeError as exc:
        return _realtime_refusal(exc)
    return JSONResponse(status_code=status, content=answer)


def _wake_guard(request: Request) -> Optional[JSONResponse]:
    if (refused := _realtime_guard(request)) is not None:
        return refused
    if bool(getattr(_realtime_settings(request), "wake_word", False)):
        return None
    return JSONResponse(
        status_code=403,
        content={
            "ok": False,
            "error": "disabled",
            "message": "Turn on [stonic].wake_word to use the wake word.",
        },
    )


@realtime_router.post("/wake/start")
async def realtime_wake_start(request: Request):
    """Listen for the assistant's name, transcribing locally with Whisper."""
    if (refused := _wake_guard(request)) is not None:
        return refused
    backend = getattr(request.app.state, "speech_backend", None)
    if backend is None or not hasattr(backend, "transcribe"):
        return JSONResponse(
            status_code=501,
            content={"ok": False, "error": "no_stt", "message": "Speech not set up"},
        )
    from ghost.speech import wake_word  # noqa: PLC0415

    name = str(getattr(_realtime_settings(request), "wake_word_name", "") or "")
    listener = await asyncio.to_thread(wake_word.ensure_listener, backend, name)
    listener.resume()
    return {"ok": True, **listener.describe()}


@realtime_router.post("/wake/stop")
async def realtime_wake_stop(request: Request):
    from ghost.speech import wake_word  # noqa: PLC0415

    await asyncio.to_thread(wake_word.stop_listener)
    return {"ok": True, "running": False}


@realtime_router.post("/wake/pause")
async def realtime_wake_pause(request: Request, resume: bool = False):
    """Pause while a realtime call holds the mic; resume when it ends."""
    from ghost.speech import wake_word  # noqa: PLC0415

    listener = wake_word.get_listener()
    if listener is None:
        return {"ok": True, "running": False}
    listener.resume() if resume else listener.pause()
    return {"ok": True, **listener.describe()}


@realtime_router.get("/wake/events")
async def realtime_wake_events(request: Request, after: int = 0):
    from ghost.speech import wake_word  # noqa: PLC0415

    listener = wake_word.get_listener()
    if listener is None:
        return {"running": False, "events": [], "last_seq": after}
    return {**listener.describe(), "events": listener.events(after)}


# ---------------------------------------------------------------------------
# Face Lock and gesture models (Stonic port, Phase 4)
# ---------------------------------------------------------------------------
#
# The WebView sees and measures; every decision is made in
# ghost.security.facelock. Face prints and the PIN hash never leave it.

facelock_router = APIRouter(prefix="/v1/security/facelock", tags=["security"])
facelock_assets_router = APIRouter(tags=["security"])


class FaceVerifyBody(BaseModel):
    embeddings: List[List[float]] = []
    purpose: str = "unlock"


class PinVerifyBody(BaseModel):
    pin: str = ""
    purpose: str = "unlock"


class EnrollBody(BaseModel):
    templates: List[List[float]] = []
    pin: str = ""
    ticket: Optional[str] = None


class TicketBody(BaseModel):
    ticket: Optional[str] = None
    pin: str = ""


def _stonic_flag(request: Request, name: str) -> bool:
    try:
        config = getattr(request.app.state, "config", None)
        if config is None:
            from ghost.core.config import load_config  # noqa: PLC0415

            config = load_config()
        return bool(getattr(getattr(config, "stonic", None), name, False))
    except Exception as exc:  # noqa: BLE001 - unreadable config means off
        logger.warning("Failed to read [stonic].%s: %s", name, exc)
        return False


def _facelock_manager(request: Request):
    """The manager, or a 403 response when the flag is off."""
    if not _stonic_flag(request, "facelock"):
        return None, JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "reason": "disabled",
                "message": "Turn on [stonic].facelock to use Face Lock.",
            },
        )
    from ghost.security import facelock  # noqa: PLC0415

    return facelock.ensure_manager(), None


_FACELOCK_OFF_STATUS = {
    "supported": False,
    "enabled": False,
    "locked": False,
    "templateCount": 0,
    "attemptsLeft": 0,
    "maxAttempts": 0,
    "lockoutMs": 0,
    "pinLength": 6,
}


@facelock_router.get("/status")
async def facelock_status(request: Request):
    """Advisory and read-only: never the templates or the PIN hash."""
    if not _stonic_flag(request, "facelock"):
        return {**_FACELOCK_OFF_STATUS, "available": False}
    from ghost.security import facelock  # noqa: PLC0415

    return {**facelock.ensure_manager().status(), "available": True}


@facelock_router.post("/verify-face")
async def facelock_verify_face(request: Request, body: FaceVerifyBody):
    manager, refused = _facelock_manager(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(manager.verify_face, body.embeddings, body.purpose)


@facelock_router.post("/verify-pin")
async def facelock_verify_pin(request: Request, body: PinVerifyBody):
    manager, refused = _facelock_manager(request)
    if refused is not None:
        return refused
    # scrypt takes ~50 ms; keep it off the event loop.
    return await asyncio.to_thread(manager.verify_pin, body.pin, body.purpose)


@facelock_router.post("/enroll")
async def facelock_enroll(request: Request, body: EnrollBody):
    manager, refused = _facelock_manager(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(
        manager.enroll, body.templates, body.pin, body.ticket
    )


@facelock_router.post("/change-pin")
async def facelock_change_pin(request: Request, body: TicketBody):
    manager, refused = _facelock_manager(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(manager.change_pin, body.pin, body.ticket)


@facelock_router.post("/disable")
async def facelock_disable(request: Request, body: TicketBody):
    manager, refused = _facelock_manager(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(manager.disable, body.ticket)


@facelock_router.post("/lock")
async def facelock_lock_now(request: Request):
    manager, refused = _facelock_manager(request)
    if refused is not None:
        return refused
    return {"ok": manager.lock("manual"), "status": manager.status()}


_MODEL_BUNDLE_FLAGS = {"facelock": "facelock", "gestures": "gestures"}


def _bundle_refusal(request: Request, bundle: str) -> Optional[JSONResponse]:
    flag = _MODEL_BUNDLE_FLAGS.get(bundle)
    if flag is None:
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "bad_bundle"}
        )
    if not _stonic_flag(request, flag):
        message = f"[stonic].{flag} is off"
        return JSONResponse(
            status_code=403,
            content={"ok": False, "error": "disabled", "message": message},
        )
    return None


@facelock_router.get("/models")
async def facelock_models_status(request: Request, bundle: str = "facelock"):
    if (refused := _bundle_refusal(request, bundle)) is not None:
        return refused
    from ghost.security import facelock_models  # noqa: PLC0415

    return facelock_models.download_status(bundle)


@facelock_router.post("/models/download")
async def facelock_models_download(request: Request, bundle: str = "facelock"):
    if (refused := _bundle_refusal(request, bundle)) is not None:
        return refused
    from ghost.security import facelock_models  # noqa: PLC0415

    return facelock_models.start_download(bundle)


@facelock_assets_router.get("/facelock-assets/{name}")
async def facelock_asset(request: Request, name: str):
    """Serve a downloaded, hash-verified public model/runtime file.

    Outside /v1 (like the UI's own files) because MediaPipe and ONNX Runtime
    fetch these themselves without Ghost's auth header. Only the fixed list of
    public files can be named, only while Face Lock or hand control is on, and
    each file is checked against its pinned SHA-256 before it is first served.
    """
    from fastapi.responses import FileResponse  # noqa: PLC0415

    from ghost.security import facelock_models  # noqa: PLC0415

    asset = facelock_models.ASSETS.get(name)
    if asset is None or not (
        _stonic_flag(request, "facelock") or _stonic_flag(request, "gestures")
    ):
        return JSONResponse(status_code=404, content={"error": "not_found"})
    path = await asyncio.to_thread(facelock_models.asset_path, name)
    if path is None:
        return JSONResponse(status_code=404, content={"error": "not_downloaded"})
    return FileResponse(
        path,
        media_type=asset.media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# ---------------------------------------------------------------------------
# Awareness (Stonic port, Phase 5)
# ---------------------------------------------------------------------------
#
# Off unless [stonic].awareness is on AND the user switches it on in Settings.
# Signals come from the desktop shell's sensor; nothing here reads the screen.

awareness_router = APIRouter(prefix="/v1/awareness", tags=["awareness"])


class AwarenessConfigBody(BaseModel):
    enabled: Optional[bool] = None
    character: Optional[Dict[str, Any]] = None


class AwarenessOutcomeBody(BaseModel):
    t: float
    outcome: str
    reply: str = ""


class AwarenessQuietBody(BaseModel):
    minutes: float = 60


def _awareness(request: Request):
    if not _stonic_flag(request, "awareness"):
        return None, JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "error": "disabled",
                "message": "Turn on [stonic].awareness to use awareness.",
            },
        )
    from ghost.daemon.awareness import service  # noqa: PLC0415

    return service.ensure_service(request.app.state), None


@awareness_router.get("/status")
async def awareness_status(request: Request):
    if not _stonic_flag(request, "awareness"):
        return {"available": False, "enabled": False}
    svc, _ = _awareness(request)
    return await asyncio.to_thread(svc.status)


@awareness_router.post("/configure")
async def awareness_configure(request: Request, body: AwarenessConfigBody):
    svc, refused = _awareness(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(svc.configure, body.enabled, body.character)


@awareness_router.post("/signals")
async def awareness_signals(request: Request):
    """One sample from the desktop shell's sensor. Write-only: returns nothing."""
    if not _stonic_flag(request, "awareness"):
        return JSONResponse(status_code=204, content=None)
    try:
        sample = await request.json()
    except ValueError:
        return JSONResponse(status_code=400, content={"ok": False})
    if not isinstance(sample, dict):
        return JSONResponse(status_code=400, content={"ok": False})
    svc, _ = _awareness(request)
    await asyncio.to_thread(svc.ingest, sample)
    return JSONResponse(status_code=204, content=None)


@awareness_router.post("/activity")
async def awareness_activity(request: Request):
    svc, refused = _awareness(request)
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except ValueError:
        body = {}
    svc.activity(body if isinstance(body, dict) else {})
    return {"ok": True}


@awareness_router.post("/outcome")
async def awareness_outcome(request: Request, body: AwarenessOutcomeBody):
    svc, refused = _awareness(request)
    if refused is not None:
        return refused
    ok = await asyncio.to_thread(
        svc.engine.report_outcome, body.t, body.outcome, body.reply
    )
    return {"ok": ok}


@awareness_router.post("/quiet")
async def awareness_quiet(request: Request, body: AwarenessQuietBody):
    svc, refused = _awareness(request)
    if refused is not None:
        return refused
    svc.engine.quiet_for(min(max(body.minutes, 1), 720), "you asked")
    return {"ok": True, **svc.status()}


@awareness_router.post("/think-now")
async def awareness_think_now(request: Request):
    svc, refused = _awareness(request)
    if refused is not None:
        return refused
    decision = await asyncio.to_thread(svc.engine.beat_now)
    return {"ok": decision is not None, "decision": decision}


@awareness_router.get("/events")
async def awareness_events(request: Request, after: int = 0):
    svc, refused = _awareness(request)
    if refused is not None:
        return refused
    return svc.events(after)


@awareness_router.get("/diary")
async def awareness_diary(request: Request):
    """What the eyes recorded today, the same text the model reads."""
    svc, refused = _awareness(request)
    if refused is not None:
        return refused
    snap = await asyncio.to_thread(svc.eyes.snapshot)
    keep = ("running", "digest", "recentDays", "today", "todayApps", "active")
    return {k: snap.get(k) for k in keep}


@awareness_router.post("/forget")
async def awareness_forget(request: Request):
    svc, refused = _awareness(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(svc.forget)


# ---------------------------------------------------------------------------
# Memory v2 (Stonic port, Phase 6) — Memory Core + file memory
# ---------------------------------------------------------------------------

memory_v2_router = APIRouter(prefix="/v1/memory/v2", tags=["memory-v2"])


class MemoryRecallBody(BaseModel):
    query: str
    query_en: str = ""
    mind: str = "voice"
    character_id: Optional[str] = None
    k: int = 6


class MemoryAskBody(BaseModel):
    question: str
    keywords: str = ""
    character_id: Optional[str] = None
    recent: List[Dict[str, Any]] = []


class MemoryRememberBody(BaseModel):
    text: str
    pin: bool = False
    about: str = ""
    character_id: Optional[str] = None


class MemoryFollowUpBody(BaseModel):
    what: str
    when: str = ""
    done: bool = False
    character_id: Optional[str] = None


class MemoryPatchBody(BaseModel):
    text: Optional[str] = None
    text_en: Optional[str] = None
    type: Optional[str] = None
    sensitivity: Optional[str] = None
    triggers: Optional[List[str]] = None


class MemoryPinBody(BaseModel):
    pinned: bool = True


class MemoryResolveBody(BaseModel):
    status: str = "done"


class MemoryPauseBody(BaseModel):
    paused: bool = True


class MemoryNotesCommandBody(BaseModel):
    command: str
    path: Optional[str] = None
    file_text: Optional[str] = None
    old_str: Optional[str] = None
    new_str: Optional[str] = None
    insert_line: Optional[int] = None
    insert_text: Optional[str] = None
    old_path: Optional[str] = None
    new_path: Optional[str] = None
    view_range: Optional[List[int]] = None


class MemoryNotesGrepBody(BaseModel):
    pattern: str
    path: Optional[str] = None
    glob: Optional[str] = None
    output_mode: Optional[str] = None
    head_limit: Optional[int] = None
    context: Optional[int] = None
    case_insensitive: bool = False


def _memory_v2(request: Request):
    """Memory Core, or a 403 response when ``[stonic].memory_v2`` is off."""
    if not _stonic_flag(request, "memory_v2"):
        return None, JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "error": "disabled",
                "message": "Turn on [stonic].memory_v2 to use memory v2.",
            },
        )
    from ghost.memory.v2 import bridge  # noqa: PLC0415

    core = bridge.get_core()
    if not core.ensure():
        return None, JSONResponse(
            status_code=503, content={"ok": False, "error": core.reason or "off"}
        )
    return core, None


@memory_v2_router.get("/status")
async def memory_v2_status(request: Request):
    if not _stonic_flag(request, "memory_v2"):
        return {"available": False, "enabled": False}
    from ghost.memory.v2 import bridge  # noqa: PLC0415

    core = bridge.get_core()
    status = await asyncio.to_thread(core.status)
    notes = await asyncio.to_thread(lambda: bridge.get_notes().stats())
    return {"available": True, **status, "notes": notes}


@memory_v2_router.post("/recall")
async def memory_v2_recall(request: Request, body: MemoryRecallBody):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(
        core.recall,
        body.query,
        query_en=body.query_en,
        mind=body.mind,
        character_id=body.character_id,
        k=body.k,
    )


@memory_v2_router.post("/ask")
async def memory_v2_ask(request: Request, body: MemoryAskBody):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(
        core.ask,
        body.question,
        keywords=body.keywords,
        character_id=body.character_id,
        recent=body.recent,
    )


@memory_v2_router.post("/remember")
async def memory_v2_remember(request: Request, body: MemoryRememberBody):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(
        core.remember,
        body.text,
        source="user",
        pin=body.pin,
        about=body.about,
        character_id=body.character_id,
    )


@memory_v2_router.post("/follow-up")
async def memory_v2_follow_up(request: Request, body: MemoryFollowUpBody):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(
        core.follow_up,
        body.what,
        when=body.when,
        done=body.done,
        source="user",
        character_id=body.character_id,
    )


@memory_v2_router.get("/context")
async def memory_v2_context(
    request: Request, mind: str = "voice", character_id: Optional[str] = None
):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(core.context_block, mind, character_id=character_id)


@memory_v2_router.get("/memories")
async def memory_v2_memories(
    request: Request, status: str = "active,candidate", limit: int = 200
):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    statuses = [x for x in status.split(",") if x]
    return await asyncio.to_thread(core.list_memories, status=statuses, limit=limit)


@memory_v2_router.get("/memories/{memory_id}")
async def memory_v2_memory(request: Request, memory_id: int):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(core.memory_detail, memory_id)


@memory_v2_router.patch("/memories/{memory_id}")
async def memory_v2_update(request: Request, memory_id: int, body: MemoryPatchBody):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    return await asyncio.to_thread(core.update_memory, memory_id, patch)


@memory_v2_router.post("/memories/{memory_id}/pin")
async def memory_v2_pin(request: Request, memory_id: int, body: MemoryPinBody):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(core.set_pinned, memory_id, body.pinned)


@memory_v2_router.delete("/memories/{memory_id}")
async def memory_v2_forget(request: Request, memory_id: int, hard: bool = False):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(core.forget_memory, memory_id, hard=hard)


@memory_v2_router.get("/intentions")
async def memory_v2_intentions(request: Request, status: str = "open"):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(core.list_intentions, status=status)


@memory_v2_router.post("/intentions/{intention_id}/resolve")
async def memory_v2_resolve(
    request: Request, intention_id: int, body: MemoryResolveBody
):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(core.resolve_intention, intention_id, body.status)


@memory_v2_router.post("/pause")
async def memory_v2_pause(request: Request, body: MemoryPauseBody):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(core.set_paused, body.paused)


@memory_v2_router.post("/backup")
async def memory_v2_backup(request: Request):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(core.backup)


@memory_v2_router.get("/export")
async def memory_v2_export(request: Request):
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    return await asyncio.to_thread(core.store.export_all)


@memory_v2_router.post("/migrate")
async def memory_v2_migrate(request: Request):
    """Forward migration of the v1 facts (runs once; also runs on first use)."""
    core, refused = _memory_v2(request)
    if refused is not None:
        return refused
    from ghost.memory.store import LocalFactStore  # noqa: PLC0415
    from ghost.memory.v2 import bridge  # noqa: PLC0415

    return await asyncio.to_thread(
        bridge.migrate_forward,
        LocalFactStore(bridge._default_facts_path(), max_facts=100_000),
        core,
    )


@memory_v2_router.post("/rollback")
async def memory_v2_rollback(request: Request):
    """Copy v2-only memories into the v1 facts, so memory_v2 can be switched off
    without losing anything.
    Allowed while the flag is off too — that is when a rollback is needed."""
    from ghost.memory.v2 import bridge  # noqa: PLC0415

    return await asyncio.to_thread(bridge.rollback_to_v1)


@memory_v2_router.get("/notes")
async def memory_v2_notes(request: Request):
    _, refused = _memory_v2(request)
    if refused is not None:
        return refused
    from ghost.memory.v2 import bridge  # noqa: PLC0415

    notes = bridge.get_notes()
    return {
        "ok": True,
        "items": await asyncio.to_thread(notes.list),
        "index": await asyncio.to_thread(notes.prompt_index),
    }


@memory_v2_router.post("/notes/command")
async def memory_v2_notes_command(request: Request, body: MemoryNotesCommandBody):
    """Anthropic's memory tool (memory_20250818) over the notes folder."""
    _, refused = _memory_v2(request)
    if refused is not None:
        return refused
    from ghost.memory.v2 import bridge  # noqa: PLC0415

    args = {k: v for k, v in body.model_dump().items() if v is not None}
    return await asyncio.to_thread(bridge.get_notes().command, args)


@memory_v2_router.post("/notes/grep")
async def memory_v2_notes_grep(request: Request, body: MemoryNotesGrepBody):
    _, refused = _memory_v2(request)
    if refused is not None:
        return refused
    from ghost.memory.v2 import bridge  # noqa: PLC0415

    args = {k: v for k, v in body.model_dump().items() if v is not None}
    return await asyncio.to_thread(bridge.get_notes().grep, args)


@memory_v2_router.post("/notes/migrate")
async def memory_v2_notes_migrate(request: Request):
    _, refused = _memory_v2(request)
    if refused is not None:
        return refused
    from ghost.memory.v2 import bridge  # noqa: PLC0415

    return await asyncio.to_thread(bridge.migrate_notes)


# ---------------------------------------------------------------------------
# Always-Active Stonic Mode (preset over the individual flags)
# ---------------------------------------------------------------------------


class StonicPresetBody(BaseModel):
    enabled: bool = True


@stonic_router.get("/preset")
async def stonic_preset_status(request: Request):
    from ghost.core import stonic_preset  # noqa: PLC0415

    try:
        return await asyncio.to_thread(stonic_preset.status)
    except Exception as exc:  # noqa: BLE001 — advisory, like /flags
        logger.warning("Failed to read the Stonic preset: %s", exc)
        return {
            "preset": stonic_preset.PRESET,
            "active": False,
            "flags": {},
            "can_restore": False,
        }


@stonic_router.post("/preset")
async def stonic_preset_apply(request: Request, body: StonicPresetBody):
    """Turn Always-Active Stonic Mode on (all seven flags) or off (each restored)."""
    from ghost.core import stonic_preset  # noqa: PLC0415

    config = getattr(request.app.state, "config", None)
    result = await asyncio.to_thread(stonic_preset.apply, body.enabled, config=config)
    if body.enabled:
        try:
            from ghost.memory.v2 import bridge  # noqa: PLC0415

            await asyncio.to_thread(bridge.attach, request.app.state)
        except Exception:  # noqa: BLE001 — memory starts on next launch instead
            logger.debug("memory v2 did not start after the preset", exc_info=True)
    return {"ok": True, **result}


@optimize_router.get("/runs/{run_id}")
async def get_optimize_run(run_id: str, request: Request):
    """Get optimization run details."""
    try:
        from ghost.core.config import DEFAULT_CONFIG_DIR
        from ghost.learning.optimize.store import OptimizationStore

        db_path = DEFAULT_CONFIG_DIR / "optimize.db"
        if not db_path.exists():
            return {"run_id": run_id, "status": "not_found"}

        store = OptimizationStore(db_path)
        run = store.get_run(run_id)
        store.close()

        if run is None:
            return {"run_id": run_id, "status": "not_found"}

        return {
            "run_id": run.run_id,
            "status": run.status,
            "benchmark": run.benchmark,
            "trials": len(run.trials),
            "best_trial_id": (run.best_trial.trial_id if run.best_trial else None),
        }
    except Exception as exc:
        logger.warning("Failed to get optimization run %s: %s", run_id, exc)
        return {"run_id": run_id, "status": "not_found"}


@optimize_router.post("/runs")
async def start_optimize_run(req: OptimizeRunRequest, request: Request):
    """Start a new optimization run."""
    return {"status": "started", "run_id": "placeholder"}


def include_all_routes(app) -> None:
    """Include all extended API routers in a FastAPI app."""
    from ghost.server.approval_routes import (
        router as approval_router,  # noqa: PLC0415
    )

    app.include_router(approval_router)
    app.include_router(agents_router)
    app.include_router(memory_router)
    app.include_router(traces_router)
    app.include_router(telemetry_router)
    app.include_router(skills_router)
    app.include_router(sessions_router)
    app.include_router(budget_router)
    app.include_router(metrics_router)
    app.include_router(websocket_router)
    app.include_router(learning_router)
    app.include_router(speech_router)
    app.include_router(feedback_router)
    app.include_router(optimize_router)
    app.include_router(stonic_router)
    app.include_router(computer_router)
    app.include_router(realtime_router)
    app.include_router(facelock_router)
    app.include_router(facelock_assets_router)
    app.include_router(awareness_router)
    app.include_router(memory_v2_router)

    # Memory v2 (Stonic port, phase 6): start Memory Core when the flag is on.
    @app.on_event("startup")
    async def _memory_v2_start() -> None:
        try:
            from ghost.memory.v2 import bridge  # noqa: PLC0415

            await asyncio.to_thread(bridge.attach, app.state)
        except Exception:  # noqa: BLE001 — memory never stops the server
            logger.debug("memory v2 did not start", exc_info=True)

    # Agent Manager routes (if available)
    try:
        if hasattr(app.state, "agent_manager") and app.state.agent_manager:
            from ghost.server.agent_manager_routes import (  # noqa: PLC0415
                create_agent_manager_router,
            )

            (
                agents_r,
                templates_r,
                global_r,
                tools_r,
                sendblue_r,
            ) = create_agent_manager_router(app.state.agent_manager)
            app.include_router(agents_r)
            app.include_router(templates_r)
            app.include_router(global_r)
            app.include_router(tools_r)
            app.include_router(sendblue_r)
    except ImportError:
        pass

    # WebSocket bridge for real-time agent events. Must subscribe on the
    # same EventBus instance channels/agents actually publish to
    # (app.state.bus, set in server/app.py) — the get_event_bus() global
    # singleton is a *different* bus that nothing in `ghost serve` ever
    # publishes to, so events silently never reached this endpoint.
    try:
        from ghost.core.events import get_event_bus
        from ghost.server.ws_bridge import create_ws_router

        ws_router = create_ws_router(getattr(app.state, "bus", None) or get_event_bus())
        app.include_router(ws_router)
    except Exception:
        logger.debug("WebSocket bridge not available", exc_info=True)


__all__ = [
    "include_all_routes",
    "memory_v2_router",
    "agents_router",
    "memory_router",
    "traces_router",
    "telemetry_router",
    "skills_router",
    "sessions_router",
    "budget_router",
    "metrics_router",
    "websocket_router",
    "learning_router",
    "speech_router",
    "feedback_router",
    "optimize_router",
    "stonic_router",
    "computer_router",
    "realtime_router",
    "facelock_router",
    "facelock_assets_router",
    "awareness_router",
]
