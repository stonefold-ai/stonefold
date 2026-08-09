# SPDX-License-Identifier: Apache-2.0
"""The standard closure check (spec §7.6, v0.3 §?).

**The failure this exists for.** A gate refuses the work — the payment is held,
the mandate change denied — and the actor then closes the item as done. The
refusal becomes invisible: the queue is empty, the managed system's completeness
check passes, and the next human sees nothing to look at. The mirror failure is
no better: an actor that correctly declines to close anything leaves a row nobody
touched, which is byte-identical to an actor that crashed.

One control covers both, because a declared disposition vocabulary gives the
actor a way to say *I looked at this and it is not mine to close*, and gives the
gateway a way to refuse the claim that contradicts its own record.

**What it guarantees, stated narrowly:** *nothing is closed as done while this
gateway holds a refusal for the same actor in the same run.* That is a claim
about the record, not about completeness. This code cannot know whether work was
done; it knows what passed through the gateway. An item that never arrived is
invisible here and stays the managed system's responsibility.

**Scope, and it is a real limitation rather than a rough edge.** The check is
keyed on the *run*, not the item, because an intent does not cite the item it is
acting for — ``Supplier.updateBankAccount`` names a supplier, not the request
that asked for it. So it is exact where a run handles one item and over-broad
where a run handles forty: a truthful closure can be held because something else
in the same run was refused. The direction is deliberate — a hold on an honest
closure, never a silent false one — and an honest disposition still passes.
"""

from __future__ import annotations

from typing import Any

from stonefold_core.enums import Decision
from stonefold_core.registry import DISPOSITION_IS_DECLARED
from stonefold_gates.base import (
    CheckResult,
    GateContext,
    PreconditionCheck,
    check_hold,
    check_pass,
)

# The reserved name is declared in the registry module (the linter needs it and
# core never imports this layer); re-exported here, where it is implemented.
__all__ = [
    "DISPOSITION_IS_DECLARED",
    "AuditDecisionHistory",
    "CLOSED_WITHOUT_THE_WORK",
    "DISPOSITION_REQUIRED",
    "disposition_is_declared",
    "standard_checks",
]

#: Reason codes, both hold-shaped. The registry declaration carries
#: their retry classes: DISPOSITION_REQUIRED is retryable (resubmit with a
#: disposition), CLOSED_WITHOUT_THE_WORK escalates (a human decides).
DISPOSITION_REQUIRED = "DISPOSITION_REQUIRED"
CLOSED_WITHOUT_THE_WORK = "CLOSED_WITHOUT_THE_WORK"

#: A refusal, for this check's purposes. A HOLD counts: the action has not
#: happened, which is exactly what a completion claim would be denying.
_REFUSALS = (Decision.DENY, Decision.HALT, Decision.HOLD)


class AuditDecisionHistory:
    """``DecisionHistory`` over the gateway's own audit sink.

    Reads only records the gateway wrote, and only those matching this actor and
    run. Any sink exposing ``all_records()`` works; anything else is treated as
    no history at all, which fails the check closed rather than passing it.
    """

    def __init__(self, sink: Any) -> None:
        self._sink = sink

    def refusals_in_run(self, *, actor_id: str, correlation_id: str) -> tuple[str, ...]:
        reader = getattr(self._sink, "all_records", None)
        if reader is None:
            raise RuntimeError("audit sink cannot be queried for this run's decisions")
        return tuple(
            f"{record.resource}.{record.action}" if record.action else record.resource
            for record in reader()
            if record.actor == actor_id
            and record.correlationId == correlation_id
            and record.decision in _REFUSALS
        )


def disposition_is_declared(gctx: GateContext) -> CheckResult:
    """The standard check. Rules in the order §7.6 states them.

    Rule 1 (no declaration) raises rather than returning FAIL: a check named on
    an action that cannot satisfy it is a configuration error, and §10's
    fail-closed path is where configuration errors belong. The §13.20 lint rule
    catches it at load, so reaching this at runtime means the policy was loaded
    without linting.
    """
    rdef = gctx.registry.file.resources.get(gctx.resolved.resource)
    adef = rdef.actions.get(gctx.resolved.action or "") if rdef is not None else None
    closure = adef.closure if adef is not None else None
    vocabulary = adef.disposition_vocabulary() if adef is not None else ()
    if closure is None or not vocabulary:
        raise RuntimeError(
            f"{DISPOSITION_IS_DECLARED} is named on "
            f"{gctx.resolved.resource}.{gctx.resolved.action} which declares no "
            "closure disposition vocabulary (registry docs/06 §5c)"
        )

    supplied = gctx.resolved.data.get(closure.dispositionField)
    text = "" if supplied is None else str(supplied).strip()
    if text not in vocabulary:
        # Rule 2. Nothing is closed; the actor may resubmit with a disposition.
        return check_hold(
            DISPOSITION_REQUIRED,
            {"field": closure.dispositionField, "declared": list(vocabulary)},
        )

    if text not in closure.claimsCompletion:
        # Rule 4, the honest exit: a non-completion disposition never consults
        # history. Keeping this path cheap is what stops the control becoming an
        # outage — the actor always has somewhere to put a truthful answer.
        return check_pass()

    # Rule 3. The claim is "done"; check it against what this gateway refused.
    if gctx.history is None:
        # No history wired: the claim cannot be checked, and an unverifiable
        # completion claim is exactly the thing that must not pass. §10.
        raise RuntimeError(
            "no decision history: a completion claim cannot be checked against "
            "this run's refusals"
        )
    # The run key is the audit's own: ``correlation_id or session.id``
    # (stonefold_core.audit.build_record). Computing it any other way here would
    # silently fail to match the records — and failing to match reads as "nothing
    # was refused", which is the one wrong answer this check must never give.
    run = gctx.session.correlation_id or gctx.session.id
    refused = gctx.history.refusals_in_run(actor_id=gctx.actor.id, correlation_id=run)
    if refused:
        return check_hold(
            CLOSED_WITHOUT_THE_WORK,
            {"disposition": text, "refusedInRun": list(refused)},
        )
    return check_pass()


def standard_checks() -> dict[str, PreconditionCheck]:
    """The reserved-name checks a gateway supplies (currently one).

    Merged over the integrator's map, not under it: the name is reserved, so a
    local implementation of it would silently change a documented guarantee.
    """
    return {DISPOSITION_IS_DECLARED: disposition_is_declared}
