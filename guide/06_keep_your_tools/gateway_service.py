"""THE GATEWAY SERVICE — owned by the platform/infra engineer.

The same service as example 02, plus ONE addition: the interception proxy.
It exposes the agent's old tool surface (``POST /tools/{name}``) and, per
call, translates the tool name and argument keys through the reviewed
``mappings.yaml`` into a declared action — then runs the exact same
pipeline every other example uses. A tool with no mapping entry is denied
and audited, never passed through.

    uvicorn gateway_service:app --port 8099

The agent developer never sees this file; their agent keeps its old tools.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Header

from stonefold_core import Actor, Connectors, InMemoryAuditSink, Session, load_policy, load_registry
from stonefold_connectors import InMemoryConnector
from stonefold_gateway.main import create_app
from stonefold_gateway.transport import Gateway, MCPProxy, ToolMapping
from stonefold_store import InMemoryOutboxStore

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

if not (REPO / "spec" / "schema").exists():  # a plain clone leaves the submodule empty
    raise SystemExit("spec/ submodule is empty — run: git submodule update --init")


def _load_yaml(name: str) -> dict[str, Any]:
    with (HERE / name).open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def _audit_store() -> Any:
    """Durable when the deployment provides Postgres; in-memory otherwise."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("gateway: DATABASE_URL not set -> in-memory audit (dev mode)")
        return InMemoryAuditSink()
    import psycopg
    from stonefold_store.audit_pg import PostgresAuditSink, create_audit_schema

    conn = psycopg.connect(dsn, autocommit=True)
    create_audit_schema(conn)  # idempotent
    print("gateway: audit -> Postgres")
    return PostgresAuditSink(conn)


def _load_mappings() -> list[ToolMapping]:
    """mappings.yaml -> ToolMapping objects. The reviewed table, verbatim:
    the proxy will do a lookup in it per call — nothing is inferred."""
    entries = _load_yaml("mappings.yaml")["mappings"]
    return [
        ToolMapping(
            tool=e["tool"],
            resource=e["resource"],
            action=e["action"],
            arg_map=e.get("argMap", {}),
        )
        for e in entries
    ]


def build_app() -> FastAPI:
    # 1. The same reviewed artifacts as always (see example 02).
    registry = load_registry(_load_yaml("registry.yaml"))
    schema = json.loads(
        (REPO / "spec" / "schema" / "stele.schema.json").read_text(encoding="utf-8")
    )
    policy = load_policy(_load_yaml("policy.stele.yaml"), registry, schema=schema)

    world = InMemoryConnector(
        {"Customer": [{"id": "C1", "name": "Acme"}, {"id": "C2", "name": "Globex"}]}
    )

    audit = _audit_store()
    outbox = InMemoryOutboxStore(audit=audit)
    gateway = Gateway(
        registry=registry,
        audit=audit,
        policy=policy,
        connectors=Connectors({"in_memory": world}),
        outbox=outbox,
    )
    app = create_app(gateway, audit=audit, outbox=outbox)

    # 2. THE ADDITION — the interception proxy in front of the same gateway.
    #    (A free_form mapping — a raw-string tool like run_sql mapped as a
    #    pass-through — would make MCPProxy refuse to start unless explicitly
    #    acknowledged; here run_sql simply has no entry, so it is denied.)
    proxy = MCPProxy(gateway, _load_mappings())

    @app.post("/tools/{tool}")
    def call_tool(
        tool: str,
        args: dict[str, Any],
        x_actor_id: str = Header(..., alias="X-Actor-Id"),
        x_session_id: str = Header(..., alias="X-Session-Id"),
    ) -> dict[str, Any]:
        # The agent's old surface: tool name in the path, its old argument
        # names in the body. Identity from the transport headers, as ever.
        result = proxy.call_tool(
            tool, args,
            actor=Actor(id=x_actor_id), session=Session(id=x_session_id),
        )
        # same wire shape as /submit_intent renders
        return {
            "decision": result.decision.value,
            "rule": result.rule,
            "ticket": result.ticket,
            "output": result.output,
            "reasonCode": result.reason_code,
            "retryClass": result.retry_class.value if result.retry_class else None,
        }

    return app


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8099")))
