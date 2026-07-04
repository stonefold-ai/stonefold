"""BATCH profile — atomic batch decision semantics (v0.5 CS-023; RFC §12, SIF §5)."""

from __future__ import annotations

from collections.abc import Sequence

from stonefold_tck.checks import PROFILE_BATCH, check, expect
from stonefold_tck.checks._util import ALICE, SESSION, pay, setup, submit
from stonefold_tck.driver import (
    CAP_APPROVALS,
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


def _effects(driver: ConformanceDriver) -> Sequence[object]:
    return [e for e in driver.effects() if e.get("action") == "pay"]


@check("H1", "any DENY refuses the whole batch before anything commits or stages",
       PROFILE_BATCH, requires=[CAP_BATCH, CAP_STAGING])
def h1_deny_refuses_batch(driver: ConformanceDriver) -> None:
    setup(driver)
    before = _widget_count(driver)
    r = _batch(driver, WIDGET, pay(500, country="XX"))  # denylisted country
    expect(r.decision == "deny", "a denied operation refuses the whole batch")
    expect(r.failing_index == 1, "the refusal names the failing operation (SIF §6)")
    expect(len(r.results) == 2, "the batch reports one result per operation")
    expect(_widget_count(driver) == before,
           "the record op did NOT commit — the batch refused before anything applied")
    driver.dispatch_once()
    expect(_effects(driver) == [], "no effect staged or dispatched from a refused batch")


@check("H2", "a HALT refuses the whole batch the same way", PROFILE_BATCH,
       requires=[CAP_BATCH, CAP_STAGING, CAP_KILL])
def h2_halt_refuses_batch(driver: ConformanceDriver) -> None:
    setup(driver)
    before = _widget_count(driver)
    kill_id = driver.kill(scope="action_class", resource="Widget", action="create")
    r = _batch(driver, WIDGET, pay(500))
    driver.lift(kill_id)
    expect(r.decision == "halt", "a halted operation refuses the whole batch")
    expect(r.failing_index == 0, "the refusal names the halted operation")
    expect(_widget_count(driver) == before, "nothing committed from the refused batch")
    driver.dispatch_once()
    expect(_effects(driver) == [], "nothing staged or dispatched from the refused batch")


@check("H3", "a HOLD does not refuse the batch: record commits, effect stages",
       PROFILE_BATCH, requires=[CAP_BATCH, CAP_STAGING, CAP_APPROVALS])
def h3_hold_commits_batch(driver: ConformanceDriver) -> None:
    setup(driver)
    before = _widget_count(driver)
    r = _batch(driver, WIDGET, pay(2000))  # above the approval threshold
    expect(r.decision == "hold", "a held operation does not refuse the batch")
    expect(r.failing_index is None, "the batch committed — no failing operation")
    expect(_widget_count(driver) == before + 1,
           "the record op committed atomically with the staging (§4.4)")
    ticket = r.results[1].ticket
    expect(ticket is not None, "the held effect carries its staging ticket")
    driver.dispatch_once()
    expect(_effects(driver) == [], "the held effect must not dispatch before approval")
    expect(driver.approve(ticket or "", "carol"), "approval accepted")
    driver.dispatch_once()
    expect(len(_effects(driver)) == 1, "the approved effect dispatches")


@check("H4", "a later rejection does not roll committed batch ops back",
       PROFILE_BATCH, requires=[CAP_BATCH, CAP_STAGING, CAP_APPROVALS])
def h4_rejection_keeps_committed_ops(driver: ConformanceDriver) -> None:
    setup(driver)
    before = _widget_count(driver)
    r = _batch(driver, WIDGET, pay(2000))
    ticket = r.results[1].ticket
    expect(ticket is not None, "the held effect carries its staging ticket")
    expect(driver.reject(ticket or "", "carol"), "rejection accepted")
    driver.dispatch_once()
    expect(_effects(driver) == [], "the rejected effect never dispatches")
    expect(_widget_count(driver) == before + 1,
           "the committed record op remains — independently authorized (CS-023)")
