# SPDX-License-Identifier: Apache-2.0
"""What a gate reads, and what to do when it cannot be trusted (CS-044).

**The failure this exists for.** A control reads content that ages — a
critical-analyte list, a sanctions list, a tariff table, the fraud rules a bank
rewrites quarterly. While the content is current the control works and nobody
looks at it. The dependency lives in a comment, so:

* a gate whose author *forgot* the freshness check passes confidently on last
  year's medicine, and **no analysis over the policy can find it**, because the
  policy records the check's name and never what the check reads;
* a stale copy of a fact and an unreachable source produce the same refusal with
  the same reason code, so *the world says no* and *my copy of the world is old*
  are indistinguishable from outside;
* an estate that cannot report a state at all has no way to say **governed, but
  the guard is currently unavailable** — the honest declaration denies every
  legitimate attempt, so integrators mislabel the action instead.

Naming the dependency in the policy fixes all three, and the fix that matters
most is the boring one: the dependency becomes **data**, so
``gates_reading(policy, registry, source)`` can answer *this rule set is 25 days
overdue — which gates are affected, and which gates in the same class read
nothing at all?* without running anything. A reviewer, an auditor and a
regulatory assessment all read a policy rather than run it, and the gate that
kills someone is the one that reads nothing while its neighbours read the list.

**Two defaults, and they differ on purpose.** §7.6 rule 1: outages fail, only
readable ambiguity holds — otherwise every blip becomes a human interruption and
fail-closed degrades into a queue that gets rubber-stamped. A **stale** source is
readable: we fetched it and read its date, and "three weeks past review, decide"
is judgment-shaped, so it holds. An **unreadable** source is an outage, so it
denies. A deployment whose guard is permanently unavailable opts into
``onUnavailable: hold`` deliberately, and the linter makes it choose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from stonefold_core.enums import Outcome
from stonefold_gates.base import CheckResult, GateContext, check_hold, window_seconds

#: Reason codes, all three reaching the audit (CS-029).
SOURCE_STALE = "SOURCE_STALE"
SOURCE_UNDATED = "SOURCE_UNDATED"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"

#: What a gate may do about a source it cannot trust.
_DISPOSITIONS = ("hold", "deny", "allow")


class SourceAdapter(Protocol):
    """How old the thing a control reads is.

    One method, because that is the only question the gateway asks. A source that
    carries no date of its own returns ``None``, which is not the same as being
    unreachable: undated content with a declared freshness requirement is
    *unverifiable*, and unverifiable is treated as stale rather than fresh.
    """

    def as_of(self) -> datetime | None:
        """When the source's content was last known good, or ``None`` if undated."""
        ...


@dataclass(frozen=True)
class ReadDecl:
    """One ``reads:`` entry, after parsing."""

    source: str
    freshness_s: float | None = None
    on_stale: str = "hold"


def parse_reads(cfg: Any) -> tuple[ReadDecl, ...]:
    """Parse a gate's ``reads:`` list. Unknown shapes yield nothing to read,
    which the linter catches rather than the runtime guessing."""
    if not isinstance(cfg, list):
        return ()
    out: list[ReadDecl] = []
    for entry in cfg:
        if isinstance(entry, str):
            out.append(ReadDecl(source=entry))
        elif isinstance(entry, dict) and entry.get("source"):
            freshness = entry.get("freshness")
            out.append(ReadDecl(
                source=str(entry["source"]),
                freshness_s=window_seconds(freshness) if freshness else None,
                on_stale=str(entry.get("onStale", "hold")),
            ))
    return tuple(out)


def _verdict(disposition: str, code: str, evidence: dict[str, Any]) -> CheckResult | None:
    """``None`` means "carry on and run the checks" — the ``allow`` disposition."""
    if disposition == "allow":
        return None
    if disposition == "deny":
        return CheckResult(Outcome.FAIL, code=code, evidence=evidence)
    return check_hold(code, evidence)


def check_reads(
    reads: tuple[ReadDecl, ...], on_unavailable: str, gctx: GateContext
) -> CheckResult | None:
    """Verify every declared source before the gate's own checks run.

    Returns ``None`` when every source is trustworthy (or the policy declared it
    does not care), and otherwise the verdict the declaration asked for. Ordered
    so the first untrustworthy source decides: a gate reading two rule sets, one
    of them overdue, is not in a position to run either check.
    """
    if not reads:
        return None
    adapters: dict[str, SourceAdapter] = dict(getattr(gctx, "sources", {}) or {})
    now = gctx.env.now

    for decl in reads:
        adapter = adapters.get(decl.source)
        if adapter is None:
            # A declared source with no registered adapter is unreadable, not
            # fresh. Same rule ``requireMatch`` follows for a missing obligation
            # adapter: a dependency the deployment has not built is a dependency
            # failure, never a silent pass.
            verdict = _verdict(on_unavailable, SOURCE_UNAVAILABLE,
                               {"source": decl.source, "reason": "no adapter registered"})
            if verdict is not None:
                return verdict
            continue
        try:
            as_of = adapter.as_of()
        except Exception as exc:  # an outage, per §7.6 rule 1
            verdict = _verdict(on_unavailable, SOURCE_UNAVAILABLE,
                               {"source": decl.source, "reason": str(exc)})
            if verdict is not None:
                return verdict
            continue

        if decl.freshness_s is None:
            continue  # the gate named the source without demanding currency
        if as_of is None:
            # Undated content with a freshness requirement cannot be verified,
            # and unverifiable is not fresh.
            verdict = _verdict(decl.on_stale, SOURCE_UNDATED, {"source": decl.source})
            if verdict is not None:
                return verdict
            continue
        if now is None:
            # No clock, so age is unknowable — invariant 1: we never invent a
            # time. Treated as an outage rather than as fresh.
            verdict = _verdict(on_unavailable, SOURCE_UNAVAILABLE,
                               {"source": decl.source, "reason": "no clock supplied"})
            if verdict is not None:
                return verdict
            continue
        age = now - as_of
        if age > timedelta(seconds=decl.freshness_s):
            verdict = _verdict(decl.on_stale, SOURCE_STALE, {
                "source": decl.source,
                "asOf": as_of.isoformat(),
                "ageDays": round(age.total_seconds() / 86400, 1),
                "maxAgeDays": round(decl.freshness_s / 86400, 1),
            })
            if verdict is not None:
                return verdict
    return None
