"""Feedback subsystem: LLM-as-judge scoring and signal aggregation."""

from ghost.learning.optimize.feedback.collector import FeedbackCollector
from ghost.learning.optimize.feedback.judge import TraceJudge

__all__ = ["TraceJudge", "FeedbackCollector"]
