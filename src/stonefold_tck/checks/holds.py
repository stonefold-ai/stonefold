# SPDX-License-Identifier: Apache-2.0
"""J1–J7 — the hold substrate (v0.6 CS-026/027/028/031; profile ``hold-precondition``).

A hold-capable check's judgment-shaped ambiguity suspends the intent for a
human; a code-less hold, a check outage, and a hold with no resolvable release
contract all resolve FAIL (a hold must be worth — and reachable by — a human's
attention); composed holds bind every contract; held rows expire actively, on
the injected clock that anchored the staging TTL; duplicate holds collapse
into one queue item while distinct questions stay distinct.

The ``tck.holdOnMarker``/``tck.codelessHold`` checks read the resolved
TARGET's fields (docs/12 §3) — so the TCK moves the WORLD (re-seeding the
target) to resolve a question, exactly the way the pattern works in life: the
clerk fixes the record, not the intent.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from stonefold_tck.checks import PROFILE_HOLD, check, expect
from stonefold_tck.checks._util import T0, expect_decision, expect_ticket, setup, submit
from stonefold_tck.driver import (
    CAP_APPROVALS,
    CAP_AUDIT,
    CAP_CLOCK,
    CAP_FEEDBACK,
    CAP_FRESHNESS,
    CAP_HOLD,
    CAP_STAGING,
    ConformanceDriver,
    Operation,
)
from stonefold_tck.fixtures import POLICY_HOLD, POLICY_HOLD_NO_RESOLVER


def _pay(amount: float, *, target: str) -> Operation:
    return Operation(
        resource="Payment",
        action="pay",
        data={"amount": amount, "destinationCountry": "SK", "payeeId": "PY1"},
        target=target,
    )


def _seed_targets(driver: ConformanceDriver, **flags: dict[str, Any]) -> None:
    """Seed Payment target rows: ``_seed_targets(driver, PQ={"hold": True})``."""
    driver.seed(
        "Payment",
        [{"id": ref, "tenant": "t1", **fields} for ref, fields in flags.items()],
    )


@check(
    "J1",
    "a hold-capable check's hold stages the intent with its reason code",
    PROFILE_HOLD,
    requires=[CAP_HOLD, CAP_STAGING, CAP_FEEDBACK],
)
def j1_hold_stages_with_code(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_HOLD, seed_world=False)
    _seed_targets(driver, PQ={"hold": True})
    result = expect_decision(submit(driver, _pay(500, target="PQ")), "hold",
                             "judgment-shaped ambiguity on the target")
    expect_ticket(result, "the held intent")
    expect(
        result.reason_code == "tck-queue",
        f"the hold lost the check's declared reason code 'tck-queue' "
        f"(got {result.reason_code!r})",
    )
    expect(
        result.retry_class == "escalate",
        f"the hold lost its declared retry class — the registry declares "
        f"tck-queue: escalate (CS-029), got {result.retry_class!r}",
    )
    driver.dispatch_once()
    expect(len(driver.effects()) == 0, "a held effect dispatched")


@check(
    "J2",
    "a hold without a reason code resolves FAIL (implementation error)",
    PROFILE_HOLD,
    requires=[CAP_HOLD],
)
def j2_codeless_hold_fails(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_HOLD, seed_world=False)
    _seed_targets(driver, PB={"badhold": True})
    expect_decision(submit(driver, _pay(500, target="PB")), "deny",
                    "a code-less hold (CS-026 rule 2)")


@check(
    "J3",
    "composed holds bind EVERY contract — a resolver or an approver alone releases nothing",
    PROFILE_HOLD,
    requires=[CAP_HOLD, CAP_STAGING, CAP_APPROVALS],
)
def j3_multi_hold_requires_all_contracts(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_HOLD, seed_world=False)
    _seed_targets(driver, PQ={"hold": True}, PR={"hold": True})
    # amount > 1000 ⇒ the approval gate ALSO holds: two release contracts.
    result = expect_decision(submit(driver, _pay(2000, target="PQ")), "hold",
                             "precondition-hold + approval")
    ticket = expect_ticket(result, "the doubly-held intent")

    # the clerk resolves the question in the WORLD, so the released row's
    # dispatch-time re-validation finds it answered (CS-017).
    _seed_targets(driver, PQ={"hold": False}, PR={"hold": True})

    # the approval-bypass regression (CS-027): the approval alone must not
    # promote a row the precondition also holds…
    expect(driver.approve(ticket, "tck-approver-1"), "the approver's credit was refused")
    driver.dispatch_once()
    expect(len(driver.effects()) == 0,
           "approval alone released a row the precondition also held (CS-027 bypass)")
    # …and the resolver's release satisfies ONLY the precondition contract.
    expect(driver.resolve(ticket, "tck-resolver-1", gate="precondition"),
           "the resolver's release was refused")
    driver.dispatch_once()
    expect(len(driver.effects()) == 1,
           "both contracts were satisfied but the effect did not dispatch exactly once")

    # inverse order on a fresh intent: resolver first, then still held.
    second = expect_decision(
        submit(driver, _pay(2000, target="PR"), session="tck-s2"),
        "hold", "second doubly-held intent",
    )
    ticket2 = expect_ticket(second, "the second held intent")
    _seed_targets(driver, PQ={"hold": False}, PR={"hold": False})
    expect(driver.resolve(ticket2, "tck-resolver-1", gate="precondition"),
           "the resolver's release was refused")
    driver.dispatch_once()
    expect(len(driver.effects()) == 1,
           "the resolver alone released a row the approval also held (CS-027 bypass)")
    expect(driver.approve(ticket2, "tck-approver-1"), "the approver's credit was refused")
    driver.dispatch_once()
    expect(len(driver.effects()) == 2,
           "both contracts were satisfied but the second effect did not dispatch")


@check(
    "J4",
    "held rows expire actively, on the injected clock that anchored the staging TTL",
    PROFILE_HOLD,
    requires=[CAP_HOLD, CAP_STAGING, CAP_CLOCK, CAP_FRESHNESS, CAP_AUDIT],
)
def j4_expiry_on_the_injected_clock(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_HOLD, seed_world=False)
    _seed_targets(driver, PQ={"hold": True})
    result = expect_decision(submit(driver, _pay(500, target="PQ")), "hold", "held intent")
    ticket = expect_ticket(result, "the held intent")

    # pay is irreversible ⇒ the REQUIRED TCK staging TTL is 30 minutes. One
    # minute short of the deadline nothing expires — this boundary is what
    # proves the deadline is anchored at STAGING on the injected clock, not at
    # some wall-clock the kit cannot see.
    driver.set_clock(T0 + timedelta(minutes=29))
    expect(driver.sweep_holds() == 0, "the hold expired BEFORE its staging TTL")

    driver.set_clock(T0 + timedelta(minutes=31))
    expect(driver.sweep_holds() >= 1, "the lapsed hold was not expired by the sweep")
    reasons = [r.reason for r in driver.audit()]
    expect(
        any(r == "expired-hold:precondition" for r in reasons),
        f"the expiry settle lacks the 'expired-hold:precondition' reason "
        f"(CS-028), got {reasons[-3:]}",
    )
    # a late release must not resurrect the expired row (refusing the release
    # outright is also conformant — either way nothing may dispatch).
    driver.resolve(ticket, "tck-resolver-1", gate="precondition")
    driver.dispatch_once()
    expect(len(driver.effects()) == 0,
           "a release AFTER expiry resurrected the hold (CS-028: expired is settled)")


@check(
    "J5",
    "a check outage resolves FAIL, never HOLD (outages fail; only readable ambiguity holds)",
    PROFILE_HOLD,
    requires=[CAP_HOLD],
)
def j5_outage_fails_never_holds(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_HOLD, seed_world=False)
    _seed_targets(driver, PX={"crash": True})
    expect_decision(submit(driver, _pay(500, target="PX")), "deny",
                    "a crashing check (CS-026 rule 1)")


@check(
    "J6",
    "duplicate holds collapse — one question, one queue item, an attempt count",
    PROFILE_HOLD,
    requires=[CAP_HOLD, CAP_STAGING, CAP_AUDIT],
)
def j6_duplicate_holds_collapse(driver: ConformanceDriver) -> None:
    # CS-031: the same question asked twice within the REQUIRED TCK dedupe
    # window (one hour) is one queue item — the agent gets the SAME ticket,
    # and each attempt is still audited. CS-040 (v0.6.1) sharpens the
    # identity: a DIFFERENT question (a different target here) must NOT
    # collapse — over-collapsing loses a question.
    setup(driver, policy=POLICY_HOLD, seed_world=False)
    _seed_targets(driver, PQ={"hold": True}, PR={"hold": True})
    first = expect_decision(submit(driver, _pay(500, target="PQ")), "hold", "first attempt")
    ticket = expect_ticket(first, "the held intent")
    second = expect_decision(
        submit(driver, _pay(500, target="PQ"), session="tck-s2"), "hold", "second attempt"
    )
    expect(
        second.ticket == ticket,
        f"the duplicate hold queued a second item ({second.ticket!r}) instead of "
        f"collapsing into {ticket!r} (CS-031)",
    )
    distinct = expect_decision(
        submit(driver, _pay(500, target="PR"), session="tck-s3"), "hold",
        "a hold over a DIFFERENT target",
    )
    expect(
        distinct.ticket is not None and distinct.ticket != ticket,
        "a hold over a different target collapsed into an unrelated queue item "
        "(CS-040: distinct questions never collapse)",
    )
    holds = [r for r in driver.audit() if r.decision == "hold"]
    expect(len(holds) >= 3,
           "a deduped attempt went unaudited (CS-031: every attempt writes a record)")
    driver.dispatch_once()
    expect(len(driver.effects()) == 0, "a deduped hold dispatched an effect")


@check(
    "J7",
    "a hold with no resolvable release contract refuses fail-closed, never stages",
    PROFILE_HOLD,
    requires=[CAP_HOLD, CAP_AUDIT],
)
def j7_unresolvable_hold_refuses(driver: ConformanceDriver) -> None:
    # CS-027: the gate names no ``resolvers:`` and the REQUIRED TCK config has
    # no deployment default resolver role (docs/12 §2) — so this hold's release
    # contract is unsatisfiable. Staging it would park the intent where no one
    # could ever release it; the gateway must refuse at decision time instead.
    setup(driver, policy=POLICY_HOLD_NO_RESOLVER, seed_world=False)
    _seed_targets(driver, PQ={"hold": True})
    result = expect_decision(submit(driver, _pay(500, target="PQ")), "deny",
                             "a hold with no resolvable contract (CS-027)")
    expect(result.ticket is None,
           "the unresolvable hold was staged (it must be refused, never parked)")
    reasons = [r.reason for r in driver.audit()]
    expect(
        any(r == "hold-unresolvable" for r in reasons),
        f"the refusal lacks the normative 'hold-unresolvable' reason (CS-027), "
        f"got {reasons[-3:]}",
    )
