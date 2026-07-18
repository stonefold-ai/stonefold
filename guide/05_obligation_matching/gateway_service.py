"""THE GATEWAY SERVICE — owned by the platform/infra engineer.

Example 04's full machine, plus the v0.6 obligation wiring. One adapter
instance (the function developer's erp_adapter.py) is handed to THREE
consumers, because the obligation's lifecycle spans all three:

    - the GATE ENGINE queries it at decision time ("is this owed?")
    - the PIPELINE reserves the matched record inside the staging commit
      (so two staged payments can never claim one order line)
    - the WORKER liveness-checks the reservation at dispatch, consumes it
      with the settle, and releases it on any cancellation

Run:  uvicorn gateway_service:app --port 8099   (from this directory)
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI

from stonefold_core import (
    Connectors,
    FreshnessConfig,
    InMemoryAuditSink,
    RawCall,
    RequestEnv,
    load_policy,
    load_registry,
)
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine, make_dispatch_revalidator
from stonefold_gateway.main import create_app
from stonefold_gateway.transport import Gateway
from stonefold_store import DispatchWorker, InMemoryOutboxStore

import erp_adapter  # the function developer's file: the door to "the ERP"

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

if not (REPO / "spec" / "schema").exists():  # a plain clone leaves the submodule empty
    raise SystemExit("spec/ submodule is empty — run: git submodule update --init")


def _load_yaml(name: str) -> dict[str, Any]:
    # The two YAML artifacts live NEXT TO this service on disk in the guide;
    # in production they come from a reviewed config repo / mount.
    with (HERE / name).open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def _audit_store() -> Any:
    # Same env-driven pattern as example 04 (see its _stores() for the full
    # Postgres/Redis wiring); kept to the audit store here so this file can
    # focus on what is NEW: the obligation plumbing.
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("gateway: DATABASE_URL not set -> in-memory audit (dev mode)")
        return InMemoryAuditSink()
    import psycopg
    from stonefold_store.audit_pg import PostgresAuditSink, create_audit_schema

    conn = psycopg.connect(dsn, autocommit=True)
    create_audit_schema(conn)  # idempotent: safe on every boot
    print("gateway: audit -> Postgres")
    return PostgresAuditSink(conn)


def build_app() -> FastAPI:
    # -- the reviewed artifacts -------------------------------------------
    # registry.yaml now DECLARES the obligation registry (name, adapter
    # connector, typed match surface); policy.stele.yaml carries the
    # requireMatch rule. The linter cross-checks them at load: an
    # obligation.* path the registry does not declare refuses to load.
    registry = load_registry(_load_yaml("registry.yaml"))
    schema = json.loads(
        (REPO / "spec" / "schema" / "stele.schema.json").read_text(encoding="utf-8")
    )
    policy = load_policy(_load_yaml("policy.stele.yaml"), registry, schema=schema)

    # -- the obligation adapter -------------------------------------------
    # ONE instance, shared by engine + pipeline + worker (see module
    # docstring). The dict is keyed by the DECLARED registry name, so the
    # policy's `registry: erp.purchase_orders` resolves to this adapter.
    adapters = {"erp.purchase_orders": erp_adapter.build_adapter()}

    audit = _audit_store()
    outbox = InMemoryOutboxStore(audit=audit)
    world = InMemoryConnector()  # the "payment rail" the effect leaves through

    # consumer 1: the gate engine — decision-time matching (query only).
    engine = DefaultGateEngine(registry, obligations=adapters)

    def env_factory(raw: RawCall) -> RequestEnv:
        # a live service runs on the wall clock; the injected clock is what
        # makes freshness TTLs and time-based gates testable AND real.
        return RequestEnv(now=datetime.now(timezone.utc))

    # consumer 2: the Gateway/pipeline — reserves the matched record INSIDE
    # the staging commit (CS-035), and collapses duplicate holds within the
    # dedupe window (CS-031/CS-040) so a retrying agent cannot spam the
    # clerk's queue with the same question.
    gateway = Gateway(
        registry=registry,
        audit=audit,
        policy=policy,
        gates=engine,
        connectors=Connectors({"in_memory": world}),
        outbox=outbox,
        env_factory=env_factory,
        freshness=FreshnessConfig(),
        obligations=adapters,
        dedupe_window_s=3600.0,
    )

    # consumer 3: the worker — reservation liveness at the dispatch claim,
    # consume with the settle (the receipt lands in the audit record),
    # release on any terminal non-success.
    worker = DispatchWorker(
        outbox,
        Connectors({"in_memory": world}),
        registry=registry,
        revalidate=make_dispatch_revalidator(engine, policy),
        obligations=adapters,
    )

    def run_worker() -> None:
        # the same background-drain loop as example 04; in production this
        # is typically its own process over its own DB connection.
        while True:
            try:
                busy = worker.run_once()
            except Exception:
                busy = False
            if not busy:
                time.sleep(0.2)

    threading.Thread(target=run_worker, name="dispatch-worker", daemon=True).start()

    return create_app(gateway, audit=audit, outbox=outbox)


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8099")))
