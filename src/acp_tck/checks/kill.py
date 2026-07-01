"""KILL profile — kill-switch semantics, in the TCK's *serialized* form.

Black-box note (docs/12 §5): the RFC's no-race guarantee (§9, CS-004) is a
concurrency property of the implementation's dispatch transaction; a TCK
cannot assert the absence of a race from outside. What it CAN assert is the
serialized contract the race-free implementation must satisfy at every
interleaving boundary: a kill issued before the dispatch step cancels the
staged action; a kill issued after it does not (and is never claimed to)
reverse the effect. The true concurrent race test remains an
implementation-internal obligation (the reference keeps one over real
Postgres row locks).
"""

from __future__ import annotations

from acp_tck.checks import PROFILE_KILL, check, expect
from acp_tck.checks._util import SESSION, expect_decision, expect_ticket, pay, setup, submit
from acp_tck.driver import CAP_KILL, CAP_STAGING, ConformanceDriver


def _pay_effects(driver: ConformanceDriver) -> int:
    return sum(1 for e in driver.effects() if e.get("action") == "pay")


@check("E1", "a session kill turns matching attempts into HALT; lifting restores", PROFILE_KILL,
       requires=[CAP_KILL])
def e1_session_kill(driver: ConformanceDriver) -> None:
    setup(driver)
    kill_id = driver.kill(scope="session", session_id=SESSION)
    expect_decision(submit(driver, pay(500)), "halt", "attempt in the killed session")
    r = submit(driver, pay(500, payee="PY2"), session="tck-other-session")
    expect(r.decision in ("allow", "hold"), f"a different session is unaffected (got {r.decision})")
    driver.lift(kill_id)
    expect_decision(submit(driver, pay(400, payee="PY3")), "allow", "after the kill is lifted")


@check("E2s", "kill before the dispatch step cancels; after it, nothing is reversed", PROFILE_KILL,
       requires=[CAP_KILL, CAP_STAGING])
def e2_serialized_no_race(driver: ConformanceDriver) -> None:
    setup(driver)
    # (a) staged, then killed, then dispatched ⇒ cancelled, never sent
    r = submit(driver, pay(500))
    expect_ticket(expect_decision(r, "allow", "staged pay"), "staged pay")
    kill_id = driver.kill(scope="agent", agent="tck-agent")
    driver.dispatch_once()
    expect(_pay_effects(driver) == 0, "a kill issued before dispatch must cancel the staged effect")
    driver.lift(kill_id)
    # (b) staged, dispatched, then killed ⇒ already out; kill reverses nothing
    r = submit(driver, pay(500, payee="PY2"))
    expect_decision(r, "allow", "second staged pay")
    driver.dispatch_once()
    expect(_pay_effects(driver) == 1, "the second pay dispatched")
    driver.kill(scope="agent", agent="tck-agent")
    expect(_pay_effects(driver) == 1, "a kill never un-sends a committed effect (guarantee scope, CS-004)")


@check("E6", "an action-class kill halts that action and nothing else", PROFILE_KILL,
       requires=[CAP_KILL])
def e6_action_class_kill(driver: ConformanceDriver) -> None:
    setup(driver)
    driver.kill(scope="action_class", action="pay")
    expect_decision(submit(driver, pay(500)), "halt", "the killed action class")
    from acp_tck.checks._util import email

    r = submit(driver, email())
    expect(r.decision in ("allow", "hold"), f"other actions are unaffected (got {r.decision})")
