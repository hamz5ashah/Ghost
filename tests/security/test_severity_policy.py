from __future__ import annotations


def test_severity_policy_block():
    from ghost.security.severity_policy import SeverityPolicy
    from ghost.security.types import ThreatLevel

    policy = SeverityPolicy()
    assert policy.action_for(ThreatLevel.CRITICAL) == "block"


def test_severity_policy_warn():
    from ghost.security.severity_policy import SeverityPolicy
    from ghost.security.types import ThreatLevel

    policy = SeverityPolicy()
    assert policy.action_for(ThreatLevel.HIGH) == "warn"


def test_severity_policy_sanitize():
    from ghost.security.severity_policy import SeverityPolicy
    from ghost.security.types import ThreatLevel

    policy = SeverityPolicy()
    assert policy.action_for(ThreatLevel.MEDIUM) == "sanitize"


def test_severity_policy_log():
    from ghost.security.severity_policy import SeverityPolicy
    from ghost.security.types import ThreatLevel

    policy = SeverityPolicy()
    assert policy.action_for(ThreatLevel.LOW) == "log"
