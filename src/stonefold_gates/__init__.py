"""stonefold_gates — the fourteen deterministic gates and the gate engine (M2, RFC §7).

Depends on ``stonefold_core`` (value model, condition engine, the ``GateEngine`` seam)
and ``stonefold_store`` (counters); nothing in ``stonefold_core`` imports this package back —
the engine is injected into ``enforce`` through ``stonefold_core.gating.GateEngine``.
"""

from __future__ import annotations

from stonefold_gates.base import GateContext, PreconditionCheck
from stonefold_gates.content import (
    ContentHookRegistry,
    HookError,
    HookTimeout,
    default_hooks,
    dlp_basic,
)
from stonefold_gates.engine import DefaultGateEngine, build_eval_context
from stonefold_gates.gates import disclosure_post_check

__all__ = [
    "DefaultGateEngine",
    "build_eval_context",
    "GateContext",
    "PreconditionCheck",
    "ContentHookRegistry",
    "HookError",
    "HookTimeout",
    "default_hooks",
    "dlp_basic",
    "disclosure_post_check",
]
