# SPDX-License-Identifier: Apache-2.0
"""N1–N4 — closure accountability (v0.3; profile ``closure``).

The failure under test: a gate refuses the work, and the actor then closes the
item as done. The refusal becomes invisible — the queue is empty and the managed
system's completeness check passes.

What a conforming gateway must do, in the order §7.6 states it: hold a closure
that carries no declared disposition; hold one that claims completion while the
gateway holds a refusal for the same actor in the same run, naming what it
refused; **let an honest disposition through** even after a refusal, because a
control with no honest exit is an outage; and never satisfy a completion claim it
cannot check.

N3 is the one that separates a control from an outage; N4 is the one that checks
the deliverable, because a refusal nobody can see is worth nothing.
"""

from __future__ import annotations

from stonefold_tck.checks import PROFILE_CLOSURE, check, expect
from stonefold_tck.checks._util import expect_decision, pay, setup, submit
from stonefold_tck.driver import CAP_AUDIT, CAP_CLOSURE, ConformanceDriver, Operation
from stonefold_tck.fixtures import POLICY_CLOSURE


def _close(disposition: str | None = None, task: str = "T-1") -> Operation:
    data: dict[str, object] = {"taskId": task}
    if disposition is not None:
        data["disposition"] = disposition
    return Operation(resource="Task", action="close", data=data)


def _refused_earlier(driver: ConformanceDriver) -> None:
    """Put a refusal in this run: a payment over the fixture's value limit."""
    expect_decision(submit(driver, pay(20000)), "deny", "over the value limit")


@check(
    "N1",
    "a closure with no declared disposition holds, and nothing is closed",
    PROFILE_CLOSURE,
    requires=[CAP_CLOSURE],
)
def n1_missing_disposition_holds(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_CLOSURE)
    result = expect_decision(submit(driver, _close()), "hold", "no disposition supplied")
    expect(
        result.reason_code == "DISPOSITION_REQUIRED",
        f"the hold is coded {result.reason_code!r} instead of the normative "
        "'DISPOSITION_REQUIRED' (§? rule 2)",
    )
    expect(
        result.retry_class == "retryable",
        f"a missing disposition is classed {result.retry_class!r}; it is the actor's "
        "to fix by resubmitting, so the normative class is 'retryable'",
    )
    # and the same for a value outside the declared vocabulary
    invented = expect_decision(
        submit(driver, _close("handled-somehow")), "hold", "undeclared disposition"
    )
    expect(
        invented.reason_code == "DISPOSITION_REQUIRED",
        "a disposition outside the declared vocabulary must hold like a missing one — "
        "otherwise the vocabulary is advisory",
    )


@check(
    "N2",
    "claiming completion after a refusal in the same run holds, naming the refusal",
    PROFILE_CLOSURE,
    requires=[CAP_CLOSURE],
)
def n2_completion_after_refusal_holds(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_CLOSURE)
    _refused_earlier(driver)
    result = expect_decision(
        submit(driver, _close("resolved")), "hold", "completion claimed after a refusal"
    )
    expect(
        result.reason_code == "CLOSED_WITHOUT_THE_WORK",
        f"the hold is coded {result.reason_code!r} instead of the normative "
        "'CLOSED_WITHOUT_THE_WORK' (§? rule 3)",
    )
    expect(
        result.retry_class == "escalate",
        f"a false completion claim is classed {result.retry_class!r}; resubmitting "
        "cannot fix it and a human must decide, so the normative class is 'escalate'",
    )


@check(
    "N3",
    "an honest disposition passes, refusal or not — the control has an exit",
    PROFILE_CLOSURE,
    requires=[CAP_CLOSURE],
)
def n3_honest_disposition_passes(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_CLOSURE)
    _refused_earlier(driver)
    expect_decision(
        submit(driver, _close("escalated")),
        "allow",
        "an actor that was refused must still be able to close the item honestly — "
        "if every disposition held, the only way to satisfy the gate would be to do "
        "nothing, and a queue full of untouched rows is indistinguishable from a crash",
    )
    # a clean run may of course claim completion
    setup(driver, policy=POLICY_CLOSURE)
    expect_decision(
        submit(driver, _close("resolved")), "allow", "completion claimed with no refusals"
    )


@check(
    "N4",
    "the refused closure is in the record: the queue looks the same, the audit does not",
    PROFILE_CLOSURE,
    requires=[CAP_CLOSURE, CAP_AUDIT],
)
def n4_the_attempt_is_in_the_record(driver: ConformanceDriver) -> None:
    """The whole value of this control is legibility, so it has to be checkable.

    A held closure that leaves no trace would be worse than no control: the item
    stays open, and nobody can tell that an actor tried to close it as done. The
    record is the deliverable.
    """
    setup(driver, policy=POLICY_CLOSURE)
    _refused_earlier(driver)
    expect_decision(submit(driver, _close("resolved")), "hold", "false completion claim")

    closures = [a for a in driver.audit() if a.action == "close"]
    expect(
        bool(closures),
        "the refused closure left no audit record — the attempt to close the item as "
        "done is exactly what nothing else in the estate can see",
    )
    last = closures[-1]
    expect(
        last.decision == "hold",
        f"the audited closure is {last.decision!r}, not the hold that was returned",
    )
    expect(
        last.reason_code == "CLOSED_WITHOUT_THE_WORK",
        f"the audit record carries reason code {last.reason_code!r}; a human reading "
        "the log must see WHY the closure paused without reading check code (§7.6 rule 2)",
    )
