"""THE GATEWAY SERVICE — owned by the platform/infra engineer.

The full machine, wired the way a deployment wires it:

  DATABASE_URL -> Postgres: the durable outbox (staged effects, claimed with
                  SELECT ... FOR UPDATE), the append-only audit_log, and the
                  kill orders. guide/docker-compose.yml provides one.
  REDIS_URL    -> Redis: sliding-window rate counters.
  (unset       -> in-memory equivalents, printed notice; fine on a laptop.)

Plus the one piece no earlier example had: a background DISPATCH WORKER that
drains staged effects — over its OWN database connection, exactly as a
separate worker process would.

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
from stonefold_gateway.kill_service import KillService
from stonefold_gateway.main import create_app
from stonefold_gateway.transport import Gateway
from stonefold_store import (
    DispatchWorker,
    InFlightRegistry,
    InMemoryCounterStore,
    InMemoryKillStore,
    InMemoryOutboxStore,
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

if not (REPO / "spec" / "schema").exists():  # a plain clone leaves the submodule empty
    raise SystemExit("spec/ submodule is empty — run: git submodule update --init")


def _load_yaml(name: str) -> dict[str, Any]:
    with (HERE / name).open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def _stores() -> tuple[Any, Any, Any, Any, Any]:
    """(audit, outbox, worker_outbox, kill, counters) from the environment.

    The worker gets its OWN outbox handle: with Postgres that is a second
    connection (psycopg connections are not shared across threads), which is
    exactly how a separate worker process would connect.
    """
    dsn = os.environ.get("DATABASE_URL")
    redis_url = os.environ.get("REDIS_URL")

    counters: Any = InMemoryCounterStore()
    if redis_url:
        import redis
        from stonefold_store.redis_store import RedisCounterStore

        counters = RedisCounterStore(redis.Redis.from_url(redis_url))
        print("gateway: counters -> Redis")
    else:
        print("gateway: REDIS_URL not set -> in-memory counters (dev mode)")

    if not dsn:
        print("gateway: DATABASE_URL not set -> in-memory outbox/audit/kill (dev mode)")
        audit = InMemoryAuditSink()
        outbox = InMemoryOutboxStore(audit=audit)
        return audit, outbox, outbox, InMemoryKillStore(), counters

    import psycopg
    from stonefold_store.audit_pg import PostgresAuditSink, create_audit_schema
    from stonefold_store.kill_pg import PostgresKillStore, create_kill_schema
    from stonefold_store.outbox_pg import PostgresOutboxStore, create_schema

    conn = psycopg.connect(dsn, autocommit=True)          # request path
    worker_conn = psycopg.connect(dsn, autocommit=True)   # the worker's own
    create_schema(conn)
    create_audit_schema(conn)
    create_kill_schema(conn)
    print("gateway: outbox/audit/kill -> Postgres (FOR UPDATE claims)")
    return (
        PostgresAuditSink(conn),
        PostgresOutboxStore(conn),
        PostgresOutboxStore(worker_conn),
        PostgresKillStore(conn),
        counters,
    )


def build_app() -> FastAPI:
    registry = load_registry(_load_yaml("registry.yaml"))
    schema = json.loads(
        (REPO / "spec" / "schema" / "stele.schema.json").read_text(encoding="utf-8")
    )
    policy = load_policy(_load_yaml("policy.stele.yaml"), registry, schema=schema)

    audit, outbox, worker_outbox, kill, counters = _stores()
    world = InMemoryConnector()  # the "payment rail"; your bank adapter in production
    connectors = Connectors({"in_memory": world})
    engine = DefaultGateEngine(registry, counters=counters)

    # time-based gates (rate) and decision TTLs read the injected clock —
    # in a live service that is the wall clock, per request:
    def env_factory(raw: RawCall) -> RequestEnv:
        return RequestEnv(now=datetime.now(timezone.utc))

    gateway = Gateway(
        registry=registry,
        audit=audit,
        policy=policy,
        gates=engine,
        connectors=connectors,
        outbox=outbox,
        kill=kill,
        env_factory=env_factory,
        freshness=FreshnessConfig(),  # every staged row gets a decision TTL
    )

    # THE DISPATCH WORKER — drains staged effects in the background. Inside
    # each claim it re-checks kill -> TTL -> volatile gates before sending;
    # the idempotency key on every row makes retries safe.
    worker = DispatchWorker(
        worker_outbox,
        connectors,
        registry=registry,
        kill=kill,
        revalidate=make_dispatch_revalidator(engine, policy),
    )

    def run_worker() -> None:
        while True:
            try:
                busy = worker.run_once()
            except Exception:
                busy = False
            if not busy:
                time.sleep(0.2)

    threading.Thread(target=run_worker, name="dispatch-worker", daemon=True).start()

    kill_service = KillService(kill, audit=audit, inflight=InFlightRegistry())
    return create_app(gateway, kill_service=kill_service, audit=audit, outbox=outbox)


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8099")))
