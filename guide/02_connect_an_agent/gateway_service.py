"""THE GATEWAY SERVICE — owned by the platform/infra engineer.

A real HTTP service around the one chokepoint. Run it exactly like any
FastAPI app:

    uvicorn gateway_service:app --port 8099
    (from this directory; or:  uvicorn --app-dir guide/02_connect_an_agent gateway_service:app)

Configuration comes from the environment, like any deployment:

    DATABASE_URL  postgres for the append-only audit log
                  (guide/docker-compose.yml provides one)
                  unset -> in-memory audit, with a printed notice

The agent developer never sees this file. They get a URL and one tool.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI

from stonefold_core import Connectors, InMemoryAuditSink, load_policy, load_registry
from stonefold_connectors import InMemoryConnector
from stonefold_gateway.main import create_app
from stonefold_gateway.transport import Gateway
from stonefold_store import InMemoryOutboxStore

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


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


def build_app() -> FastAPI:
    # 1. Load the two reviewed artifacts. A policy with lint errors refuses
    #    to load — the service will not start permissive.
    registry = load_registry(_load_yaml("registry.yaml"))
    schema = json.loads(
        (REPO / "spec" / "schema" / "stele.schema.json").read_text(encoding="utf-8")
    )
    policy = load_policy(_load_yaml("policy.stele.yaml"), registry, schema=schema)

    # 2. The connector: the adapter to the system behind each resource. Here
    #    an in-memory table stands in for the CRM database.
    world = InMemoryConnector(
        {"Customer": [{"id": "C1", "name": "Acme"}, {"id": "C2", "name": "Globex"}]}
    )

    # 3. The chokepoint + the HTTP app around it. create_app exposes:
    #      GET  /tool-schema     — the ONE tool, generated from the registry
    #      POST /submit_intent   — identity from X-Actor-Id / X-Session-Id
    #                              headers (your transport's auth), never the body
    #      GET  /admin/trace/{correlationId} — the audit replay (operator surface)
    audit = _audit_store()
    outbox = InMemoryOutboxStore(audit=audit)  # effects arrive in example 04
    gateway = Gateway(
        registry=registry,
        audit=audit,
        policy=policy,
        connectors=Connectors({"in_memory": world}),
        outbox=outbox,
    )
    return create_app(gateway, audit=audit, outbox=outbox)


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8099")))
