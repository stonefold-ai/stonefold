# SPDX-License-Identifier: Apache-2.0
"""BATCH profile — atomic batch decision semantics (v0.5 CS-023; RFC §12, SIF §5).

Failure messages state the violation observed, not the expectation.
"""

from __future__ import annotations

from stonefold_tck.checks import PROFILE_BATCH, check, expect
from stonefold_tck.checks._util import ALICE, SESSION, effects_of, pay, setup, submit
from stonefold_tck.driver import (
    CAP_APPROVALS,
    CAP_AUDIT,
    CAP_BATCH,
    CAP_KILL,
    CAP_STAGING,
    BatchSubmitResult,
    ConformanceDriver,
    Operation,
)

WIDGET = Operation(
    resource="Widget", action="create",
    data={"id": "WB1", "owner_id": "alice", "name": "batch widget"},
)


def _batch(driver: ConformanceDriver, *ops: Operation) -> BatchSubmitResult:
    return driver.submit_batch(ALICE, SESSION, list(ops))


def _widget_count(driver: ConformanceDriver) -> int:
    rows = submit(driver, Operation(resource="Widget", action="read")).rows
    return len(rows or ())


@check("H1", "any DENY refuses the whole batch before anything commits or stages",
       PROFILE_BATCH, requires=[CAP_BATCH, CAP_STAGING, CAP_AUDIT])
def h1_deny_refuses_batch(driver: ConformanceDriver) -> None:
    setup(driver)
    before = _widget_count(driver)
    r = _batch(driver, WIDGET, pay(500, country="XX"))  # denylisted country
    expect(r.decision == "deny", "a denied operation did not refuse the whole batch")
    expect(r.failing_index == 1,
           f"the refusal does not name the failing operation (SIF §6): "
           f"expected index 1, got {r.failing_index!r}")
    expect(len(r.results) == 2,
           f"the batch reported {len(r.results)} results for 2 operations")
    expect(_widget_count(driver) == before,
           "the record op committed from a refused batch (must refuse before anything applies)")
    driver.dispatch_once()
    expect(effects_of(driver, "pay") == 0,
           "an effect staged or dispatched from a refused batch")
    # RFC §12 (CS-023): every operation in the batch gets its own audit record —
    # the failing op with its own deny, the rest with outcome ``batch-refused``.
    entries = list(driver.audit())
    expect(
        any(e.decision == "deny" and e.action == "pay" for e in entries),
        "the failing operation left no deny audit record of its own (CS-023)",
    )
    expect(
        any(e.outcome == "batch-refused" for e in entries),
        "the non-failing operation left no 'batch-refused' audit record (CS-023)",
    )


@check("H2", "a HALT refuses the whole batch the same way", PROFILE_BATCH,
       requires=[CAP_BATCH, CAP_STAGING, CAP_KILL])
def h2_halt_refuses_batch(driver: ConformanceDriver) -> None:
    setup(driver)
    before = _widget_count(driver)
    kill_id = driver.kill(scope="action_class", resource="Widget", action="create")
    r = _batch(driver, WIDGET, pay(500))
    driver.lift(kill_id)
    expect(r.decision == "halt", "a halted operation did not refuse the whole batch")
    expect(r.failing_index == 0,
           f"the refusal does not name the halted operation: "
           f"expected index 0, got {r.failing_index!r}")
    expect(_widget_count(driver) == before, "the refused batch committed a record op")
    driver.dispatch_once()
    expect(effects_of(driver, "pay") == 0,
           "an effect staged or dispatched from the refused batch")


@check("H3", "a HOLD does not refuse the batch: record commits, effect stages",
       PROFILE_BATCH, requires=[CAP_BATCH, CAP_STAGING, CAP_APPROVALS])
def h3_hold_commits_batch(driver: ConformanceDriver) -> None:
    setup(driver)
    before = _widget_count(driver)
    r = _batch(driver, WIDGET, pay(2000))  # above the approval threshold
    expect(r.decision == "hold", "a held operation refused the batch (a HOLD must not)")
    expect(r.failing_index is None,
           f"the committed batch names a failing operation ({r.failing_index!r})")
    expect(_widget_count(driver) == before + 1,
           "the record op did not commit atomically with the staging (§4.4)")
    ticket = r.results[1].ticket
    expect(ticket is not None, "the held effect carries no staging ticket")
    driver.dispatch_once()
    expect(effects_of(driver, "pay") == 0, "the held effect dispatched before approval")
    expect(driver.approve(ticket or "", "carol"), "the approval was refused")
    driver.dispatch_once()
    expect(effects_of(driver, "pay") == 1, "the approved effect did not dispatch")


@check("H4", "a later rejection does not roll committed batch ops back",
       PROFILE_BATCH, requires=[CAP_BATCH, CAP_STAGING, CAP_APPROVALS])
def h4_rejection_keeps_committed_ops(driver: ConformanceDriver) -> None:
    setup(driver)
    before = _widget_count(driver)
    r = _batch(driver, WIDGET, pay(2000))
    ticket = r.results[1].ticket
    expect(ticket is not None, "the held effect carries no staging ticket")
    expect(driver.reject(ticket or "", "carol"), "the rejection was refused")
    driver.dispatch_once()
    expect(effects_of(driver, "pay") == 0, "the rejected effect dispatched")
    expect(_widget_count(driver) == before + 1,
           "the rejection rolled back the committed record op — each operation "
           "was independently authorized (CS-023)")
