# SPDX-License-Identifier: Apache-2.0
"""STAGING profile — the outbox, approvals, dual-authorization (RFC §4.4, §7.8/7.9).

Failure messages state the violation observed, not the expectation.
"""

from __future__ import annotations

from stonefold_tck.checks import PROFILE_STAGING, check, expect
from stonefold_tck.checks._util import (
    effects_of,
    expect_decision,
    expect_ticket,
    pay,
    setup,
    submit,
)
from stonefold_tck.driver import (
    CAP_APPROVALS,
    CAP_DISPATCH_FAILURE,
    CAP_STAGING,
    ConformanceDriver,
    Operation,
)


@check("D1", "an allowed effect is staged, then dispatched exactly once", PROFILE_STAGING,
       requires=[CAP_STAGING])
def d1_staged_exactly_once(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(500))
    expect_decision(r, "allow", "small pay")
    expect_ticket(r, "allowed effect")
    expect(effects_of(driver, "pay") == 0,
           "an effect left before the dispatch step (effects stage by default, CS-003)")
    driver.dispatch_once()
    expect(effects_of(driver, "pay") == 1, "the staged effect did not dispatch exactly once")
    driver.dispatch_once()  # a worker retry must not double-send
    expect(effects_of(driver, "pay") == 1,
           "a re-run of the worker double-sent the effect (idempotency)")


@check("D2", "requireApproval holds; a human releases or rejects", PROFILE_STAGING,
       requires=[CAP_STAGING, CAP_APPROVALS])
def d2_approval(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(2000))
    expect_decision(r, "hold", "pay above the approval threshold")
    ticket = expect_ticket(r, "held pay")
    driver.dispatch_once()
    expect(effects_of(driver, "pay") == 0, "a held action dispatched without approval")
    expect(driver.approve(ticket, "carol"), "a third party's approval was refused")
    driver.dispatch_once()
    expect(effects_of(driver, "pay") == 1, "the approved action did not dispatch")

    r = submit(driver, pay(2000, payee="PY9"))
    ticket = expect_ticket(expect_decision(r, "hold", "second held pay"), "held pay")
    expect(driver.reject(ticket, "carol"), "the rejection was refused")
    driver.dispatch_once()
    expect(effects_of(driver, "pay") == 1, "a rejected action dispatched")


@check("D3", "dualAuthorization needs two distinct non-actor identities", PROFILE_STAGING,
       requires=[CAP_STAGING, CAP_APPROVALS])
def d3_dual_authorization(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(6000))
    expect_decision(r, "hold", "pay above the dual-auth threshold")
    ticket = expect_ticket(r, "dual-auth pay")
    expect(not driver.approve(ticket, "alice"), "the actor approved its own action")
    expect(driver.approve(ticket, "carol"), "the first approver was refused")
    driver.dispatch_once()
    expect(effects_of(driver, "pay") == 0,
           "one approval released a dual-auth action (quorum 2)")
    expect(driver.approve(ticket, "dave"), "the second, distinct approver was refused")
    driver.dispatch_once()
    expect(effects_of(driver, "pay") == 1,
           "two distinct approvals did not release the action")


@check("D4", "a failed irreversible effect stages its declared compensation", PROFILE_STAGING,
       requires=[CAP_STAGING, CAP_DISPATCH_FAILURE])
def d4_compensation(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, Operation(resource="Payment", action="zap", target="P1"))
    expect_decision(r, "allow", "zap (irreversible, declared compensation)")
    driver.inject_dispatch_failure("zap")
    driver.dispatch_once()
    expect(effects_of(driver, "zap") == 0, "zap executed despite the injected failure")
    driver.dispatch_once()  # the auto-staged compensation dispatches on the next pass
    expect(
        effects_of(driver, "unzap") == 1,
        "the declared compensation (unzap) was not staged and dispatched after the failure",
    )
