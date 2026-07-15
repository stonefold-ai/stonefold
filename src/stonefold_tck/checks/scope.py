"""SCOPE profile — enforcement below the model (RFC §6.3).

Failure messages state the violation observed, not the expectation.
"""

from __future__ import annotations

from stonefold_tck.checks import PROFILE_SCOPE, check, expect
from stonefold_tck.checks._util import ALICE, BOB, expect_decision, pay, setup, submit
from stonefold_tck.driver import ConformanceDriver, Operation


@check("B1", "read scope is injected below the model", PROFILE_SCOPE)
def b1_read_scope(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, Operation(resource="Widget"), actor=ALICE)
    expect_decision(r, "allow", "scoped observe")
    rows = list(r.rows or [])
    expect(len(rows) == 3, f"alice owns 3 of 10 widgets but got {len(rows)} rows")
    expect(
        all(row.get("owner_id") == "alice" for row in rows),
        "a returned row is not alice's",
    )
    r = submit(driver, Operation(resource="Widget"), actor=BOB)
    expect(len(list(r.rows or [])) == 7,
           f"bob owns exactly 7 widgets but got {len(list(r.rows or []))} rows")


@check("B2", "scope on an effect denies an out-of-scope target pre-dispatch", PROFILE_SCOPE)
def b2_effect_scope(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(500, target="P2"), actor=ALICE)  # P2 is tenant t2
    expect_decision(r, "deny", "effect on a target outside the actor's tenant")
    expect(len(driver.effects()) == 0, "an effect left the gateway despite the deny")
    r = submit(driver, pay(500, target="P1"), actor=ALICE)  # P1 is tenant t1
    expect_decision(r, "allow", "effect on an in-scope target")


@check("B3", "the agent payload cannot set or widen its own scope", PROFILE_SCOPE)
def b3_payload_cannot_widen(driver: ConformanceDriver) -> None:
    setup(driver)
    # a hostile payload claims to be bob / asks for bob's rows — identity comes
    # from the transport (the submit call), so the result must be unchanged.
    r = submit(
        driver,
        Operation(resource="Widget", data={"owner_id": "bob", "actor": "bob", "scope": "*"}),
        actor=ALICE,
    )
    expect_decision(r, "allow", "observe with a spoofing payload")
    rows = list(r.rows or [])
    expect(len(rows) == 3, f"the payload spoof widened scope (got {len(rows)} rows)")
    expect(all(row.get("owner_id") == "alice" for row in rows),
           "a returned row is not alice's — the spoof leaked another actor's rows")
