"""Guide 05 — Obligation matching (v0.6): in bounds is not the same as owed.

Every gate so far compares the intent against constants in the policy. None
can catch the payment that is under every limit and corresponds to NOTHING --
no order was ever placed, or the invoice is already paid. v0.6's
``requireMatch`` closes that: the intent must match exactly one open record
in a system the agent cannot write to, the record is RESERVED when the action
stages and CONSUMED when it lands -- so one order line can never pay two
invoices -- and what the agent hears back is a machine-readable code with a
retry class, so an iterating agent knows fix-and-retry from give-up.

Run:  python guide/05_obligation_matching.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from stonefold_core import (
    Actor,
    Connectors,
    Decision,
    FreshnessConfig,
    InMemoryAuditSink,
    RawCall,
    RequestEnv,
    RetryClass,
    Session,
    enforce,
    load_policy,
    load_registry,
)
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine, make_dispatch_revalidator
from stonefold_store import (
    DispatchWorker,
    InMemoryObligationRegistry,
    InMemoryOutboxStore,
)

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


def main() -> None:
    # ------------------------------------------------------------------ #
    # 1. Declare the OBLIGATION REGISTRY: which system of record payments #
    #    must match against, and the typed fields the policy may compare  #
    #    (a match surface, not a domain model).                           #
    # ------------------------------------------------------------------ #
    registry = load_registry(
        {
            "connectors": ["in_memory", "erp-adapter"],
            "obligationRegistries": {
                "erp.purchase_orders": {
                    "connector": "erp-adapter",
                    "capability": "transactional",
                    "schema": {
                        "vendorId": {"type": "string"},
                        "state": {"values": ["open", "closed"]},
                        "line": {
                            "properties": {
                                "amount": {"type": "decimal"},
                                "state": {"values": ["unconsumed", "reserved", "consumed"]},
                            }
                        },
                    },
                },
            },
            "resources": {
                "Payment": {
                    "connector": "in_memory",
                    "actions": {"pay": {"kind": "effect"}},
                },
            },
        }
    )

    # ------------------------------------------------------------------ #
    # 2. The ADAPTER: your code over the real ERP/EMR, four idempotent    #
    #    operations (query / reserve / consume / release). The in-memory  #
    #    one ships with the reference; ``state_path`` makes reservations  #
    #    visible to the match itself.                                     #
    # ------------------------------------------------------------------ #
    erp = InMemoryObligationRegistry(
        {
            "PO-7001": {
                "vendorId": "ACME",
                "state": "open",
                "line": {"amount": 800.0, "state": "unconsumed"},
            },
        },
        state_path="line.state",
    )
    adapters = {"erp.purchase_orders": erp}

    # ------------------------------------------------------------------ #
    # 3. The POLICY: the match rule is IN THE FILE, where a reviewer and  #
    #    the linter can see it -- not buried in check code.               #
    # ------------------------------------------------------------------ #
    policy = load_policy(
        {
            "agent": "ap-agent",
            "allow": [{"effect": ["pay"]}],
            "gates": {
                "pay": {
                    "requireMatch": {
                        "registry": "erp.purchase_orders",
                        "match": [
                            "obligation.vendorId == data.vendorId",
                            "obligation.state == 'open'",
                            "obligation.line.state == 'unconsumed'",
                            {"field": "obligation.line.amount",
                             "matches": "data.amount", "within": "10%"},
                        ],
                        "consume": "obligation.line",
                        "onNoMatch": "deny",           # or "hold" -> a human queue
                        "onAmbiguous": "hold",         # several matches: NEVER pick
                        "resolvers": "role:ap-clerk",  # who releases such a hold
                    },
                },
            },
        },
        registry,
    )

    world = InMemoryConnector()
    connectors = Connectors({"in_memory": world})
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    engine = DefaultGateEngine(registry, obligations=adapters)
    worker = DispatchWorker(
        outbox, connectors, registry=registry, clock=lambda: NOW,
        revalidate=make_dispatch_revalidator(engine, policy),
        obligations=adapters,  # the worker consumes at settle, releases on cancel
    )
    actor = Actor(id="ap-bot")

    def pay(amount: float, vendor: str = "ACME", *, session: str = "s1") -> object:
        return enforce(
            RawCall(resource="Payment", action="pay",
                    data={"amount": amount, "vendorId": vendor}),
            actor, Session(id=session, correlation_id=session),
            registry=registry, audit=audit, policy=policy, gates=engine,
            connectors=connectors, outbox=outbox, env=RequestEnv(now=NOW),
            freshness=FreshnessConfig(),
            obligations=adapters,          # the pipeline reserves at staging
            dedupe_window_s=3600.0,        # duplicate holds collapse (CS-031)
        )

    # BEAT 1 -- the agent extracted the amount wrong. The refusal tells it
    # exactly what to do: outside-tolerance is RETRYABLE (fix and resubmit).
    wrong = pay(990.0)
    assert wrong.decision is Decision.DENY
    assert wrong.reason_code == "outside-tolerance"
    assert wrong.retry_class is RetryClass.RETRYABLE
    print(f"pay 990 (order says 800) -> {wrong.decision.value}"
          f"  code={wrong.reason_code} class={wrong.retry_class.value}")

    # BEAT 2 -- corrected: matches the open PO line, stages (line RESERVED),
    # dispatches, and the line is CONSUMED with the settle.
    good = pay(800.0)
    assert good.decision is Decision.ALLOW
    worker.drain()
    assert len(world.effects) == 1
    settle = audit.records[-1]
    assert settle.consumption is not None
    assert settle.consumption["state"] == "consumed"
    print(f"pay 800 (matches PO-7001) -> {good.decision.value}; dispatched;"
          f" line consumed (receipt {settle.consumption['receipt'][:12]}...)")

    # BEAT 3 -- the SAME invoice again. The line is spent, so it matches
    # nothing. This is the refusal no earlier version could produce -- and
    # the class tells the agent there is nothing to fix: stop resubmitting.
    again = pay(800.0, session="s2")
    assert again.decision is Decision.DENY
    assert again.reason_code == "no-match"
    assert again.retry_class is RetryClass.TERMINAL
    print(f"same invoice resubmitted -> {again.decision.value}"
          f"  code={again.reason_code} class={again.retry_class.value}")

    # BEAT 4 -- AMBIGUITY holds for a human, and never picks. Two open ACME
    # lines could both take this payment; the gateway refuses to choose --
    # the question goes to role:ap-clerk with both candidates named.
    erp.add("PO-7002", {"vendorId": "ACME", "state": "open",
                        "line": {"amount": 800.0, "state": "unconsumed"}})
    erp.add("PO-7003", {"vendorId": "ACME", "state": "open",
                        "line": {"amount": 800.0, "state": "unconsumed"}})
    which = pay(800.0, session="s3")
    assert which.decision is Decision.HOLD
    assert which.reason_code == "ambiguous-match"
    assert which.retry_class is None  # a gateway hold means: wait for a human
    print(f"two lines could match    -> {which.decision.value}"
          f"  code={which.reason_code} (queued for role:ap-clerk)")

    # ...and the same question asked twice collapses into ONE queue item
    # with an attempt count (CS-031) -- holds spend human attention.
    which2 = pay(800.0, session="s4")
    assert which2.ticket == which.ticket
    row = outbox.get(which.ticket)
    assert row is not None and row.attempts == 2
    print(f"asked twice              -> same ticket, attempts={row.attempts}")

    # BEAT 5 -- a fraudulent invoice from a vendor with no order at all:
    # under every limit, matching nothing. Money never at risk.
    fraud = pay(4500.0, vendor="QUICKPAY", session="s5")
    assert fraud.decision is Decision.DENY and fraud.reason_code == "no-match"
    worker.drain()
    assert len(world.effects) == 1  # still exactly one payment ever left
    print(f"no order exists          -> {fraud.decision.value}"
          f"  code={fraud.reason_code}; total payments sent: 1")

    print("ok: 05_obligation_matching")


if __name__ == "__main__":
    main()
