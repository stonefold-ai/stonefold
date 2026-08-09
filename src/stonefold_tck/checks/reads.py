# SPDX-License-Identifier: Apache-2.0
"""P1–P4 — what a gate reads (v0.3; profile ``reads``).

A control that depends on content which ages works perfectly while the content is
current, which is why the failure is found after an incident rather than before
one. Four things a conforming gateway must do about it.

P2 is the one worth reading twice: a stale source and an unreachable one must be
**distinguishable**. Before this, both produced the same refusal with the same
code, so a cache holding an old copy looked exactly like the source saying no —
and in one case exactly like a product regression.
"""

from __future__ import annotations

from stonefold_tck.checks import PROFILE_READS, check, expect
from stonefold_tck.checks._util import setup, submit
from stonefold_tck.driver import CAP_CLOCK, CAP_READS, ConformanceDriver, Operation
from stonefold_tck.fixtures import POLICY_READS


def _dismiss() -> Operation:
    # `target` is how the kit names an existing row; tck.flagSet reads the
    # resolved target, so the source's state is the only variable in these checks.
    return Operation(resource="Result", action="dismiss",
                     data={"resultId": "R-1"}, target="R-1")


@check(
    "P1",
    "a fresh source lets the gate's own checks run",
    PROFILE_READS,
    requires=[CAP_READS, CAP_CLOCK],
)
def p1_fresh_source_passes(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_READS)
    driver.set_source_age("critical-analyte-list", 3)
    result = submit(driver, _dismiss())
    expect(
        result.decision == "allow",
        f"a source 3 days old against a 30-day requirement gave {result.decision!r} — "
        "declaring a dependency must not refuse work while the dependency is current",
    )


@check(
    "P2",
    "a stale source and an unreachable one are distinguishable",
    PROFILE_READS,
    requires=[CAP_READS, CAP_CLOCK],
)
def p2_stale_is_not_unavailable(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_READS)
    driver.set_source_age("critical-analyte-list", 55)
    stale = submit(driver, _dismiss())
    expect(
        stale.reason_code == "SOURCE_STALE",
        f"a source 55 days past a 30-day requirement is coded {stale.reason_code!r} "
        "instead of the normative 'SOURCE_STALE'",
    )
    expect(
        stale.decision == "hold",
        f"a stale source gave {stale.decision!r}; the content was READ and its date "
        "seen, so this is judgment-shaped and holds by default (§7.6 rule 1)",
    )

    setup(driver, policy=POLICY_READS)
    driver.set_source_outage("critical-analyte-list", True)
    gone = submit(driver, _dismiss())
    expect(
        gone.reason_code == "SOURCE_UNAVAILABLE",
        f"an unreadable source is coded {gone.reason_code!r} instead of the normative "
        "'SOURCE_UNAVAILABLE'",
    )
    expect(
        gone.reason_code != stale.reason_code,
        "a stale copy and an unreachable source produced the SAME code, so 'the world "
        "says no' and 'my copy of the world is old' cannot be told apart — which is "
        "the failure §? exists to remove",
    )


@check(
    "P3",
    "undated content is not treated as fresh",
    PROFILE_READS,
    requires=[CAP_READS, CAP_CLOCK],
)
def p3_undated_is_not_fresh(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_READS)
    driver.set_source_age("critical-analyte-list", None)  # reachable, but undated
    result = submit(driver, _dismiss())
    expect(
        result.reason_code == "SOURCE_UNDATED",
        f"undated content with a freshness requirement is coded "
        f"{result.reason_code!r} instead of 'SOURCE_UNDATED'",
    )
    expect(
        result.decision != "allow",
        "content whose age cannot be verified was treated as current. Unverifiable is "
        "not fresh — otherwise a source that stops reporting its date silently "
        "disables the requirement",
    )


@check(
    "P4",
    "an unavailable guard can queue for a human instead of denying forever",
    PROFILE_READS,
    requires=[CAP_READS, CAP_CLOCK],
)
def p4_unavailable_can_hold(driver: ConformanceDriver) -> None:
    """The case an estate that cannot report a state has to be able to express.

    Denying every legitimate attempt and mislabelling the action were the only two
    options before this; both are worse than asking a person.
    """
    setup(driver, policy=POLICY_READS)   # its gate declares onUnavailable: hold
    driver.set_source_outage("critical-analyte-list", True)
    result = submit(driver, _dismiss())
    expect(
        result.decision == "hold",
        f"a gate declaring onUnavailable: hold gave {result.decision!r} — the point of "
        "the declaration is that a permanently unavailable guard becomes a question "
        "rather than a permanent refusal",
    )
    expect(
        bool(result.ticket),
        "the held action carries no ticket, so nobody can release it",
    )
