# SPDX-License-Identifier: Apache-2.0
"""Decision freshness (v0.4 CS-017, changeset docs/RFC-changeset-v0.3-to-v0.4.md).

A staged effect's decision is only valid for a bounded time: every staged row
carries an ``expires_at`` stamped at staging from deployment configuration (NOT
policy syntax — the language stays frozen), and the dispatch claim re-validates
the **volatile** gates whose facts move independently of the agent. A lapsed TTL
settles ``CANCELLED``/``stale-decision``; a dispatch-time gate failure settles
``CANCELLED``/``stale-guard:<gate>``. Both are audited, never partially
dispatched.

This module is pure (no I/O, no clock — the clock is injected where these values
are consumed) — part of the trust kernel.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from stonefold_core.enums import Kind, Reversibility
from stonefold_core.models import GateResult, ResolvedAction
from stonefold_core.outbox import PendingAction

# The gate classes re-validated at dispatch (CS-017 rule 2): their facts change
# with the world, not with the frozen staged payload. Everything else is
# non-volatile BY DEFINITION and MUST NOT be re-run: valueLimit/contentCheck
# (the payload is frozen), rate/quota/quantityCap/spendLimit (consumed at
# decision time — re-running double-counts), requireApproval/dualAuthorization
# (the grant IS the release; its freshness is bounded by the TTL).
# ``requireMatch`` (v0.6 CS-032 rule 3) is volatile as a full re-query for now;
# CS-035 (reservation lifecycle) replaces the dispatch-time re-run with a
# reservation-liveness check once obligations are reserved at staging.
VOLATILE_GATES: frozenset[str] = frozenset(
    {"allowlist", "denylist", "window", "precondition", "emissionControl",
     "requireMatch"}
)

# Settle reason for a row claimed after its TTL (CS-017 rule 1).
STALE_DECISION = "stale-decision"


def stale_guard_reason(gate: str) -> str:
    """Settle reason for a dispatch-time volatile-gate failure (CS-017 rule 2)."""
    return f"stale-guard:{gate}"


# Re-runs the volatile gates for one claimed row at dispatch time, returning the
# first non-PASS ``GateResult`` (⇒ cancel as ``stale-guard:<gate>``) or ``None``
# when the decision is still fresh. The ``datetime`` is the dispatch-time clock.
DispatchRevalidator = Callable[[PendingAction, datetime], GateResult | None]


@dataclass(frozen=True)
class FreshnessConfig:
    """Decision-TTL deployment configuration (CS-017 rule 1).

    Both TTLs MUST be finite and positive; the irreversible TTL SHOULD be short
    (minutes–hours, not days) — an irreversible effect's decision goes stale
    fastest and cannot be compensated once dispatched.
    """

    default_ttl: timedelta = timedelta(hours=24)
    irreversible_ttl: timedelta = timedelta(minutes=30)

    def __post_init__(self) -> None:
        if self.default_ttl <= timedelta(0) or self.irreversible_ttl <= timedelta(0):
            raise ValueError("freshness TTLs must be finite and positive (CS-017)")

    def ttl_for(self, resolved: ResolvedAction) -> timedelta:
        """The TTL class an action falls in: irreversible effects get the short
        TTL, everything else the default."""
        if (
            resolved.kind is Kind.EFFECT
            and resolved.attrs.reversibility is Reversibility.IRREVERSIBLE
        ):
            return self.irreversible_ttl
        return self.default_ttl

    def expiry_for(self, resolved: ResolvedAction, now: datetime) -> datetime:
        """The ``expires_at`` to stamp on a row staged at ``now``."""
        return now + self.ttl_for(resolved)
