"""The typed runtime objects the pipeline passes between stages (design §2).

Each value type is a frozen ``pydantic`` model so the pure kernel can treat them
as immutable. There is **no I/O and no framework import** in this module — it is
part of the trust kernel (``stonefold_core``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonefold_core.enums import (
    Decision,
    Emission,
    Explainability,
    Kind,
    OperativeForce,
    Outcome,
    Reversibility,
)


class Attributes(BaseModel):
    """Declared, read-only governance facts about an action (RFC §5).

    Conditions reference these (e.g. ``action.reversibility == irreversible``).
    They are declared on the action in the registry and are immutable to the
    policy. Defaults are the *least surprising* safe values; the registry is
    expected to declare them explicitly for any consequential action.
    """

    model_config = ConfigDict(frozen=True)

    reversibility: Reversibility = Reversibility.REVERSIBLE
    emission: Emission = Emission.NONE
    operativeForce: OperativeForce = OperativeForce.NONE
    # resultSensitivity is an open value set (RFC §5: "or a domain classification
    # label"), so it stays a plain string rather than an enum.
    resultSensitivity: str = "internal"
    explainability: Explainability = Explainability.NONE


class RawCall(BaseModel):
    """The agent's submitted intent, before resolution (design §1.1, §2).

    NOTE the deliberate absence of any ``actor``/``owner``/``tenant`` field: the
    agent supplies *what* it wants to do (resource, action, data) but never *who*
    it is. Identity is injected by the gateway from the session (RFC §6.3,
    invariant 3). If ``data`` happens to contain such fields they are treated as
    opaque parameters and never used for scope.
    """

    model_config = ConfigDict(frozen=True)

    resource: str
    action: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Compensation(BaseModel):
    """A declared compensating effect (RFC §4.4, design §8.5/§9): the action to
    auto-stage if an *irreversible* effect fails at dispatch — e.g. ``refund`` for
    a ``pay``. Names a resource+action the registry knows."""

    model_config = ConfigDict(frozen=True)

    resource: str
    action: str
    data: dict[str, Any] = Field(default_factory=dict)


class ResolvedAction(BaseModel):
    """An attempted action after registry resolution (design §2).

    Unknown names never reach this type — they short-circuit to DENY in
    ``Registry.resolve`` (RFC §12 step 1).
    """

    model_config = ConfigDict(frozen=True)

    kind: Kind
    resource: str
    action: str | None
    data: dict[str, Any]
    attrs: Attributes
    connector: str
    # The connector's pinned artifact digest (CS-020), copied from the registry at
    # resolution so the load-time and dispatch-time checks compare against the same
    # value that was in force when the action was staged. ``None`` ⇒ not pinned.
    connector_digest: str | None = None
    # Registry-declared legal from-states for a TRANSITION action (RFC §4.5).
    # Empty for non-transitions. The built-in transition precondition (M2) uses
    # this; it is a MUST-hold check, not optional policy.
    from_states: tuple[str, ...] = ()
    # Declared compensation for an irreversible effect (auto-staged on dispatch
    # failure, design §9). ``None`` ⇒ no compensation declared.
    compensation: Compensation | None = None


class Actor(BaseModel):
    """The end principal the agent acts for (RFC §2, design §2).

    Constructed by the gateway from the authenticated session/transport, *never*
    from the agent payload (invariant 3). Drives ``scope`` and approvals.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    roles: frozenset[str] = frozenset()
    claims: dict[str, Any] = Field(default_factory=dict)


class Session(BaseModel):
    """Ambient session identity used for correlation and kill matching."""

    model_config = ConfigDict(frozen=True)

    id: str
    correlation_id: str | None = None


class GateResult(BaseModel):
    """The result of evaluating one gate (design §2)."""

    model_config = ConfigDict(frozen=True)

    gate: str
    outcome: Outcome
    reason: str = ""


class EvalResult(BaseModel):
    """The terminal verdict of one ``enforce`` call (design §2, §3)."""

    model_config = ConfigDict(frozen=True)

    decision: Decision
    rule: str
    gates: tuple[GateResult, ...] = ()
    # Populated on HOLD/accepted-effect with the staged ``pending_actions`` id.
    ticket: str | None = None
    # The connector result on an executed ALLOW (rows for an observe, a receipt
    # for a record/transition). ``None`` for refusals and not-yet-executed effects.
    output: Any | None = None
    # Human-readable description of the scope applied below the model (RFC §11).
    scope_applied: tuple[str, ...] = ()


class AuditRecord(BaseModel):
    """One append-only audit record (RFC §11).

    Written for *every* evaluated action — allowed, held, denied, or halted.
    Fields mirror the RFC §11 "required at full" table; values not yet known at
    write time (e.g. ``outcome`` for a refusal) take their conservative default.
    """

    id: str
    timestamp: datetime
    agent: str
    actor: str
    kind: str | None = None
    resource: str | None = None
    action: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    scopeApplied: list[str] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)
    decision: Decision = Decision.DENY
    # The deciding rule/gate or settle reason (RFC §11: the decision is recorded
    # "with the deciding rule/gate") — e.g. "gate:denylist", "stale-decision",
    # "scope-lost", "dispatch".
    rule: str | None = None
    approval: dict[str, Any] | None = None
    # Connector result: "success" | "failure" | "not_executed".
    outcome: str = "not_executed"
    # RFC §11 (CS-009): the downstream identifier(s) of an executed/settled effect's
    # result — the connector-returned id(s) of the created/changed record(s), the
    # handle/lineage key an external system uses to locate/reconcile/compensate it.
    # A *list* because one action may fan out to several records (a payment + its
    # ledger entry); empty for refusals, holds, and non-effect actions.
    resultRefs: list[str] = Field(default_factory=list)
    correlationId: str | None = None
