"""CORE profile — authorization precedence (A) and deterministic gates (C).

Scenario ids follow ``tests/acceptance-scenarios.md``.
"""

from __future__ import annotations

from acp_tck.checks import PROFILE_CORE, check, expect
from acp_tck.checks._util import (
    ALICE,
    email,
    expect_decision,
    pay,
    setup,
    submit,
)
from acp_tck.driver import CAP_CLOCK, ConformanceDriver, Operation
from acp_tck.fixtures import POLICY_DENY_WINS, POLICY_GATE_LAYERS, POLICY_MISSING_PATH


@check("A1", "default deny: unlisted and unknown actions are refused", PROFILE_CORE)
def a1_default_deny(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, Operation(resource="Payment", action="unzap", target="P1"))
    expect_decision(r, "deny", "declared-but-not-allowed action (unzap)")
    r = submit(driver, Operation(resource="Ghost", action="boo"))
    expect_decision(r, "deny", "unknown resource/action")


@check("A2", "an explicit deny beats a matching allow", PROFILE_CORE)
def a2_deny_wins(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_DENY_WINS, seed_world=True)
    r = submit(driver, pay(5))
    expect_decision(r, "deny", "action in both allow and deny")


@check("A3", "action-level and kind-level gates both apply (AND)", PROFILE_CORE)
def a3_gate_layers(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_GATE_LAYERS)
    # good.example: passes the action-level allowlist AND the kind-level denylist
    expect_decision(submit(driver, email(domain="good.example")), "allow", "domain clean at both levels")
    # evil.example: fails the action-level allowlist
    expect_decision(submit(driver, email(domain="evil.example")), "deny", "action-level gate")
    # dual.example: passes the action-level allowlist but hits the KIND-level
    # denylist — a deny here proves the kind-level gate also applies (AND).
    expect_decision(submit(driver, email(domain="dual.example")), "deny", "kind-level gate")


@check("C1", "valueLimit bounds a numeric field", PROFILE_CORE)
def c1_value_limit(driver: ConformanceDriver) -> None:
    setup(driver)
    expect_decision(submit(driver, pay(10001)), "deny", "amount above valueLimit max")
    r = submit(driver, pay(500))
    expect(r.decision in ("allow", "hold"), f"amount within limit must not be denied (got {r.decision})")
    expect_decision(r, "allow", "amount 500 needs no approval")


@check("C2", "rate limits per window and per subject", PROFILE_CORE, requires=[CAP_CLOCK])
def c2_rate(driver: ConformanceDriver) -> None:
    setup(driver)
    for n in (1, 2):
        expect_decision(submit(driver, pay(500, payee="PY1")), "allow", f"pay #{n} to PY1")
    expect_decision(submit(driver, pay(500, payee="PY1")), "deny", "pay #3 to PY1 (rate 2/hour)")
    expect_decision(submit(driver, pay(500, payee="PY2")), "allow", "pay #1 to a different payee")


@check("C3", "allowlist and denylist membership on fields", PROFILE_CORE)
def c3_lists(driver: ConformanceDriver) -> None:
    setup(driver)
    expect_decision(submit(driver, email(domain="evil.example")), "deny", "recipient domain not allowlisted")
    expect_decision(submit(driver, email(domain="good.example")), "allow", "allowlisted recipient domain")
    expect_decision(submit(driver, pay(500, country="XX")), "deny", "denylisted destination country")


@check("C4", "transition legal from-states are enforced", PROFILE_CORE)
def c4_from_states(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, Operation(resource="Order", action="confirm", target="O1"))
    expect_decision(r, "allow", "confirm from 'pending'")
    r = submit(driver, Operation(resource="Order", action="confirm", target="O2"))
    expect_decision(r, "deny", "confirm from 'confirmed' (not a legal from-state)")


@check("C5", "quantityCap: per-subject cumulative cap", PROFILE_CORE, requires=[CAP_CLOCK])
def c5_quantity_cap(driver: ConformanceDriver) -> None:
    setup(driver)

    def administer(target: str) -> Operation:
        return Operation(resource="Med", action="administer",
                         data={"drug": "aspirin", "patientId": "x"}, target=target)

    expect_decision(submit(driver, administer("M1")), "allow", "dose 1 for PAT1")
    expect_decision(submit(driver, administer("M2")), "allow", "dose 2 for PAT1")
    expect_decision(submit(driver, administer("M1")), "deny", "dose 3 for PAT1 (cap 2/24h)")
    expect_decision(submit(driver, administer("M3")), "allow", "dose 1 for PAT2 (separate subject)")


@check("C6", "disclosure binds a restricted read to allowed sinks", PROFILE_CORE)
def c6_disclosure(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, Operation(resource="Sealed", action="readSealed", target="S1", sink="ops"))
    expect_decision(r, "deny", "restricted read to a non-allowed sink")
    r = submit(driver, Operation(resource="Sealed", action="readSealed", target="S1", sink="tckSink"))
    expect_decision(r, "allow", "restricted read to the allowed sink")


@check("C7", "contentCheck hook verdict blocks the payload", PROFILE_CORE)
def c7_content_check(driver: ConformanceDriver) -> None:
    setup(driver)
    expect_decision(submit(driver, email(body="please BLOCK-ME now")), "deny", "hook-blocked content")
    expect_decision(submit(driver, email(body="all clear")), "allow", "clean content")


@check("C8", "a condition path absent at runtime fails CLOSED", PROFILE_CORE)
def c8_missing_path_fails_closed(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_MISSING_PATH)
    # the gate's `when` references resource.no_such_field — absent on the target.
    # Fail-closed means the gate DENIES; "condition false ⇒ gate not applicable ⇒
    # allow" would be the dangerous wrong reading (RFC §8, CS-005).
    r = submit(driver, pay(5))
    expect_decision(r, "deny", "gate condition over a missing path")


@check("C9", "named precondition check gates the action", PROFILE_CORE)
def c9_precondition_check(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, Operation(resource="Med", action="administer",
                                 data={"drug": "x", "patientId": "x"}, target="M4"))
    expect_decision(r, "deny", "tck.flagSet on a target with flag=false")
