"""Guide 01 — Hello, gateway.

The smallest possible Stonefold program: declare what exists (a registry),
say what one agent may do (a policy), and put the gateway between them.

Run:  python guide/01_hello_gateway.py
"""

from __future__ import annotations

from stonefold_core import (
    Actor,
    Connectors,
    Decision,
    InMemoryAuditSink,
    RawCall,
    Session,
    enforce,
    load_policy,
    load_registry,
)
from stonefold_connectors import InMemoryConnector


def main() -> None:
    # ------------------------------------------------------------------ #
    # 1. The REGISTRY: the complete list of things that exist.            #
    #    An action the registry does not declare is not merely forbidden  #
    #    -- it is unsayable. This is the gateway's outermost wall.        #
    # ------------------------------------------------------------------ #
    registry = load_registry(
        {
            "connectors": ["in_memory"],
            "resources": {
                "Note": {
                    "connector": "in_memory",
                    "actions": {
                        "read": {"kind": "observe"},   # look at notes
                        "create": {"kind": "record"},  # write a note
                    },
                },
            },
        }
    )

    # The connector is the code that actually touches the system behind a
    # resource. For the guide, an in-memory table stands in for a database.
    world = InMemoryConnector({"Note": [{"id": "N1", "text": "hello"}]})
    connectors = Connectors({"in_memory": world})

    # Every decision -- allowed or refused -- writes an audit record here.
    audit = InMemoryAuditSink()

    actor = Actor(id="alice")       # WHO is acting: from your transport/auth,
    session = Session(id="s1")      # never from the agent's own payload.

    # ------------------------------------------------------------------ #
    # 2. No policy loaded yet. The gateway's answer is built in:          #
    #    DEFAULT DENY. Nothing is allowed until a policy allows it.       #
    # ------------------------------------------------------------------ #
    result = enforce(
        RawCall(resource="Note", action="read"),
        actor, session,
        registry=registry, audit=audit, connectors=connectors,
    )
    assert result.decision is Decision.DENY
    assert result.rule == "default-deny"
    print(f"no policy      -> {result.decision.value:5s}  (rule: {result.rule})")

    # ------------------------------------------------------------------ #
    # 3. The POLICY: the simplest one possible. One agent, one permission.#
    #    Everything not listed stays denied.                              #
    # ------------------------------------------------------------------ #
    policy = load_policy(
        {
            "agent": "hello-agent",
            "allow": [
                {"observe": ["Note"]},  # may READ notes -- and nothing else
            ],
        },
        registry,
    )

    result = enforce(
        RawCall(resource="Note", action="read"),
        actor, session,
        registry=registry, audit=audit, policy=policy, connectors=connectors,
    )
    assert result.decision is Decision.ALLOW
    assert result.output == [{"id": "N1", "text": "hello"}]
    print(f"allowed read   -> {result.decision.value:5s}  rows: {result.output}")

    # Writing a note is a different permission ('record'); still denied.
    result = enforce(
        RawCall(resource="Note", action="create", data={"text": "world"}),
        actor, session,
        registry=registry, audit=audit, policy=policy, connectors=connectors,
    )
    assert result.decision is Decision.DENY
    print(f"unallowed write-> {result.decision.value:5s}  (rule: {result.rule})")

    # A name the registry never declared is refused before policy even runs.
    result = enforce(
        RawCall(resource="Database", action="dropAllTables"),
        actor, session,
        registry=registry, audit=audit, policy=policy, connectors=connectors,
    )
    assert result.decision is Decision.DENY
    assert result.rule == "unknown-action"
    print(f"unknown action -> {result.decision.value:5s}  (rule: {result.rule})")

    # ------------------------------------------------------------------ #
    # 4. Everything above -- including every refusal -- is on the record. #
    # ------------------------------------------------------------------ #
    assert len(audit.records) == 4
    print(f"\naudit log: {len(audit.records)} records "
          f"({sum(1 for r in audit.records if r.decision is Decision.DENY)} denies, "
          f"{sum(1 for r in audit.records if r.decision is Decision.ALLOW)} allow)")
    print("ok: 01_hello_gateway")


if __name__ == "__main__":
    main()
