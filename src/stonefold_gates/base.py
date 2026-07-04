"""Shared plumbing for the gate implementations (design §6).

A gate is a small deterministic function ``(cfg, GateContext) -> GateResult``
resolving to PASS / FAIL / HOLD. It MUST NOT raise to signal a *policy* decision
(CLAUDE.md): a raised exception means a *dependency failure*, which the gate
itself converts to fail-closed FAIL (or, where ``failureMode: open`` is set, to
PASS). This module holds the context object, the value-resolution helpers, and
the ``N/window`` / duration parsers every gate shares.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from stonefold_core.condition import (
    ConditionRuntimeError,
    EvalContext,
    MissingValueError,
)
from stonefold_core.enums import Outcome
from stonefold_core.gating import RequestEnv
from stonefold_core.models import Actor, GateResult, ResolvedAction, Session
from stonefold_core.policy import FailureMode
from stonefold_core.registry import InMemoryRegistry
from stonefold_store import CounterStore

# A "session" window is unbounded in time but keyed by the session id — model it
# as a very long sliding window so one mechanism serves both forms.
SESSION_WINDOW_S: float = 10.0 * 365 * 24 * 3600

PreconditionCheck = Callable[["GateContext"], bool]


@dataclass(frozen=True)
class GateContext:
    """Everything a gate may read. Built once per request by the engine."""

    resolved: ResolvedAction
    actor: Actor
    session: Session
    env: RequestEnv
    eval_ctx: EvalContext
    registry: InMemoryRegistry
    counters: CounterStore
    hooks: Any  # stonefold_gates.content.ContentHookRegistry (Any avoids an import cycle)
    preconditions: Mapping[str, PreconditionCheck]
    failure_mode: FailureMode
    agent: str


GateFn = Callable[[Any, GateContext], GateResult]


# --- GateResult builders -------------------------------------------------
def passed(gate: str, reason: str = "") -> GateResult:
    return GateResult(gate=gate, outcome=Outcome.PASS, reason=reason)


def failed(gate: str, reason: str) -> GateResult:
    return GateResult(gate=gate, outcome=Outcome.FAIL, reason=reason)


def held(gate: str, reason: str) -> GateResult:
    return GateResult(gate=gate, outcome=Outcome.HOLD, reason=reason)


# --- value resolution ----------------------------------------------------
def resolve_field(field_path: str | None, gctx: GateContext) -> Any:
    """Resolve a dotted ``namespace.field`` reference (e.g. ``data.amount``)
    against the request context. Raises ``MissingValueError`` if the reference is
    absent or malformed — the caller turns that into fail-closed FAIL (design
    §10)."""
    if not isinstance(field_path, str) or not field_path:
        raise MissingValueError(f"bad field reference {field_path!r}")
    return gctx.eval_ctx.lookup(tuple(field_path.split(".")))


def to_number(value: Any) -> float:
    if isinstance(value, bool):
        raise ConditionRuntimeError(f"{value!r} is not numeric")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise ConditionRuntimeError(f"{value!r} is not numeric") from exc
    raise ConditionRuntimeError(f"{value!r} is not numeric")


def now_ts(gctx: GateContext) -> float:
    """The injected wall-clock as epoch seconds. No clock ⇒ fail-closed for any
    time/counter gate (we never invent a time — invariant 1)."""
    if gctx.env.now is None:
        raise MissingValueError("no clock supplied for a time-based gate")
    return gctx.env.now.timestamp()


# --- window / rate parsing ----------------------------------------------
_WINDOW_WORDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
}
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def window_seconds(token: object) -> float:
    """Convert a window token to seconds: a word (``hour``/``day``), a duration
    (``24h``/``15m``), or ``session`` (unbounded, keyed by session)."""
    text = str(token).strip()
    if text == "session":
        return SESSION_WINDOW_S
    if text in _WINDOW_WORDS:
        return float(_WINDOW_WORDS[text])
    m = re.fullmatch(r"(\d+)([smhd])", text)
    if m is not None:
        return float(int(m.group(1)) * _DURATION_UNITS[m.group(2)])
    raise ConditionRuntimeError(f"unrecognised window {token!r}")


def parse_rate(spec: object) -> tuple[float, float]:
    """Parse ``"N/window"`` into ``(limit, window_seconds)``."""
    text = str(spec).strip()
    n, sep, w = text.partition("/")
    if not sep:
        raise ConditionRuntimeError(f"rate spec must be 'N/window', got {spec!r}")
    try:
        limit = float(n.strip())
    except ValueError as exc:
        raise ConditionRuntimeError(f"bad limit in {spec!r}") from exc
    return limit, window_seconds(w)
