"""Shared setup for TCK checks: actors, clock, default world seeding."""

from __future__ import annotations

from datetime import datetime, timezone

from stonefold_tck.checks import ConformanceFailure, expect
from stonefold_tck.driver import CAP_CLOCK, ConformanceDriver, Operation, SubmitResult, TckActor
from stonefold_tck.fixtures import TCK_POLICY, TCK_REGISTRY

T0 = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)

ALICE = TckActor(id="alice", claims={"tenant": "t1"})
BOB = TckActor(id="bob", claims={"tenant": "t2"})
SESSION = "tck-s1"


def setup(
    driver: ConformanceDriver,
    *,
    policy: str = TCK_POLICY,
    registry: str = TCK_REGISTRY,
    seed_world: bool = True,
) -> None:
    """Load fixtures (must succeed), pin the clock, seed the default world."""
    result = driver.load(registry, policy)
    expect(result.ok, f"fixture registry/policy failed to load: {list(result.errors)}")
    if CAP_CLOCK in driver.capabilities():
        driver.set_clock(T0)
    if seed_world:
        driver.seed(
            "Widget",
            [{"id": f"W{i}", "owner_id": "alice", "name": f"widget {i}"} for i in (1, 2, 3)]
            + [{"id": f"W{i}", "owner_id": "bob", "name": f"widget {i}"} for i in range(4, 11)],
        )
        driver.seed("Account", [{"id": "A1", "tenant": "t1", "name": "ours"},
                                {"id": "A2", "tenant": "t2", "name": "theirs"}])
        driver.seed("Order", [{"id": "O1", "currentState": "pending"},
                              {"id": "O2", "currentState": "confirmed"}])
        driver.seed("Payment", [{"id": "P1", "tenant": "t1"}, {"id": "P2", "tenant": "t2"}])
        driver.seed("Sealed", [{"id": "S1", "secret": "the-secret"}])
        driver.seed(
            "Med",
            [
                {"id": "M1", "patientId": "PAT1", "flag": True},
                {"id": "M2", "patientId": "PAT1", "flag": True},
                {"id": "M3", "patientId": "PAT2", "flag": True},
                {"id": "M4", "patientId": "PAT3", "flag": False},
            ],
        )


def submit(
    driver: ConformanceDriver,
    op: Operation,
    *,
    actor: TckActor = ALICE,
    session: str = SESSION,
) -> SubmitResult:
    return driver.submit(actor, session, op)


def expect_decision(result: SubmitResult, expected: str, what: str) -> SubmitResult:
    expect(
        result.decision == expected,
        f"{what}: expected decision {expected!r}, got {result.decision!r}"
        + (f" ({result.reason})" if result.reason else ""),
    )
    return result


def expect_ticket(result: SubmitResult, what: str) -> str:
    if result.ticket is None:
        raise ConformanceFailure(f"{what}: expected a ticket for the staged/held action")
    return result.ticket


def pay(amount: float, *, payee: str = "PY1", country: str = "SK", target: str = "P1") -> Operation:
    return Operation(
        resource="Payment",
        action="pay",
        data={"amount": amount, "destinationCountry": country, "payeeId": payee},
        target=target,
    )


def email(domain: str = "good.example", body: str = "hello") -> Operation:
    return Operation(
        resource="Email",
        action="sendEmail",
        data={"to": f"x@{domain}", "recipientDomain": domain, "body": body},
    )
