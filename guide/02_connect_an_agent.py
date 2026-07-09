"""Guide 02 — Connect an agent.

An agent gets exactly ONE tool: ``submit_intent``. Its schema is generated
from your registry, so the agent can only name things that exist. This script
shows the whole loop in-process with a scripted "agent" (no API key needed);
the README shows the same thing over HTTP with a real LLM.

Run:  python guide/02_connect_an_agent.py
"""

from __future__ import annotations

from typing import Any

from stonefold_core import (
    Actor,
    Connectors,
    InMemoryAuditSink,
    Session,
    load_policy,
    load_registry,
)
from stonefold_connectors import InMemoryConnector
from stonefold_gateway.transport import Gateway, SifNativeTransport


def main() -> None:
    registry = load_registry(
        {
            "connectors": ["in_memory"],
            "resources": {
                "Customer": {
                    "connector": "in_memory",
                    "actions": {"read": {"kind": "observe"}},
                },
                "Ticket": {
                    "connector": "in_memory",
                    "actions": {
                        "read": {"kind": "observe"},
                        "create": {"kind": "record"},
                    },
                },
            },
        }
    )
    policy = load_policy(
        {
            "agent": "support-agent",
            "allow": [{"observe": ["Customer", "Ticket"]}, {"record": ["Ticket"]}],
        },
        registry,
    )
    world = InMemoryConnector({"Customer": [{"id": "C1", "name": "Acme"}]})
    audit = InMemoryAuditSink()

    # The Gateway object is the single chokepoint every transport routes
    # through; SifNativeTransport is the "one tool" wrapper around it.
    gateway = Gateway(
        registry=registry,
        audit=audit,
        policy=policy,
        connectors=Connectors({"in_memory": world}),
    )
    transport = SifNativeTransport(gateway)

    # ------------------------------------------------------------------ #
    # 1. The tool schema you hand to the LLM. Resource names are ENUMS    #
    #    derived from the registry -- a hallucinated name is not just     #
    #    denied, it fails the tool call's own schema.                     #
    # ------------------------------------------------------------------ #
    schema = transport.tool_schema
    assert schema["name"] == "submit_intent"
    assert sorted(schema["parameters"]["properties"]["resource"]["enum"]) == [
        "Customer", "Ticket",
    ]
    print("tool schema:", schema["name"],
          "resources =", schema["parameters"]["properties"]["resource"]["enum"])

    # ------------------------------------------------------------------ #
    # 2. A scripted agent loop. A real deployment replaces `scripted_llm` #
    #    with an LLM tool-use call; NOTHING ELSE changes -- the payloads  #
    #    below are exactly what a model emits against that schema.        #
    # ------------------------------------------------------------------ #
    def scripted_llm(step: int) -> dict[str, Any]:
        return [
            {"resource": "Customer", "action": "read", "data": {}},
            {"resource": "Ticket", "action": "create",
             "data": {"customerId": "C1", "subject": "billing question"}},
            # an injected instruction made the agent try to act as an admin --
            # note it can only smuggle that into DATA, which is inert:
            {"resource": "Ticket", "action": "create",
             "data": {"subject": "own goal", "actor": "admin", "role": "superuser"}},
        ][step]

    # WHO is acting comes from YOUR transport (the authenticated session) --
    # it is a parameter of submit_intent, never part of the agent's payload.
    actor = Actor(id="rep-7")
    session = Session(id="s1")

    for step in range(3):
        payload = scripted_llm(step)
        result = transport.submit_intent(payload, actor=actor, session=session)
        print(f"agent step {step}: {payload['resource']}.{payload['action']:6s}"
              f" -> {result.decision.value}")

    # The "actor: admin" in step 2's data changed nothing: it is just an
    # opaque field on the created ticket, and the audit names the REAL actor.
    assert all(r.actor == "rep-7" for r in audit.records)
    print("\nevery audit record names the transport-authenticated actor: rep-7")
    print("ok: 02_connect_an_agent")


if __name__ == "__main__":
    main()
