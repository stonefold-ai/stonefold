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

from stonefold_core.enums import Decision
from stonefold_core.gating import ApprovalSpec
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


def cancellation_record(row: PendingAction, reason: str) -> AuditRecord:
    """The audit record for a row cancelled inside the dispatch claim (CS-017:
    stale cancellations are audited, in the same transaction as the cancel).
    Deferred import keeps the module import graph acyclic-by-inspection."""
    from stonefold_core.audit import build_record

    result = EvalResult(decision=Decision.DENY, rule=reason, gates=row.gates, ticket=row.id)
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

    def approve(self, action_id: str, approver_id: str) -> PendingAction:
        """Record a distinct approval; promote to ``PENDING`` once quorum is met.
        Raises ``SelfApprovalError`` if the actor approves its own dual-auth row."""
        ...

    def reject(self, action_id: str, approver_id: str) -> PendingAction:
        """Reject a held row ⇒ ``CANCELLED`` (never dispatched)."""
        ...
