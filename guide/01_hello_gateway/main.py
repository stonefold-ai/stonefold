"""THE DEMO DRIVER — stands in for the running gateway process.

In production the infra engineer wires exactly these objects inside a
service (example 02 shows that split); here one small file plays that role
so you can watch the four decisions that define the model.

Run:  python guide/01_hello_gateway/main.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

# The gateway's building blocks (the infra engineer's imports):
from stonefold_core import (
    Actor,               # WHO acts — from your transport/auth, never the agent
    Connectors,          # the adapter registry the gateway executes through
    Decision,            # allow | hold | deny | halt
    InMemoryAuditSink,   # every decision lands here (Postgres in production)
    RawCall,             # one attempted action, before any validation
    Session,             # the ambient session (correlation, kill matching)
    enforce,             # THE pipeline: one call per attempt, always audited
    load_policy,         # YAML -> validated, linted, compiled policy
    load_registry,       # YAML -> the indexed catalogue
)
from stonefold_connectors import InMemoryConnector  # a table standing in for your DB

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]  # .../stonefold

if not (REPO / "spec" / "schema").exists():  # a plain clone leaves the submodule empty
    raise SystemExit("spec/ submodule is empty — run: git submodule update --init")


def load_yaml(name: str) -> dict[str, Any]:
    with (HERE / name).open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def main() -> None:
    # -- startup: what a gateway service does once, at boot ---------------
    registry = load_registry(load_yaml("registry.yaml"))
    schema = json.loads(
        (REPO / "spec" / "schema" / "stele.schema.json").read_text(encoding="utf-8")
    )
    # load_policy validates against the JSON Schema AND runs the semantic
    # linter; a policy with errors refuses to load — the gateway would
    # rather not start than start permissive.
    policy = load_policy(load_yaml("policy.stele.yaml"), registry, schema=schema)

    world = InMemoryConnector({"Note": [{"id": "N1", "text": "hello"}]})
    connectors = Connectors({"in_memory": world})
    audit = InMemoryAuditSink()

    actor = Actor(id="alice")   # supplied by YOUR transport, per request
    session = Session(id="s1")

    def submit(resource: str, action: str, data: dict[str, Any] | None = None) -> Any:
        return enforce(
            RawCall(resource=resource, action=action, data=data or {}),
            actor, session,
            registry=registry, audit=audit, policy=policy, connectors=connectors,
        )

    # -- the four decisions that define the model --------------------------
    allowed = submit("Note", "read")
    assert allowed.decision is Decision.ALLOW
    assert allowed.output == [{"id": "N1", "text": "hello"}]
    print(f"read (allowed by policy)   -> {allowed.decision.value:5s} rows={allowed.output}")

    unlisted = submit("Note", "create", {"text": "world"})
    assert unlisted.decision is Decision.DENY and unlisted.rule == "default-deny"
    print(f"create (not in the policy) -> {unlisted.decision.value:5s} rule={unlisted.rule}")

    unknown = submit("Database", "dropAllTables")
    assert unknown.decision is Decision.DENY and unknown.rule == "unknown-action"
    print(f"undeclared name            -> {unknown.decision.value:5s} rule={unknown.rule}")

    # every attempt above — including both refusals — is on the record:
    assert len(audit.records) == 3
    print(f"\naudit log: {len(audit.records)} records, refusals included")
    print("ok: 01_hello_gateway")


if __name__ == "__main__":
    main()
