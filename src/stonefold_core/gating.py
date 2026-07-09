"""The structural seam between the pure pipeline and the gate layer (RFC §7,
§12 step 4; design §6).

``stonefold_core`` stays pure: it declares the *interface* a gate engine satisfies and
the value types that cross the boundary, but never imports ``stonefold_gates`` /
``stonefold_store``. The concrete engine (``stonefold_gates.engine``) is injected into
``enforce`` by the gateway or the tests — exactly the ``Protocol``-for-seams
convention in CLAUDE.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from stonefold_core.enums import Outcome
from stonefold_core.models import Actor, GateResult, ResolvedAction, Session

if TYPE_CHECKING:
    from stonefold_core.compiler import CompiledPolicy


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
    how many distinct approvals it requires. v0.6 (CS-027) generalises this into
    ``ReleaseContract`` — one per holding gate; ``ApprovalSpec`` remains for
    pre-v0.6 rows and callers."""

    quorum: int = 1
    dual_auth: bool = False
    distinct_from_actor: bool = False
    approvers: tuple[str, ...] = ()
    timeout_s: float | None = None
    on_timeout: str = "deny"  # "deny" (default) | "allow"


@dataclass(frozen=True)
class ReleaseContract:
    """What ONE holding gate demands before the staged row may promote
    (RFC §12, CS-027). A held row carries one contract per holding gate and
    promotes only when every contract is satisfied — satisfying one never
    satisfies another. ``satisfied_by`` records the identities credited so far
    (a human approver/resolver, or ``system:timeout`` for ``onTimeout: allow``,
    CS-028)."""

    gate: str  # the holding gate key, e.g. "requireApproval", "precondition"
    cause: str = ""  # audit cause, e.g. "precondition:matchesOpenPurchaseOrder"
    quorum: int = 1
    dual_auth: bool = False
    distinct_from_actor: bool = False
    approvers: tuple[str, ...] = ()  # approver/resolver role names (identity seam)
    timeout_s: float | None = None
    on_timeout: str = "deny"  # "deny" (default) | "allow"
    reason_code: str = ""  # the hold's machine-readable code (CS-026 rule 2)
    evidence: dict[str, Any] | None = None  # optional check-supplied context
    satisfied_by: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        return len(self.satisfied_by) >= self.quorum

    def audit_dict(self) -> dict[str, Any]:
        """The RFC §11 rendering of this contract (one entry of ``releases``)."""
        return {
            "gate": self.gate,
            "cause": self.cause or self.gate,
            "quorum": self.quorum,
            "dualAuthorization": self.dual_auth,
            "approvers": list(self.approvers),
            "timeoutSeconds": self.timeout_s,
            "onTimeout": self.on_timeout,
            "reasonCode": self.reason_code,
            "evidence": self.evidence,
            "satisfiedBy": list(self.satisfied_by),
            "satisfied": self.satisfied,
        }


def contract_from_approval(spec: ApprovalSpec) -> ReleaseContract:
    """Adapt a pre-v0.6 ``ApprovalSpec`` row to the contract model (CS-027
    compatibility: legacy held rows keep their exact release semantics)."""
    gate = "dualAuthorization" if spec.dual_auth else "requireApproval"
    return ReleaseContract(
        gate=gate,
        cause=gate,
        quorum=spec.quorum,
        dual_auth=spec.dual_auth,
        distinct_from_actor=spec.distinct_from_actor,
        approvers=spec.approvers,
        timeout_s=spec.timeout_s,
        on_timeout=spec.on_timeout,
    )


@dataclass(frozen=True)
class GateOutcome:
    """The gate stage's verdict: PASS ⇒ ALLOW, FAIL ⇒ DENY, HOLD ⇒ HOLD
    (RFC §12 step 4). ``results`` is the per-gate trace for the audit record;
    ``releases`` carries the release contract of EVERY holding gate (CS-027);
    ``approval`` mirrors the first approval-shaped contract for pre-v0.6
    consumers."""

    outcome: Outcome
    results: tuple[GateResult, ...] = ()
    reason: str = ""
    ticket: str | None = None
    approval: "ApprovalSpec | None" = None
    releases: tuple[ReleaseContract, ...] = ()


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
