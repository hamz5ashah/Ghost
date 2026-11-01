"""Structural protocols for substituting fakes in place of GhostSystem."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional, Protocol

if TYPE_CHECKING:
    from ghost.core.config import GhostConfig
    from ghost.core.events import EventBus
    from ghost.engine._stubs import InferenceEngine
    from ghost.security.capabilities import CapabilityPolicy
    from ghost.sessions.session import SessionStore
    from ghost.tools._stubs import BaseTool
    from ghost.tools.storage._stubs import MemoryBackend
    from ghost.traces.collector import TraceCollector
    from ghost.traces.store import TraceStore


class OrchestratorDeps(Protocol):
    """Minimum surface of GhostSystem that QueryOrchestrator depends on.

    Tests can satisfy this with a lightweight class — no need to construct
    the full GhostSystem dataclass or materialize every subsystem.
    """

    config: GhostConfig
    bus: EventBus
    engine: InferenceEngine
    engine_key: str
    model: str
    agent_name: str
    tools: List[BaseTool]
    memory_backend: Optional[MemoryBackend]
    capability_policy: Optional[CapabilityPolicy]
    rate_limiter: Optional[Any]
    session_store: Optional[SessionStore]
    trace_store: Optional[TraceStore]
    trace_collector: Optional[TraceCollector]  # written by _run_agent

    # Optional attribute (getattr with default) — declared for type clarity.
    _skill_few_shot_examples: Any
