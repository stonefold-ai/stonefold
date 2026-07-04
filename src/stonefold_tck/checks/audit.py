"""AUDIT profile — every outcome recorded; effects and evidence agree (RFC §11).

Black-box note: transactionality itself (CS-006, the audit write sharing the
settle transaction) is not observable from outside — what IS observable is its
consequence: after any sequence of operations, the set of executed effects and
the set of success-outcome audit records must agree exactly, and every refusal
must have left a record. Crash-consistency remains an implementation-internal
test (the reference keeps one over real Postgres).
"""

from __future__ import annotations

from stonefold_tck.checks import PROFILE_AUDIT, check, expect
from stonefold_tck.checks._util import SESSION, expect_decision, pay, setup, submit
from stonefold_tck.driver import CAP_AUDIT, CAP_KILL, CAP_STAGING, ConformanceDriver


@check("F1", "every decision — allow, hold, deny, halt — leaves an audit record", PROFILE_AUDIT,
       requires=[CAP_AUDIT, CAP_KILL])
def f1_every_outcome_recorded(driver: ConformanceDriver) -> None:
    setup(driver)
    expect_decision(submit(driver, pay(500)), "allow", "allowed pay")
    expect_decision(submit(driver, pay(2000, payee="PY2")), "hold", "held pay")
    expect_decision(submit(driver, pay(10001, payee="PY3")), "deny", "denied pay")
    driver.kill(scope="session", session_id=SESSION)
    expect_decision(submit(driver, pay(500, payee="PY4")), "halt", "halted pay")

    entries = list(driver.audit())
    decisions = {e.decision for e in entries}
    for wanted in ("allow", "hold", "deny", "halt"):
        expect(wanted in decisions, f"a {wanted!r} decision must be audited")
    for e in entries:
        if e.action == "pay":
            expect(e.resource == "Payment", "audit records carry the attempted resource/action")


@check("F2c", "executed effects and success-audit records agree exactly", PROFILE_AUDIT,
       requires=[CAP_AUDIT, CAP_STAGING])
def f2_effects_evidence_consistency(driver: ConformanceDriver) -> None:
    setup(driver)
    expect_decision(submit(driver, pay(500)), "allow", "pay 1")
    expect_decision(submit(driver, pay(400, payee="PY2")), "allow", "pay 2")
    expect_decision(submit(driver, pay(10001, payee="PY3")), "deny", "refused pay")
    driver.dispatch_once()

    executed = sum(1 for e in driver.effects() if e.get("action") == "pay")
    settled_ok = sum(
        1 for a in driver.audit() if a.action == "pay" and a.outcome == "success"
    )
    expect(executed == 2, f"exactly the two allowed pays executed (got {executed})")
    expect(
        settled_ok == executed,
        f"success-audit records ({settled_ok}) must equal executed effects ({executed}) — "
        "no effect without a record, no record without an effect (CS-006's observable face)",
    )
