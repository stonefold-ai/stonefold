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
    RetryClass,
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
    """The result of evaluating one gate (design §2).

    v0.6 (CS-026/CS-029): a deny/hold additionally carries a machine-readable
    ``code``; ``source`` names the registered check/hook that produced the
    verdict (empty for the gate's own logic); ``evidence`` is optional
    check-supplied context for the human resolving a hold. All additive —
    pre-v0.6 records validate unchanged.
    """

    model_config = ConfigDict(frozen=True)

    gate: str
    outcome: Outcome
    reason: str = ""
    code: str = ""
    source: str = ""
    evidence: dict[str, Any] | None = None
    # v0.6 CS-029: the code's declared retry class (check-declared, else the
    # gate's built-in default assigned by the engine). ``None`` on PASS and on
    # approval-shaped holds (the agent's move there is to wait).
    retry_class: RetryClass | None = None


class EvalResult(BaseModel):
    """The terminal verdict of one ``enforce`` call (design §2, §3)."""

    model_config = ConfigDict(frozen=True)

    decision: Decision
    rule: str
    gates: tuple[GateResult, ...] = ()
    # v0.6 CS-029: the machine-readable reason code + retry class for a
    # deny/hold — the agent's convergence signal. Empty/None on ALLOW.
    reason_code: str = ""
    retry_class: RetryClass | None = None
    # Populated on HOLD/accepted-effect with the staged ``pending_actions`` id.
    ticket: str | None = None
    # The connector result on an executed ALLOW (rows for an observe, a receipt
    # for a record/transition). ``None`` for refusals and not-yet-executed effects.
    output: Any | None = None
    # Human-readable description of the scope applied below the model (RFC §11).
    scope_applied: tuple[str, ...] = ()


class BatchResult(BaseModel):
    """The terminal verdict of one ``enforce_batch`` call (RFC §12, CS-023).

    A SIF batch is decided atomically: every operation is decided first, then
    the batch either commits as a whole or is refused as a whole. ``results``
    carries one ``EvalResult`` per operation, in submission order — each backed
    by its own audit record (RFC §11).
    """

    model_config = ConfigDict(frozen=True)

    # The batch verdict: the first refusing operation's DENY/HALT; else HOLD
    # when any operation is held (the batch committed, approvals pending); else
    # ALLOW.
    decision: Decision
    # Index of the refusing operation (the SIF §6 error pointer
    # ``operations[i]``). ``None`` when the batch committed.
    failing_index: int | None = None
    results: tuple[EvalResult, ...] = ()


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
    # v0.6 CS-029: the machine-readable code + retry class of a deny/hold —
    # what the agent was told (subject to CS-030 visibility; the audit always
    # carries it). Empty/None on ALLOW.
    reasonCode: str = ""
    retryClass: RetryClass | None = None
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
