"""Operators — persistent, scheduled autonomous agents."""

from ghost.operators.loader import load_operator
from ghost.operators.manager import OperatorManager
from ghost.operators.types import OperatorManifest

__all__ = ["OperatorManifest", "OperatorManager", "load_operator"]
