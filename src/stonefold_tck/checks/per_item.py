# SPDX-License-Identifier: Apache-2.0
"""O1–O4 — per-item verdicts (v0.6.1 CS-043; profile ``per-item``).

The failure under test: one action carries twenty items, thirteen of which need a
human. A gateway that can only allow or refuse the call takes the seven
legitimate items down with the thirteen, so refusing is an outage rather than a
control.

O2 is the one that matters most. A driver that reports ``allow`` for a partial
application fails it, because an actor reading the decision alone would conclude
the whole call went through — the same mistake CS-029 exists to prevent, one
level up.
"""

from __future__ import annotations

from typing import Any

from stonefold_tck.checks import PROFILE_PER_ITEM, check, expect
from stonefold_tck.checks._util import expect_decision, setup, submit
from stonefold_tck.driver import CAP_AUDIT, CAP_PER_ITEM, ConformanceDriver, Operation
from stonefold_tck.fixtures import BLOCKED_ITEMS, POLICY_PER_ITEM

CLEAN = ("Q-1", "Q-2", "Q-3")


def _close(*items: str) -> Operation:
    return Operation(resource="Queue", action="closeMany", data={"itemIds": list(items)})


def _verdicts(result: Any) -> dict[str, str]:
    return {str(v.get("item")): str(v.get("decision")) for v in result.items}


@check(
    "O1",
    "a mixed call applies the clean items and refuses the rest, naming each",
    PROFILE_PER_ITEM,
    requires=[CAP_PER_ITEM],
)
def o1_mixed_call_partitions(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_PER_ITEM)
    result = submit(driver, _close(CLEAN[0], BLOCKED_ITEMS[0], CLEAN[1]))

    expect(
        len(result.items) == 3,
        f"the call carried 3 items and came back with {len(result.items)} verdicts — "
        "every item gets its own (CS-043)",
    )
    verdicts = _verdicts(result)
    expect(
        verdicts.get(CLEAN[0]) == "allow" and verdicts.get(CLEAN[1]) == "allow",
        f"the clean items were not applied: {verdicts}. Refusing them along with the "
        "blocked one is the outage this exists to remove",
    )
    expect(
        verdicts.get(BLOCKED_ITEMS[0]) == "deny",
        f"the blocked item was not refused: {verdicts}",
    )
    expect(
        sorted(result.applied) == sorted([CLEAN[0], CLEAN[1]]),
        f"applied is {list(result.applied)}; it must name exactly the items that "
        "took effect",
    )
    blocked = next(v for v in result.items if v.get("item") == BLOCKED_ITEMS[0])
    expect(
        bool(blocked.get("reasonCode")),
        "a refused item carries no reason code — per-item codes are the whole "
        "point: that item is retryable and this one is not",
    )


@check(
    "O2",
    "a partial application is never reported as allow",
    PROFILE_PER_ITEM,
    requires=[CAP_PER_ITEM],
)
def o2_partial_is_not_allow(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_PER_ITEM)
    result = submit(driver, _close(CLEAN[0], BLOCKED_ITEMS[0]))
    expect(
        result.decision != "allow",
        "a call where one item was refused reported 'allow'. An actor reading the "
        "decision alone would conclude the whole call went through, which is the "
        "CS-029 failure one level up (CS-043)",
    )
    expect(
        result.decision == "deny",
        f"the envelope is {result.decision!r}; with a denied item and no halt the "
        "most final per-item outcome is 'deny'",
    )
    expect(
        bool(result.applied),
        "the items that DID take effect are not reported, so the actor cannot tell "
        "a partial application from a total refusal",
    )


@check(
    "O3",
    "every item clean is a plain allow; every item refused applies nothing",
    PROFILE_PER_ITEM,
    requires=[CAP_PER_ITEM],
)
def o3_the_two_clean_ends(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_PER_ITEM)
    all_clean = expect_decision(submit(driver, _close(*CLEAN)), "allow", "every item clean")
    expect(
        sorted(all_clean.applied) == sorted(CLEAN),
        f"applied is {list(all_clean.applied)}; all three items were clean",
    )

    setup(driver, policy=POLICY_PER_ITEM)
    none_clean = submit(driver, _close(*BLOCKED_ITEMS))
    expect(
        none_clean.decision == "deny" and not none_clean.applied,
        f"a call whose every item is refused reported {none_clean.decision!r} with "
        f"applied={list(none_clean.applied)} — nothing may take effect",
    )


@check(
    "O4",
    "each refused item is audited on its own",
    PROFILE_PER_ITEM,
    requires=[CAP_PER_ITEM, CAP_AUDIT],
)
def o4_refusals_are_in_the_record(driver: ConformanceDriver) -> None:
    """A refused item's record is the only place that refusal exists — the estate
    shows a closed queue either way."""
    setup(driver, policy=POLICY_PER_ITEM)
    submit(driver, _close(CLEAN[0], BLOCKED_ITEMS[0], BLOCKED_ITEMS[1]))

    refusals = [
        a for a in driver.audit()
        if a.action == "closeMany" and a.decision == "deny"
    ]
    expect(
        len(refusals) == 2,
        f"{len(refusals)} audit records for 2 refused items — each item is its own "
        "decision and RFC §11 wants a record per decision",
    )
    applied = [
        a for a in driver.audit()
        if a.action == "closeMany" and a.decision == "allow"
    ]
    expect(
        len(applied) == 1,
        f"{len(applied)} audit records for the applied subset — it went to the "
        "managed system as ONE call, so recording it as several would misdescribe "
        "what happened",
    )
