# SPDX-License-Identifier: Apache-2.0
"""L1–L5 (profile ``match``) and M1–M5 (profile ``consume``) — obligation
matching and the reservation lifecycle (v0.6 CS-032–CS-036).

An intent must correspond to exactly one open obligation, read back from the
registry the gateway queries — never from the agent's copy; the matched line
is reserved with the staging commit, consumed with the settle, and released on
every terminal non-success. All black-box: the kit observes decisions, effects,
and the audit, never adapter internals.
"""

from __future__ import annotations

from typing import Any

from stonefold_tck.checks import PROFILE_CONSUME, PROFILE_MATCH, check, expect
from stonefold_tck.checks._util import (
    SESSION,
    effects_of,
    expect_decision,
    expect_ticket,
    setup,
    submit,
)
from stonefold_tck.driver import (
    CAP_AUDIT,
    CAP_FEEDBACK,
    CAP_KILL,
    CAP_OBLIGATION,
    CAP_STAGING,
    ConformanceDriver,
    Operation,
)
from stonefold_tck.fixtures import POLICY_MATCH

REGISTRY = "tck.orders"


def _order(amount: float = 800.0, *, vendor: str = "PY1", state: str = "open") -> dict[str, Any]:
    return {"vendorId": vendor, "state": state,
            "line": {"amount": amount, "state": "unconsumed"}}


def _pay(amount: float = 800.0, *, payee: str = "PY1", extra: dict[str, Any] | None = None) -> Operation:
    data: dict[str, Any] = {
        "amount": amount, "destinationCountry": "SK", "payeeId": payee,
    }
    if extra:
        data.update(extra)
    return Operation(resource="Payment", action="pay", data=data, target="P1")


def _setup(driver: ConformanceDriver, records: dict[str, dict[str, Any]]) -> None:
    setup(driver, policy=POLICY_MATCH)
    driver.seed_obligations(REGISTRY, records)


def _pay_effects(driver: ConformanceDriver) -> int:
    return effects_of(driver, "pay")


# ==========================================================================
# L — decision-time matching (profile: match)
# ==========================================================================
@check(
    "L1",
    "exactly one open obligation within tolerance passes and dispatches",
    PROFILE_MATCH,
    requires=[CAP_OBLIGATION, CAP_STAGING],
)
def l1_exactly_one_passes(driver: ConformanceDriver) -> None:
    _setup(driver, {"ORD-1": _order(800.0)})
    result = expect_decision(submit(driver, _pay(820.0)), "allow", "one open line, within 10%")
    expect_ticket(result, "the staged payment")
    driver.dispatch_once()
    expect(_pay_effects(driver) == 1, "the matched payment did not dispatch")


@check(
    "L2",
    "zero candidates resolve onNoMatch with the normative no-match refusal",
    PROFILE_MATCH,
    requires=[CAP_OBLIGATION, CAP_FEEDBACK],
)
def l2_zero_is_no_match(driver: ConformanceDriver) -> None:
    _setup(driver, {"ORD-1": _order(800.0)})
    result = expect_decision(
        submit(driver, _pay(800.0, payee="PY9")), "deny", "no order for this vendor"
    )
    expect(
        result.reason_code == "no-match",
        f"the zero-candidate refusal carries {result.reason_code!r} instead of "
        f"the normative 'no-match'",
    )
    still_open = expect_decision(
        submit(driver, _pay(800.0), session="tck-s2"), "allow", "the open line still matches"
    )
    expect(still_open.ticket is not None, "the still-open line did not stage a ticket")


@check(
    "L3",
    "several candidates hold for a human — the gateway never picks",
    PROFILE_MATCH,
    requires=[CAP_OBLIGATION, CAP_STAGING],
)
def l3_ambiguous_holds_never_picks(driver: ConformanceDriver) -> None:
    _setup(driver, {"ORD-1": _order(800.0), "ORD-2": _order(810.0)})
    result = expect_decision(submit(driver, _pay(805.0)), "hold", "two candidate lines")
    expect_ticket(result, "the held-ambiguous intent")
    driver.dispatch_once()
    expect(
        _pay_effects(driver) == 0,
        "the gateway auto-selected among ambiguous candidates (CS-032: never a pick)",
    )


@check(
    "L4",
    "a forged obligation copy in data.* changes nothing — the re-read record decides",
    PROFILE_MATCH,
    requires=[CAP_OBLIGATION],
)
def l4_forged_copy_ignored(driver: ConformanceDriver) -> None:
    _setup(driver, {"ORD-1": _order(800.0)})
    forged = {"obligation": {"line": {"amount": 5000.0, "state": "unconsumed"}}}
    expect_decision(
        submit(driver, _pay(5000.0, extra=forged)), "deny",
        "an intent shipping its own flattering copy of the obligation (CS-036)",
    )


@check(
    "L5",
    "an unreachable obligation registry fails closed (irreversible effect)",
    PROFILE_MATCH,
    requires=[CAP_OBLIGATION],
)
def l5_outage_fails_closed(driver: ConformanceDriver) -> None:
    _setup(driver, {"ORD-1": _order(800.0)})
    driver.set_obligation_outage(REGISTRY, True)
    expect_decision(submit(driver, _pay(800.0)), "deny", "registry down (fail closed)")
    driver.set_obligation_outage(REGISTRY, False)
    expect_decision(
        submit(driver, _pay(800.0), session="tck-s2"), "allow", "registry back up"
    )


# ==========================================================================
# M — the reservation lifecycle (profile: consume)
# ==========================================================================
@check(
    "M1",
    "the reservation is taken with the staging — a second intent against the line no-matches",
    PROFILE_CONSUME,
    requires=[CAP_OBLIGATION, CAP_STAGING, CAP_FEEDBACK],
)
def m1_reserved_with_staging(driver: ConformanceDriver) -> None:
    _setup(driver, {"ORD-1": _order(800.0)})
    expect_decision(submit(driver, _pay(800.0)), "allow", "first intent (staged)")
    expect(_pay_effects(driver) == 0, "an effect left before the dispatch step")
    second = expect_decision(
        submit(driver, _pay(800.0), session="tck-s2"), "deny",
        "the line is reserved by the FIRST intent's staged row",
    )
    expect(
        second.reason_code == "no-match",
        f"the spoken-for line's refusal carries {second.reason_code!r} instead "
        f"of the normative 'no-match'",
    )


@check(
    "M2",
    "consume lands with the settle — a consumed line refuses resubmission",
    PROFILE_CONSUME,
    requires=[CAP_OBLIGATION, CAP_STAGING, CAP_FEEDBACK],
)
def m2_consumed_at_settle(driver: ConformanceDriver) -> None:
    _setup(driver, {"ORD-1": _order(800.0)})
    expect_decision(submit(driver, _pay(800.0)), "allow", "matched payment")
    driver.dispatch_once()
    expect(_pay_effects(driver) == 1, "the matched payment did not dispatch")
    resubmit = expect_decision(
        submit(driver, _pay(800.0), session="tck-s2"), "deny",
        "the SAME invoice resubmitted after its line was consumed",
    )
    expect(resubmit.reason_code == "no-match",
           f"the consumed line's refusal carries {resubmit.reason_code!r} "
           f"instead of the normative 'no-match'")


@check(
    "M3",
    "a cancellation releases the line for a fresh intent",
    PROFILE_CONSUME,
    requires=[CAP_OBLIGATION, CAP_STAGING, CAP_KILL],
)
def m3_cancel_releases(driver: ConformanceDriver) -> None:
    _setup(driver, {"ORD-1": _order(800.0)})
    expect_decision(submit(driver, _pay(800.0)), "allow", "first intent (staged)")
    kill_id = driver.kill(scope="session", session_id=SESSION)
    driver.dispatch_once()  # the claim cancels under the kill and releases the line
    expect(_pay_effects(driver) == 0, "the killed payment dispatched")
    driver.lift(kill_id)
    retry = expect_decision(
        submit(driver, _pay(800.0), session="tck-s2"), "allow",
        "the released line matches a fresh intent",
    )
    expect(retry.ticket is not None, "the fresh intent did not stage a ticket")
    driver.dispatch_once()
    expect(_pay_effects(driver) == 1, "the fresh intent did not dispatch exactly once")


@check(
    "M4",
    "retries never double-consume — one line, one payment, ever",
    PROFILE_CONSUME,
    requires=[CAP_OBLIGATION, CAP_STAGING, CAP_AUDIT],
)
def m4_no_double_consume(driver: ConformanceDriver) -> None:
    _setup(driver, {"ORD-1": _order(800.0)})
    expect_decision(submit(driver, _pay(800.0)), "allow", "matched payment")
    driver.dispatch_once()
    driver.dispatch_once()  # a second worker pass must find nothing to re-send
    expect(_pay_effects(driver) == 1, "the effect dispatched more than once")
    successes = [
        r for r in driver.audit()
        if r.action == "pay" and r.decision == "allow" and r.outcome == "success"
    ]
    expect(len(successes) == 1, f"expected exactly one success settle, got {len(successes)}")
    expect_decision(
        submit(driver, _pay(800.0), session="tck-s2"), "deny",
        "a second distinct intent against the consumed line",
    )


@check(
    "M5",
    "a reservation lost to another intent cancels at claim — stale-guard:requireMatch",
    PROFILE_CONSUME,
    requires=[CAP_OBLIGATION, CAP_STAGING, CAP_AUDIT],
)
def m5_lost_reservation_cancels_at_claim(driver: ConformanceDriver) -> None:
    # CS-035's dispatch-time liveness check, through its one black-box window:
    # the adapter loses the reservation out-of-band (its own orphan-expiry TTL,
    # RFC §12 — ``seed_obligations`` models it by clearing reservation state),
    # a second intent legitimately re-reserves the freed line, and the FIRST
    # intent's claim must then find its reservation gone and cancel with the
    # normative ``stale-guard:requireMatch`` — never dispatch a payment whose
    # obligation now belongs to someone else.
    _setup(driver, {"ORD-1": _order(800.0)})
    expect_decision(submit(driver, _pay(800.0)), "allow", "first intent (staged, reserved)")
    driver.seed_obligations(REGISTRY, {"ORD-1": _order(800.0)})  # the adapter forgot
    expect_decision(
        submit(driver, _pay(800.0), session="tck-s2"), "allow",
        "a second intent over the adapter-freed line",
    )
    driver.dispatch_once()
    expect(
        _pay_effects(driver) == 1,
        "one obligation line paid twice — exactly one of the two intents may dispatch",
    )
    reasons = [r.reason for r in driver.audit()]
    expect(
        any(r == "stale-guard:requireMatch" for r in reasons),
        f"the losing intent's cancel lacks the normative 'stale-guard:requireMatch' "
        f"reason (CS-035), got {reasons[-4:]}",
    )
