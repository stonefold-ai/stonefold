"""The outbox seam — staged effects, approvals, and the kill substrate (RFC §4.4,
design §7/§9).

Every external effect is staged as a ``pending_actions`` row before anything
leaves the gateway (invariant 4). One table and one lifecycle back three
features: durable at-least-once dispatch, the approval HOLD point, and (M5) the
kill cancellation window — "approvals and kill are both just transitions on
staged actions" (design §7, review note).

``stonefold_core`` declares the value types and the ``OutboxStore`` protocol; the
in-memory and Postgres implementations live in ``stonefold_store``. The kernel never
does the I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from dataclasses import replace

from stonefold_core.enums import Decision
from stonefold_core.gating import ApprovalSpec, ReleaseContract, contract_from_approval
from stonefold_core.models import (
    Actor,
    AuditRecord,
    Compensation,
    EvalResult,
    GateResult,
    RawCall,
    ResolvedAction,
    Session,
)


class PendingState(str, Enum):
    """The lifecycle of a staged effect (design §9)."""

    PENDING_APPROVAL = "pending_approval"  # held for a human (from a gate HOLD)
    PENDING = "pending"  # ready to dispatch
    DISPATCHING = "dispatching"  # claimed by a worker (inside the FOR UPDATE)
    DONE = "done"  # dispatched successfully
    FAILED = "failed"  # dispatch failed
    CANCELLED = "cancelled"  # killed or rejected before dispatch


class OutboxError(Exception):
    """Base class for outbox faults."""


class UnknownTicketError(OutboxError):
    """No staged row with the given id."""


class ApprovalError(OutboxError):
    """An approval action that is not legal for the row's current state."""


class SelfApprovalError(ApprovalError):
    """The actor tried to approve its own action (RFC §7.9 dual-auth)."""


class PendingAction(BaseModel):
    """One staged row. Carries enough to re-dispatch the effect later, plus the
    approval contract and an idempotency key (design §9)."""

    model_config = ConfigDict(frozen=True)

    id: str  # ticket, e.g. "act_<hex>"
    idempotency_key: str  # makes the connector send safe under retries
    state: PendingState
    agent: str
    resolved: ResolvedAction
    actor: Actor
    session_id: str
    correlation_id: str | None = None
    gates: tuple[GateResult, ...] = ()
    approval: ApprovalSpec | None = None
    approvals: tuple[str, ...] = ()  # distinct approver ids recorded so far
    # v0.6 (CS-027): the release contract of EVERY holding gate; the row promotes
    # only when all are satisfied. Empty for pre-v0.6 rows — ``effective_contracts``
    # synthesises the legacy single contract from ``approval`` in that case.
    releases: tuple[ReleaseContract, ...] = ()
    compensation: Compensation | None = None
    result: dict[str, Any] | None = None  # connector result on settle
    reason: str | None = None  # why cancelled/failed
    # Decision TTL (v0.4 CS-017): the staging-time expiry. A row claimed at or
    # after this instant is cancelled ``stale-decision``. ``None`` = no expiry
    # (freshness not configured — pre-v0.4 behaviour).
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


# A kill predicate evaluated INSIDE the dispatch transaction (design §8.4). M4
# passes ``None`` (no kill); M5 supplies the real check so the PENDING→DISPATCHING
# transition and the kill test commit together.
KillCheck = Callable[[PendingAction], bool]

# The freshness check evaluated INSIDE the dispatch claim, after the kill check
# and before the connector call (v0.4 CS-017: kill → TTL → volatile gates →
# connector). Returns the cancel reason (``stale-decision`` /
# ``stale-guard:<gate>``) or ``None`` when the row may dispatch.
StaleCheck = Callable[[PendingAction], "str | None"]


def expired_hold_reason(gate: str) -> str:
    """Settle reason for a held row cancelled by the expiry sweep (v0.6 CS-028) —
    normative, like ``stale-decision``."""
    return f"expired-hold:{gate}"


def effective_contracts(row: PendingAction) -> tuple[ReleaseContract, ...]:
    """The release contracts governing a held row (CS-027).

    v0.6 rows carry them in ``releases``; a pre-v0.6 row synthesises the single
    legacy contract from ``approval`` (crediting its recorded ``approvals`` so an
    in-flight quorum survives an upgrade); a legacy row with no spec at all gets
    the permissive quorum-1 contract it always had.
    """
    if row.releases:
        return row.releases
    legacy = contract_from_approval(row.approval or ApprovalSpec())
    return (replace(legacy, satisfied_by=row.approvals),)


def apply_release(
    row: PendingAction, approver_id: str, *, gate: str | None = None
) -> PendingAction:
    """Credit one release identity against a held row's contracts and return the
    updated row (CS-027). Pure — both stores persist the result.

    ``gate=None`` credits every contract the identity may satisfy (the pre-v0.6
    call shape); ``gate="precondition"`` credits only that gate's contract.
    Rules: a ``dual_auth``/``distinct_from_actor`` contract never accepts the
    acting principal; an identity is counted at most once per contract; the row
    promotes to ``PENDING`` only when EVERY contract is satisfied. Raises
    ``SelfApprovalError`` when the call could credit nothing because every
    targeted unsatisfied contract refused the actor; ``ApprovalError`` when
    ``gate`` names no contract on the row.
    """
    if row.state is not PendingState.PENDING_APPROVAL:
        raise ApprovalError(f"{row.id} is {row.state.value}, not awaiting approval")
    contracts = effective_contracts(row)
    if gate is not None and not any(c.gate == gate for c in contracts):
        raise ApprovalError(f"{row.id} has no {gate!r} release contract")

    credited = False
    refused_self = False
    updated: list[ReleaseContract] = []
    for contract in contracts:
        targeted = (gate is None or contract.gate == gate) and not contract.satisfied
        if not targeted or approver_id in contract.satisfied_by:
            updated.append(contract)
            continue
        if (contract.dual_auth or contract.distinct_from_actor) and approver_id == row.actor.id:
            refused_self = True
            updated.append(contract)
            continue
        updated.append(replace(contract, satisfied_by=contract.satisfied_by + (approver_id,)))
        credited = True
    if not credited and refused_self:
        raise SelfApprovalError(f"{approver_id} cannot approve its own action")

    new_state = (
        PendingState.PENDING
        if all(c.satisfied for c in updated)
        else PendingState.PENDING_APPROVAL
    )
    approvals = tuple(sorted(set(row.approvals) | {approver_id})) if credited else row.approvals
    return row.model_copy(
        update={"releases": tuple(updated), "approvals": approvals, "state": new_state}
    )


def releases_audit(row: PendingAction, status: str) -> "dict[str, Any] | None":
    """The RFC §11 ``approval`` rendering of a row's release contracts (CS-027)
    at settle time: which gates held it, who released what, reason codes and
    evidence (I7). ``None`` for rows nothing held."""
    if not row.releases:
        return None
    return {
        "status": status,
        "ticket": row.id,
        "releases": [contract.audit_dict() for contract in row.releases],
    }


def cancellation_record(row: PendingAction, reason: str) -> AuditRecord:
    """The audit record for a row cancelled inside the dispatch claim (CS-017)
    or by the held-row expiry sweep (v0.6 CS-028) — audited in the same
    transaction as the cancel, preserving the original hold reason code in the
    ``gates`` trace and the release contracts in ``approval``. Deferred import
    keeps the module import graph acyclic-by-inspection."""
    from stonefold_core.audit import build_record
    from stonefold_core.reasons import classify

    reason_code, retry_class = classify(Decision.DENY, reason, row.gates)
    result = EvalResult(
        decision=Decision.DENY, rule=reason, gates=row.gates, ticket=row.id,
        reason_code=reason_code, retry_class=retry_class,
    )
    return build_record(
        agent=row.agent,
        actor=row.actor,
        session=Session(id=row.session_id, correlation_id=row.correlation_id),
        call=RawCall(
            resource=row.resolved.resource,
            action=row.resolved.action,
            data=dict(row.resolved.data),
        ),
        resolved=row.resolved,
        result=result,
        outcome="cancelled",
        approval=releases_audit(row, "cancelled"),
    )


class OutboxStore(Protocol):
    """Durable staging + lifecycle transitions (design §9). The Postgres
    implementation runs ``claim_next_pending`` as a ``SELECT … FOR UPDATE`` so a
    row is dispatched at most once (invariant 5)."""

    def stage(
        self,
        *,
        resolved: ResolvedAction,
        actor: Actor,
        session_id: str,
        agent: str,
        state: PendingState,
        correlation_id: str | None = None,
        gates: tuple[GateResult, ...] = (),
        approval: ApprovalSpec | None = None,
        releases: tuple[ReleaseContract, ...] = (),
        compensation: Compensation | None = None,
        expires_at: datetime | None = None,
    ) -> PendingAction:
        """Persist a new staged row and return it (with generated id + key)."""
        ...

    def get(self, action_id: str) -> PendingAction | None: ...

    def list_by_state(self, state: PendingState) -> list[PendingAction]: ...

    def claim_next_pending(
        self,
        kill_check: KillCheck | None = None,
        stale_check: StaleCheck | None = None,
    ) -> PendingAction | None:
        """Atomically move one ``PENDING`` row to ``DISPATCHING`` and return it —
        or cancel it inside the same claim if ``kill_check`` matches (reason
        ``kill``) or ``stale_check`` returns a reason (CS-017; audited) and keep
        scanning. ``None`` if no row is ready."""
        ...

    def settle(
        self,
        action_id: str,
        *,
        state: PendingState,
        result: dict[str, Any] | None = None,
        audit: AuditRecord | None = None,
        reason: str | None = None,
    ) -> PendingAction:
        """Move a ``DISPATCHING`` row to ``DONE``/``FAILED``, writing ``audit`` in
        the **same transaction** as the state change (invariant 6). ``reason``
        records *why* a row failed/cancelled (e.g. ``scope-lost``, CS-018)."""
        ...

    def approve(
        self, action_id: str, approver_id: str, *, gate: str | None = None
    ) -> PendingAction:
        """Record a release identity; promote to ``PENDING`` once EVERY release
        contract is satisfied (CS-027 — ``apply_release`` is the shared logic).
        ``gate`` targets one contract; ``None`` credits all the identity may
        satisfy. Raises ``SelfApprovalError`` if the actor approves its own
        dual-auth/distinct contract."""
        ...

    def reject(self, action_id: str, approver_id: str) -> PendingAction:
        """Reject a held row ⇒ ``CANCELLED`` (never dispatched)."""
        ...
