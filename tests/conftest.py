"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from acp_core import Actor, InMemoryRegistry, RawCall, Session, load_registry
from acp_core.gating import RequestEnv
from acp_core.policy import FailureMode
from acp_gates.base import GateContext, PreconditionCheck
from acp_gates.content import ContentHookRegistry, default_hooks
from acp_gates.engine import build_eval_context
from acp_store import CounterStore, InMemoryCounterStore

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).parent.parent
EXAMPLES = PROJECT_ROOT / "examples"
REGISTRY_DIR = PROJECT_ROOT / "registry"
SCHEMA = PROJECT_ROOT / "schema" / "acp.schema.json"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def min_registry() -> InMemoryRegistry:
    """The small M0 fixture registry."""
    return load_registry(load_yaml(FIXTURES / "registry_min.yaml"))


def full_registry() -> InMemoryRegistry:
    """The example-covering registry (M1+)."""
    return load_registry(load_yaml(REGISTRY_DIR / "acp-registry.yaml"))


def load_schema() -> dict[str, Any]:
    with SCHEMA.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def valid_example_paths() -> list[Path]:
    """Every example except the INTENTIONALLY-INVALID fixture."""
    return sorted(
        p for p in EXAMPLES.glob("*.acp.yaml") if not p.name.startswith("INVALID")
    )


def invalid_example_path() -> Path:
    return EXAMPLES / "INVALID-open-on-irreversible.acp.yaml"


# --- M2 gate helpers -----------------------------------------------------
def gate_ctx(
    resource: str,
    action: str,
    *,
    data: dict[str, Any] | None = None,
    actor: Actor | None = None,
    env: RequestEnv | None = None,
    registry: InMemoryRegistry | None = None,
    counters: CounterStore | None = None,
    hooks: ContentHookRegistry | None = None,
    preconditions: dict[str, PreconditionCheck] | None = None,
    failure_mode: FailureMode = FailureMode.CLOSED,
    agent: str = "test-agent",
) -> GateContext:
    """Build a ``GateContext`` for exercising a single gate in isolation."""
    reg = registry or full_registry()
    resolved = reg.resolve(RawCall(resource=resource, action=action, data=data or {}))
    the_actor = actor or Actor(id="alice")
    the_env = env or RequestEnv()
    return GateContext(
        resolved=resolved,
        actor=the_actor,
        session=Session(id="s1"),
        env=the_env,
        eval_ctx=build_eval_context(resolved, the_actor, the_env),
        registry=reg,
        counters=counters or InMemoryCounterStore(),
        hooks=hooks or default_hooks(),
        preconditions=preconditions or {},
        failure_mode=failure_mode,
        agent=agent,
    )
