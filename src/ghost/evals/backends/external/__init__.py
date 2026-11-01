"""External-framework subprocess backends (Hermes Agent, OpenClaw)."""

from ghost.evals.backends.external.hermes_agent import HermesBackend
from ghost.evals.backends.external.openclaw import OpenClawBackend

__all__ = ["HermesBackend", "OpenClawBackend"]
