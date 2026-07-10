"""THE GATEWAY SERVICE — owned by the platform/infra engineer.

Same shape as example 02, plus the two things a real deployment adds here:

  1. REGISTERING the function developer's implementations (functions.py)
     under the registry-declared names — checks and hooks on the gate
     engine, scope predicates on the scope resolver.
  2. The ENV FACTORY: for target-based checks the gateway resolves the
     target's current facts from the system of record, per request — the
     agent's payload only ever says WHICH target.

Run:  uvicorn gateway_service:app --port 8099   (from this directory)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI

from stonefold_core import (
    Connectors,
    InMemoryAuditSink,
    RawCall,
    RequestEnv,
    load_policy,
    load_registry,
)
from stonefold_core.scope import AttributeScope, ScopeRegistry, make_scope_resolver
from stonefold_connectors import InMemoryConnector
from stonefold_gates.content import ContentHookRegistry
from stonefold_gates.engine import DefaultGateEngine
from stonefold_gateway.main import create_app
from stonefold_gateway.transport import Gateway
from stonefold_store import InMemoryOutboxStore

import functions  # the function developer's file

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def _load_yaml(name: str) -> dict[str, Any]:
    with (HERE / name).open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def _audit_store() -> Any:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("gateway: DATABASE_URL not set -> in-memory audit (dev mode)")
        return InMemoryAuditSink()
    import psycopg
    from stonefold_store.audit_pg import PostgresAuditSink, create_audit_schema

    conn = psycopg.connect(dsn, autocommit=True)
    create_audit_schema(conn)
    print("gateway: audit -> Postgres")
    return PostgresAuditSink(conn)


def build_app() -> FastAPI:
    registry = load_registry(_load_yaml("registry.yaml"))
    schema = json.loads(
        (REPO / "spec" / "schema" / "stele.schema.json").read_text(encoding="utf-8")
    )
    policy = load_policy(_load_yaml("policy.stele.yaml"), registry, schema=schema)

    # the system of record behind the resources (your DB in production):
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

    # 1. REGISTRATION — the declared names get their implementations:
    engine = DefaultGateEngine(
        registry,
        hooks=ContentHookRegistry(functions.HOOKS),
        preconditions=functions.CHECKS,
    )
    scopes = make_scope_resolver(
        policy,
        ScopeRegistry({"ownedBy": AttributeScope("ownedBy", "owner_id", "id")}),
    )

    # 2. THE ENV FACTORY — per request, resolve the TARGET's current facts
    #    from the system of record so target-based checks judge the world,
    #    not the agent's claims:
    def env_factory(raw: RawCall) -> RequestEnv:
        row: dict[str, Any] = {}
        target_id = raw.data.get("id")
        if target_id is not None:
            for candidate in world.tables.get(raw.resource, []):
                if str(candidate.get("id")) == str(target_id):
                    row = dict(candidate)
                    break
        return RequestEnv(resource=row)

    audit = _audit_store()
    outbox = InMemoryOutboxStore(audit=audit)
    gateway = Gateway(
        registry=registry,
        audit=audit,
        policy=policy,
        gates=engine,
        scopes=scopes,
        connectors=Connectors({"in_memory": world}),
        outbox=outbox,
        env_factory=env_factory,
    )
    return create_app(gateway, audit=audit, outbox=outbox)


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8099")))
