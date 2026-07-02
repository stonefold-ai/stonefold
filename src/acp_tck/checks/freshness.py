"""FRESHNESS profile — v0.4 timing guarantees (RFC §12 CS-017, §6.3 CS-018).

A driver claiming ``CAP_FRESHNESS`` MUST run with the REQUIRED TCK freshness
config: default decision TTL **24 hours**, irreversible TTL **30 minutes**
(``acp_tck.driver``). The v0.4 settle reasons asserted here — ``stale-decision``,
``stale-guard:<gate>``, ``scope-lost`` — are normative (RFC §12/§6.3), so the
checks compare them exactly.

Deliberately not checked black-box: the *declared residual window* of a window
connector appearing in the audit record (B5's second clause) — the TCK's
normalized audit shape does not carry ``scopeApplied``; both reassertion forms
are covered through their shared observable (the effect does not land and the
settle reason is ``scope-lost``).
"""

from __future__ import annotations

from datetime import timedelta

from acp_tck.checks import PROFILE_FRESHNESS, check, expect
from acp_tck.checks._util import T0, expect_decision, expect_ticket, pay, setup, submit
from acp_tck.driver import (
    CAP_APPROVALS,
    CAP_AUDIT,
    CAP_CLOCK,
    CAP_FRESHNESS,
    CAP_SCOPE_REASSERT,
    CAP_STAGING,
    AuditEntry,
    ConformanceDriver,
)

_TTL_CAPS = (CAP_STAGING, CAP_CLOCK, CAP_AUDIT, CAP_FRESHNESS)


def _pay_effects(driver: ConformanceDriver) -> int:
    return sum(1 for e in driver.effects() if e.get("action") == "pay")


def _last_audit(driver: ConformanceDriver) -> AuditEntry:
    entries = driver.audit()
    expect(len(entries) > 0, "expected at least one audit record")
    return entries[-1]


@check("D5", "a staged effect whose TTL lapsed is cancelled at claim, never dispatched",
       PROFILE_FRESHNESS, requires=_TTL_CAPS)
def d5_expired_decision(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(500))  # irreversible ⇒ the REQUIRED 30-minute TTL
    expect_decision(r, "allow", "small pay")
    expect_ticket(r, "staged pay")

    driver.set_clock(T0 + timedelta(hours=1))  # past the irreversible TTL
    driver.dispatch_once()
    expect(_pay_effects(driver) == 0, "an expired decision must never dispatch")
    last = _last_audit(driver)
    expect(last.decision == "deny" and last.outcome == "cancelled",
           f"the stale cancel must be audited (deny/cancelled), got {last.decision}/{last.outcome}")
    expect(last.reason == "stale-decision",
           f"settle reason must be 'stale-decision' (RFC §12), got {last.reason!r}")


@check("D5b", "a late approval does not resurrect an expired decision",
       PROFILE_FRESHNESS, requires=_TTL_CAPS + (CAP_APPROVALS,))
def d5_late_approval(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(2000))  # requireApproval band ⇒ hold
    expect_decision(r, "hold", "pay above the approval threshold")
    ticket = expect_ticket(r, "held pay")

    driver.set_clock(T0 + timedelta(hours=1))  # the approval arrives too late
    approved = driver.approve(ticket, "carol")
    # Refusing the late approval outright is also conformant; if it is
    # accepted, the TTL must still cancel the row at claim.
    driver.dispatch_once()
    expect(_pay_effects(driver) == 0,
           "an approval after expiry must not release the effect — re-submit and re-decide")
    if approved:
        expect(_last_audit(driver).reason == "stale-decision",
               "the late-approved row must settle 'stale-decision'")


@check("D6", "a volatile gate (denylist) is re-validated inside the dispatch claim",
       PROFILE_FRESHNESS, requires=_TTL_CAPS)
def d6_volatile_revalidation(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(100, country="SK"))  # SK is not blocked at decision time
    expect_decision(r, "allow", "pay to an unsanctioned country")

    # the sanctions update lands between decision and dispatch
    driver.update_named_set("tck-blocked-countries", ["XX", "SK"])
    driver.dispatch_once()
    expect(_pay_effects(driver) == 0, "a dispatch-time denylist hit must never partially dispatch")
    last = _last_audit(driver)
    expect(last.decision == "deny" and last.outcome == "cancelled",
           f"the stale-guard cancel must be audited, got {last.decision}/{last.outcome}")
    expect(last.reason == "stale-guard:denylist",
           f"settle reason must be 'stale-guard:denylist' (RFC §12), got {last.reason!r}")

    again = submit(driver, pay(100, country="SK"))
    expect_decision(again, "deny", "a fresh submission after the set update")


@check("D6b", "counter gates are NOT re-run at dispatch (no double-counting)",
       PROFILE_FRESHNESS, requires=_TTL_CAPS)
def d6_counters_not_rerun(driver: ConformanceDriver) -> None:
    # rate 2/hour per payee: two decisions consume the counter exactly; a
    # wrong re-run at dispatch would see 3+ hits and cancel one of them.
    setup(driver)
    expect_decision(submit(driver, pay(100, payee="PYX")), "allow", "first pay")
    expect_decision(submit(driver, pay(100, payee="PYX")), "allow", "second pay")
    driver.set_clock(T0 + timedelta(minutes=10))  # within every TTL
    driver.dispatch_once()
    expect(_pay_effects(driver) == 2,
           "both staged pays must dispatch — re-running the rate counter double-counts")


@check("D6c", "an approval grant is not re-requested at dispatch",
       PROFILE_FRESHNESS, requires=_TTL_CAPS + (CAP_APPROVALS,))
def d6_approval_not_rerequested(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(2000))
    ticket = expect_ticket(expect_decision(r, "hold", "held pay"), "held pay")
    expect(driver.approve(ticket, "carol"), "approval accepted")
    driver.set_clock(T0 + timedelta(minutes=10))  # within the 30-minute TTL
    driver.dispatch_once()
    expect(_pay_effects(driver) == 1,
           "the grant IS the release — within the TTL the approved effect must dispatch")


@check("B4", "a target reassigned after the decision never receives the effect (scope no-race)",
       PROFILE_FRESHNESS, requires=(CAP_STAGING, CAP_AUDIT, CAP_SCOPE_REASSERT))
def b4_scope_no_race(driver: ConformanceDriver) -> None:
    setup(driver)
    r = submit(driver, pay(500, target="P1"))  # P1 is in alice's tenant at decision time
    expect_decision(r, "allow", "in-scope pay")

    # the race: P1 moves to another tenant before dispatch
    driver.seed("Payment", [{"id": "P1", "tenant": "t2"}, {"id": "P2", "tenant": "t2"}])
    driver.dispatch_once()
    expect(_pay_effects(driver) == 0,
           "the effect must not land on un-authorized state (RFC §6.3)")
    last = _last_audit(driver)
    expect(last.decision == "deny" and last.outcome == "failure",
           f"the scope failure must be audited, got {last.decision}/{last.outcome}")
    expect(last.reason == "scope-lost",
           f"settle reason must be 'scope-lost' (RFC §6.3), got {last.reason!r}")
