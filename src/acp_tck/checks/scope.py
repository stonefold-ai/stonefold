"""SCOPE profile — enforcement below the model (RFC §6.3)."""

from __future__ import annotations

from acp_tck.checks import PROFILE_SCOPE, check, expect
from acp_tck.checks._util import ALICE, BOB, expect_decision, pay, setup, submit
from acp_tck.driver import ConformanceDriver, Operation


@check("B1", "read scope is injected below the model", PROFILE_SCOPE)
def b1_read_scope(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, Operation(resource="Widget"), actor=ALICE)
    expect_decision(r, "allow", "scoped observe")
    rows = list(r.rows or [])
    expect(len(rows) == 3, f"alice owns 3 of 10 widgets; got {len(rows)} rows")
    expect(
        all(row.get("owner_id") == "alice" for row in rows),
        "every returned row must be alice's",
    )
    r = submit(driver, Operation(resource="Widget"), actor=BOB)
    expect(len(list(r.rows or [])) == 7, "bob sees exactly his 7 widgets")


@check("B2", "scope on an effect denies an out-of-scope target pre-dispatch", PROFILE_SCOPE)
def b2_effect_scope(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(500, target="P2"), actor=ALICE)  # P2 is tenant t2
    expect_decision(r, "deny", "effect on a target outside the actor's tenant")
    expect(len(driver.effects()) == 0, "nothing may have left the gateway")
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
    expect(len(rows) == 3, f"payload spoof must not widen scope (got {len(rows)} rows)")
    expect(all(row.get("owner_id") == "alice" for row in rows), "rows are still alice's")
