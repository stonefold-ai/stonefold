"""The structural seam between the pure pipeline and the gate layer (RFC §7,
§12 step 4; design §6).

``acp_core`` stays pure: it declares the *interface* a gate engine satisfies and
the value types that cross the boundary, but never imports ``acp_gates`` /
``acp_store``. The concrete engine (``acp_gates.engine``) is injected into
``enforce`` by the gateway or the tests — exactly the ``Protocol``-for-seams
convention in CLAUDE.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from acp_core.enums import Outcome
from acp_core.models import Actor, GateResult, ResolvedAction, Session

if TYPE_CHECKING:
    from acp_core.compiler import CompiledPolicy


@dataclass(frozen=True)
class RequestEnv:
    """Per-request runtime values gates read that are **not** in the agent's call.

    ``resource`` holds the resolved target's properties (e.g. ``currentState``,
    ``patientId``) and ``context`` the ambient state (ROE, session spend). Both
    come from the gateway/session — never the agent payload (invariant 3).
    ``now`` is the injected clock so gate decisions stay deterministic and
    testable (invariant 1). ``sink`` is the requested disclosure destination;
    ``cost`` the estimated spend unit for ``spendLimit``.
    """

    resource: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    now: datetime | None = None
    sink: str | None = None
    cost: float | None = None


@dataclass(frozen=True)
class ApprovalSpec:
    """What a HOLD needs to be released by a human (RFC §7.8/§7.9). Derived from
    the holding gate's config and carried to the outbox so the staged row knows
    how many distinct approvals it requires."""

    quorum: int = 1
    dual_auth: bool = False
    distinct_from_actor: bool = False
    approvers: tuple[str, ...] = ()
    timeout_s: float | None = None
    on_timeout: str = "deny"  # "deny" (default) | "allow"


@dataclass(frozen=True)
class GateOutcome:
    """The gate stage's verdict: PASS ⇒ ALLOW, FAIL ⇒ DENY, HOLD ⇒ HOLD
    (RFC §12 step 4). ``results`` is the per-gate trace for the audit record;
    ``approval`` is set when the HOLD came from an approval gate."""

    outcome: Outcome
    results: tuple[GateResult, ...] = ()
    reason: str = ""
    ticket: str | None = None
    approval: "ApprovalSpec | None" = None


class GateEngine(Protocol):
    """What ``enforce`` needs from the gate layer (design §6). The engine owns
    the gate registry, the counter store, and the content hooks; the pipeline
    only asks it for a verdict."""

    def evaluate(
        self,
        resolved: ResolvedAction,
        actor: Actor,
        session: Session,
        policy: "CompiledPolicy",
        env: RequestEnv,
    ) -> GateOutcome: ...
