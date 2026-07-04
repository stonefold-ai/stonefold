"""STAGING profile — the outbox, approvals, dual-authorization (RFC §4.4, §7.8/7.9)."""

from __future__ import annotations

from stonefold_tck.checks import PROFILE_STAGING, check, expect
from stonefold_tck.checks._util import expect_decision, expect_ticket, pay, setup, submit
from stonefold_tck.driver import (
    CAP_APPROVALS,
    CAP_DISPATCH_FAILURE,
    CAP_STAGING,
    ConformanceDriver,
    Operation,
)


def _effects_of(driver: ConformanceDriver, action: str) -> int:
    return sum(1 for e in driver.effects() if e.get("action") == action)


@check("D1", "an allowed effect is staged, then dispatched exactly once", PROFILE_STAGING,
       requires=[CAP_STAGING])
def d1_staged_exactly_once(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(500))
    expect_decision(r, "allow", "small pay")
    expect_ticket(r, "allowed effect")
    expect(_effects_of(driver, "pay") == 0, "nothing leaves before dispatch (staged by default, CS-003)")
    driver.dispatch_once()
    expect(_effects_of(driver, "pay") == 1, "dispatch executes the staged effect once")
    driver.dispatch_once()  # a worker retry must not double-send
    expect(_effects_of(driver, "pay") == 1, "re-running the worker must not double-send (idempotency)")


@check("D2", "requireApproval holds; a human releases or rejects", PROFILE_STAGING,
       requires=[CAP_STAGING, CAP_APPROVALS])
def d2_approval(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(2000))
    expect_decision(r, "hold", "pay above the approval threshold")
    ticket = expect_ticket(r, "held pay")
    driver.dispatch_once()
    expect(_effects_of(driver, "pay") == 0, "a held action must not dispatch")
    expect(driver.approve(ticket, "carol"), "approval by a third party is accepted")
    driver.dispatch_once()
    expect(_effects_of(driver, "pay") == 1, "the approved action dispatches")

    r = submit(driver, pay(2000, payee="PY9"))
    ticket = expect_ticket(expect_decision(r, "hold", "second held pay"), "held pay")
    expect(driver.reject(ticket, "carol"), "rejection is accepted")
    driver.dispatch_once()
    expect(_effects_of(driver, "pay") == 1, "a rejected action never dispatches")


@check("D3", "dualAuthorization needs two distinct non-actor identities", PROFILE_STAGING,
       requires=[CAP_STAGING, CAP_APPROVALS])
def d3_dual_authorization(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(6000))
    expect_decision(r, "hold", "pay above the dual-auth threshold")
    ticket = expect_ticket(r, "dual-auth pay")
    expect(not driver.approve(ticket, "alice"), "the actor cannot approve its own action")
    expect(driver.approve(ticket, "carol"), "first approver accepted")
    driver.dispatch_once()
    expect(_effects_of(driver, "pay") == 0, "one approval is not enough (quorum 2)")
    expect(driver.approve(ticket, "dave"), "second, distinct approver accepted")
    driver.dispatch_once()
    expect(_effects_of(driver, "pay") == 1, "two distinct approvals release the action")


@check("D4", "a failed irreversible effect stages its declared compensation", PROFILE_STAGING,
       requires=[CAP_STAGING, CAP_DISPATCH_FAILURE])
def d4_compensation(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, Operation(resource="Payment", action="zap", target="P1"))
    expect_decision(r, "allow", "zap (irreversible, declared compensation)")
    driver.inject_dispatch_failure("zap")
    driver.dispatch_once()
    expect(_effects_of(driver, "zap") == 0, "the injected failure means zap did not execute")
    driver.dispatch_once()  # the auto-staged compensation dispatches on the next pass
    expect(
        _effects_of(driver, "unzap") == 1,
        "the declared compensation (unzap) must be staged and dispatched after the failure",
    )
