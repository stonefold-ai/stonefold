"""Guide 03 — Registered functions: the code YOU write.

Gates like ``valueLimit`` are built in. Three extension points carry your
domain knowledge, each a small deterministic function you register with the
gateway; the registry declares their NAMES so the linter can hold policies to
them:

  * scope predicate     -- which ROWS an actor may touch (``ownedBy``)
  * content hook        -- pass/block over a payload (``no.secrets``)
  * precondition check  -- a fact about the world that must hold; may also
                           resolve HOLD for judgment-shaped ambiguity (v0.6)

Run:  python guide/03_registered_functions.py
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stonefold_core import (
    Actor,
    Connectors,
    Decision,
    InMemoryAuditSink,
    RawCall,
    RequestEnv,
    RetryClass,
    Session,
    enforce,
    load_policy,
    load_registry,
)
from stonefold_core.scope import AttributeScope, ScopeRegistry, make_scope_resolver
from stonefold_connectors import InMemoryConnector
from stonefold_gates.base import CheckResult, GateContext, check_hold
from stonefold_gates.content import ContentHookRegistry
from stonefold_gates.engine import DefaultGateEngine


# ---------------------------------------------------------------------- #
# The three functions. Rules of the house: deterministic, no model calls, #
# fail by RETURNING (a raised exception means "my dependency is down" and #
# the gateway fails closed on your behalf).                               #
# ---------------------------------------------------------------------- #
def no_secrets(content: Mapping[str, Any]) -> bool:
    """Content hook: True = clean, False = block. The gateway calls it for
    every action gated with ``contentCheck: no.secrets``."""
    return "SECRET" not in str(dict(content)).upper()


def target_is_active(ctx: GateContext) -> bool:
    """Precondition check, two-valued form: pass iff the resolved target's
    ``status`` is active. ``ctx.env.resource`` holds the target's fields --
    resolved by the GATEWAY, never taken from the agent's payload."""
    return ctx.env.resource.get("status") == "active"


def inventory_available(ctx: GateContext) -> "bool | CheckResult":
    """Precondition check, three-valued form (v0.6): a readable-but-ambiguous
    answer HOLDS for a human instead of a bare deny. A hold MUST carry a
    machine-readable reason code, declared in the registry below."""
    stock = ctx.env.resource.get("stock")
    if stock == "unknown":
        return check_hold("stock-uncertain", evidence={"target": ctx.env.resource.get("id")})
    return bool(stock and int(stock) > 0)


def main() -> None:
    # The registry DECLARES the names; the linter refuses any policy that
    # references an undeclared one. The object form on inventoryAvailable
    # declares its hold capability and the retry class of its codes (CS-029).
    registry = load_registry(
        {
            "connectors": ["in_memory"],
            "scopePredicates": ["ownedBy"],
            "contentHooks": ["no.secrets"],
            "preconditionChecks": [
                "targetIsActive",
                {
                    "name": "inventoryAvailable",
                    "holdCapable": True,
                    "reasonCodes": {"stock-uncertain": "escalate"},
                },
            ],
            "resources": {
                "Note": {
                    "connector": "in_memory",
                    "actions": {
                        "read": {"kind": "observe"},
                        "create": {"kind": "record"},
                    },
                },
                "Order": {
                    "connector": "in_memory",
                    "actions": {"ship": {"kind": "effect"}},
                },
            },
        }
    )

    policy = load_policy(
        {
            "agent": "ops-agent",
            "allow": [{"observe": ["Note"]}, {"record": ["Note"]}, {"effect": ["ship"]}],
            "scope": {"Note": "ownedBy(actor)"},
            "gates": {
                "Note.create": {"contentCheck": "no.secrets"},
                "ship": {
                    "precondition": {
                        "checks": ["targetIsActive", "inventoryAvailable"],
                        # who may release a hold this gate raises (v0.6 CS-027)
                        "resolvers": "role:warehouse-lead",
                    }
                },
            },
        },
        registry,
    )

    # REGISTRATION: hooks + checks live on the gate engine; scope predicates
    # bind through the scope resolver. Names must match the registry.
    engine = DefaultGateEngine(
        registry,
        hooks=ContentHookRegistry({"no.secrets": no_secrets}),
        preconditions={
            "targetIsActive": target_is_active,
            "inventoryAvailable": inventory_available,
        },
    )
    scopes = make_scope_resolver(
        policy,
        ScopeRegistry({"ownedBy": AttributeScope("ownedBy", "owner_id", "id")}),
    )

    world = InMemoryConnector(
        {
            "Note": [
                {"id": "N1", "owner_id": "alice", "text": "mine"},
                {"id": "N2", "owner_id": "bob", "text": "not mine"},
            ],
            "Order": [
                {"id": "O1", "status": "active", "stock": 5},
                {"id": "O2", "status": "cancelled", "stock": 5},
                {"id": "O3", "status": "active", "stock": "unknown"},
            ],
        }
    )
    connectors = Connectors({"in_memory": world})
    audit = InMemoryAuditSink()
    alice, session = Actor(id="alice"), Session(id="s1")

    def ship(order_id: str) -> Any:
        # The gateway needs the TARGET's facts to evaluate target-based checks.
        # In the HTTP gateway an env_factory resolves them per request (see the
        # README); here we do the same inline.
        row = next(r for r in world.tables["Order"] if r["id"] == order_id)
        return enforce(
            RawCall(resource="Order", action="ship", data={"id": order_id}),
            alice, session,
            registry=registry, audit=audit, policy=policy, gates=engine,
            scopes=scopes, connectors=connectors, env=RequestEnv(resource=row),
        )

    # 1. SCOPE: alice reads Note and sees only her own row -- the filter is
    #    injected below the model; the agent cannot widen it.
    read = enforce(
        RawCall(resource="Note", action="read"),
        alice, session,
        registry=registry, audit=audit, policy=policy, gates=engine,
        scopes=scopes, connectors=connectors, env=RequestEnv(),
    )
    assert read.output == [{"id": "N1", "owner_id": "alice", "text": "mine"}]
    print(f"scope          -> alice sees {len(read.output)} of 2 notes")

    # 2. CONTENT HOOK: a payload with a secret in it is blocked.
    blocked = enforce(
        RawCall(resource="Note", action="create", data={"text": "the SECRET plan"}),
        alice, session,
        registry=registry, audit=audit, policy=policy, gates=engine,
        scopes=scopes, connectors=connectors, env=RequestEnv(),
    )
    assert blocked.decision is Decision.DENY and blocked.rule == "gate:contentCheck"
    print(f"content hook   -> {blocked.decision.value} (rule: {blocked.rule})")

    # 3. PRECONDITION, the three verdicts:
    ok = ship("O1")            # active + stocked        -> pass
    assert ok.decision is Decision.ALLOW
    print(f"check pass     -> {ok.decision.value} (O1 active, stock 5)")

    refused = ship("O2")       # cancelled               -> fail, closed
    assert refused.decision is Decision.DENY and refused.rule == "gate:precondition"
    print(f"check fail     -> {refused.decision.value} (O2 is cancelled)")

    held = ship("O3")          # stock unreadable        -> HOLD for a human
    assert held.decision is Decision.HOLD
    assert held.reason_code == "stock-uncertain"       # the code you declared
    assert held.retry_class is RetryClass.ESCALATE     # ...and its class
    print(f"check hold     -> {held.decision.value} "
          f"(code: {held.reason_code}, class: {held.retry_class.value})")

    print("ok: 03_registered_functions")


if __name__ == "__main__":
    main()
