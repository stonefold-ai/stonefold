"""Guide 04 — The full machine: staged effects, approvals, kill, audit.

Effects never fire inline. An allowed effect is STAGED in the outbox and a
dispatch worker sends it -- which is what makes human approval and the kill
switch possible at all: there is a durable moment between "decided" and
"done" where a person can still say no.

Run:  python guide/04_the_full_machine.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from stonefold_core import (
    Actor,
    Connectors,
    Decision,
    FreshnessConfig,
    InMemoryAuditSink,
    KillScope,
    RawCall,
    RequestEnv,
    Session,
    enforce,
    load_policy,
    load_registry,
)
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine, make_dispatch_revalidator
from stonefold_store import DispatchWorker, InMemoryKillStore, InMemoryOutboxStore

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


def main() -> None:
    registry = load_registry(
        {
            "connectors": ["in_memory"],
            "resources": {
                "Payment": {
                    "connector": "in_memory",
                    "actions": {"pay": {"kind": "effect"}},
                },
            },
        }
    )
    policy = load_policy(
        {
            "agent": "payments-agent",
            "allow": [{"effect": ["pay"]}],
            "gates": {
                "pay": {
                    "valueLimit": {"field": "data.amount", "max": 50000},
                    "requireApproval": {
                        "when": "data.amount > 1000",
                        "approvers": "role:payments-manager",
                    },
                },
            },
        },
        registry,
    )

    world = InMemoryConnector()
    connectors = Connectors({"in_memory": world})
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)    # settles write audit in-store
    kill = InMemoryKillStore()
    engine = DefaultGateEngine(registry)

    # The worker drains staged effects. Its clock is injected (so tests and
    # this guide are deterministic), and it re-validates the volatile gates
    # inside the claim -- a decision is only trusted for a bounded time.
    worker = DispatchWorker(
        outbox, connectors,
        registry=registry, kill=kill, clock=lambda: NOW,
        revalidate=make_dispatch_revalidator(engine, policy),
    )

    actor, session = Actor(id="alice"), Session(id="s1", correlation_id="run-1")

    def pay(amount: float, *, session_id: str = "s1") -> object:
        return enforce(
            RawCall(resource="Payment", action="pay", data={"amount": amount}),
            actor, Session(id=session_id, correlation_id="run-1"),
            registry=registry, audit=audit, policy=policy, gates=engine,
            connectors=connectors, outbox=outbox, kill=kill,
            env=RequestEnv(now=NOW),
            freshness=FreshnessConfig(),  # every staged row gets a decision TTL
        )

    # 1. A small payment: ALLOWED -- but only STAGED. Nothing has moved yet.
    small = pay(400)
    assert small.decision is Decision.ALLOW and small.ticket is not None
    assert world.effects == []
    print(f"pay 400   -> {small.decision.value} (staged, ticket {small.ticket[:12]}...)")

    # ...the worker dispatches it. Exactly once: retries dedupe on the
    # idempotency key every staged row carries.
    worker.drain()
    assert len(world.effects) == 1
    print(f"worker    -> dispatched; money moved ({len(world.effects)} effect)")

    # 2. A big payment: HELD. It sits in the outbox until a HUMAN releases it.
    big = pay(5000)
    assert big.decision is Decision.HOLD and big.ticket is not None
    worker.drain()
    assert len(world.effects) == 1  # still one -- a held row never dispatches
    print(f"pay 5000  -> {big.decision.value} (awaiting role:payments-manager)")

    outbox.approve(big.ticket, "manager-1")
    worker.drain()
    assert len(world.effects) == 2
    print("approve   -> released and dispatched")

    # ...and rejection means it NEVER moves:
    rejected = pay(9000)
    outbox.reject(rejected.ticket, "manager-1")
    worker.drain()
    assert len(world.effects) == 2
    print("reject    -> cancelled; nothing dispatched")

    # 3. The KILL SWITCH: flip it, and this session can do nothing more.
    kill_id = kill.issue(KillScope.for_session("s1"), issued_by="operator").id
    halted = pay(50)
    assert halted.decision is Decision.HALT
    print(f"kill      -> {halted.decision.value} (even a tiny payment)")
    kill.lift(kill_id)
    assert pay(50, session_id="s2").decision is Decision.ALLOW

    # 4. The AUDIT: one run, replayed as one ordered story -- every allow,
    #    hold, deny, halt, and settle, with its reason.
    story = audit.by_correlation("run-1")
    print(f"\naudit replay (run-1): {len(story)} records")
    for r in story[:6]:
        print(f"  {r.decision.value:5s} {r.action or '-':4s} rule={r.rule}")
    print("ok: 04_the_full_machine")


if __name__ == "__main__":
    main()
