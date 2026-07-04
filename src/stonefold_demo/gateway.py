"""Assemble the full ACP enforcement stack over an example policy (M-DEMO).

This is the same wiring a real deployment uses: registry → compiled policy →
gate engine → scope resolver → connectors → outbox (+ optional kill store), all
behind the ``Gateway`` chokepoint. The demo points it at the shipped
``support-assistant`` policy so the thing under test is the *actual* product, not
a bespoke demo policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from stonefold_core import (
    CompiledPolicy,
    FreshnessConfig,
    InMemoryAuditSink,
    RequestEnv,
    assert_connector_digests,
    load_policy,
    load_registry,
)
from stonefold_core.scope import make_scope_resolver
from stonefold_gates.engine import DefaultGateEngine, make_dispatch_revalidator
from stonefold_gateway.transport import Gateway
from stonefold_store import DispatchWorker, InMemoryOutboxStore
from stonefold_store.kill_memory import InMemoryKillStore
from stonefold_demo.world import World

_ROOT = Path(__file__).resolve().parents[2]
_REGISTRY = _ROOT / "registry" / "stonefold-registry.yaml"
_SCHEMA = _ROOT / "schema" / "stele.schema.json"
SUPPORT_POLICY = _ROOT / "examples" / "support-assistant.stele.yaml"

# The gateway injects the clock that time-based gates (rate/window) read — never
# the agent. A *fixed* instant keeps the demo deterministic (invariant 1): a real
# deployment injects the wall clock here.
DEMO_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


@dataclass
class GatewayBundle:
    """Everything the demo needs to drive and inspect one gateway."""

    gateway: Gateway
    policy: CompiledPolicy
    audit: InMemoryAuditSink
    outbox: InMemoryOutboxStore
    kill: InMemoryKillStore
    worker: DispatchWorker

    def drain(self) -> int:
        """Dispatch every staged (allowed) effect — so a *permitted* email
        actually leaves, while refused ones never staged and so never can."""
        return self.worker.drain()


def build_gateway(world: World, *, policy_path: Path = SUPPORT_POLICY) -> GatewayBundle:
    registry = load_registry(_load_yaml(_REGISTRY))
    with _SCHEMA.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    policy = load_policy(_load_yaml(policy_path), registry, schema=schema)

    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    kill = InMemoryKillStore()
    connectors = world.connectors()
    # CS-020: verify any pinned connector digests before serving. A no-op unless
    # the registry declares digests; a mismatch fails closed here (refuses to come
    # up) under the policy's failureMode, audited.
    assert_connector_digests(
        registry, connectors,
        failure_mode=policy.policy.defaults.failureMode, audit=audit,
    )
    engine = DefaultGateEngine(registry)
    scopes = make_scope_resolver(policy)
    gateway = Gateway(
        registry=registry,
        audit=audit,
        policy=policy,
        gates=engine,
        scopes=scopes,
        connectors=connectors,
        outbox=outbox,
        kill=kill,
        env=RequestEnv(now=DEMO_NOW),
        freshness=FreshnessConfig(),  # v0.4 CS-017: staged effects carry a TTL
    )
    # v0.4 wiring: the worker's clock must be the same fixed instant the demo
    # decides at — a wall clock would see every DEMO_NOW-stamped TTL as long
    # expired. It also re-runs volatile gates (CS-017) and re-asserts scope at
    # dispatch (CS-018).
    worker = DispatchWorker(
        outbox, connectors, registry=registry, kill=kill,
        clock=lambda: DEMO_NOW,
        revalidate=make_dispatch_revalidator(engine, policy),
        scopes=scopes,
    )
    return GatewayBundle(gateway=gateway, policy=policy, audit=audit,
                         outbox=outbox, kill=kill, worker=worker)
