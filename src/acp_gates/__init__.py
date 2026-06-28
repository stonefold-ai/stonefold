"""acp_gates — the fourteen deterministic gates and the gate engine (M2, RFC §7).

Depends on ``acp_core`` (value model, condition engine, the ``GateEngine`` seam)
and ``acp_store`` (counters); nothing in ``acp_core`` imports this package back —
the engine is injected into ``enforce`` through ``acp_core.gating.GateEngine``.
"""

from __future__ import annotations

from acp_gates.base import GateContext, PreconditionCheck
from acp_gates.content import (
    ContentHookRegistry,
    HookError,
    HookTimeout,
    default_hooks,
    dlp_basic,
)
from acp_gates.engine import DefaultGateEngine, build_eval_context
from acp_gates.gates import disclosure_post_check

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
