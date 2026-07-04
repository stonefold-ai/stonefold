"""The TCK check framework and the assembled check catalogue.

A check is a small function over the driver, tagged with a scenario id (from
``tests/acceptance-scenarios.md``), a conformance profile, and the driver
capabilities it needs. Checks assert with ``expect`` (raising
``ConformanceFailure``); the runner turns missing capabilities into SKIPs —
never silent passes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from stonefold_tck.driver import ConformanceDriver

# conformance profiles (docs/12 §4)
PROFILE_CORE = "core"  # authorization + deterministic gates
PROFILE_LINT = "lint"  # load-time validation refuses bad policies
PROFILE_SCOPE = "scope"  # scope injection below the model
PROFILE_STAGING = "staging"  # outbox, approvals, dual-auth
PROFILE_KILL = "kill"  # kill-switch semantics (serialized form)
PROFILE_AUDIT = "audit"  # decision log completeness & consistency
PROFILE_FRESHNESS = "freshness"  # v0.4 CS-017/018: decision TTL, volatile re-validation, scope no-race
PROFILE_BATCH = "batch"  # v0.5 CS-023: atomic batch decision semantics
PROFILE_DIGEST = "digest"  # v0.5 CS-020: connector digest pinning (load + dispatch)

ALL_PROFILES = (
    PROFILE_CORE,
    PROFILE_LINT,
    PROFILE_SCOPE,
    PROFILE_STAGING,
    PROFILE_KILL,
    PROFILE_AUDIT,
    PROFILE_FRESHNESS,
    PROFILE_BATCH,
    PROFILE_DIGEST,
)


class ConformanceFailure(AssertionError):
    """A check's expectation did not hold for the implementation under test."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise ConformanceFailure(message)


@dataclass(frozen=True)
class Check:
    id: str  # scenario id, e.g. "B2"
    title: str
    profile: str
    requires: frozenset[str]  # driver capabilities this check needs
    fn: Callable[[ConformanceDriver], None]


_CHECKS: list[Check] = []


def check(
    id: str, title: str, profile: str, requires: Iterable[str] = ()
) -> Callable[[Callable[[ConformanceDriver], None]], Callable[[ConformanceDriver], None]]:
    """Register a check function in the catalogue."""

    def deco(fn: Callable[[ConformanceDriver], None]) -> Callable[[ConformanceDriver], None]:
        _CHECKS.append(Check(id=id, title=title, profile=profile, requires=frozenset(requires), fn=fn))
        return fn

    return deco


def all_checks() -> tuple[Check, ...]:
    # import for side effects: each module registers its checks
    from stonefold_tck.checks import (  # noqa: F401
        audit,
        batch,
        core,
        digest,
        freshness,
        kill,
        lint,
        scope,
        staging,
    )

    return tuple(_CHECKS)
