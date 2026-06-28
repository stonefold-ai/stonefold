"""The outbox seam — staged effects, approvals, and the kill substrate (RFC §4.4,
design §7/§9).

Every external effect is staged as a ``pending_actions`` row before anything
leaves the gateway (invariant 4). One table and one lifecycle back three
features: durable at-least-once dispatch, the approval HOLD point, and (M5) the
kill cancellation window — "approvals and kill are both just transitions on
staged actions" (design §7, review note).

``acp_core`` declares the value types and the ``OutboxStore`` protocol; the
in-memory and Postgres implementations live in ``acp_store``. The kernel never
does the I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from acp_core.gating import ApprovalSpec
from acp_core.models import Actor, AuditRecord, Compensation, GateResult, ResolvedAction


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
    created_at: datetime
    updated_at: datetime


# A kill predicate evaluated INSIDE the dispatch transaction (design §8.4). M4
# passes ``None`` (no kill); M5 supplies the real check so the PENDING→DISPATCHING
# transition and the kill test commit together.
KillCheck = Callable[[PendingAction], bool]


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
    ) -> PendingAction:
        """Persist a new staged row and return it (with generated id + key)."""
        ...

    def get(self, action_id: str) -> PendingAction | None: ...

    def list_by_state(self, state: PendingState) -> list[PendingAction]: ...

    def claim_next_pending(self, kill_check: KillCheck | None = None) -> PendingAction | None:
        """Atomically move one ``PENDING`` row to ``DISPATCHING`` and return it
        (or cancel it if ``kill_check`` matches). ``None`` if none are ready."""
        ...

    def settle(
        self,
        action_id: str,
        *,
        state: PendingState,
        result: dict[str, Any] | None = None,
        audit: AuditRecord | None = None,
    ) -> PendingAction:
        """Move a ``DISPATCHING`` row to ``DONE``/``FAILED``, writing ``audit`` in
        the **same transaction** as the state change (invariant 6)."""
        ...

    def approve(self, action_id: str, approver_id: str) -> PendingAction:
        """Record a distinct approval; promote to ``PENDING`` once quorum is met.
        Raises ``SelfApprovalError`` if the actor approves its own dual-auth row."""
        ...

    def reject(self, action_id: str, approver_id: str) -> PendingAction:
        """Reject a held row ⇒ ``CANCELLED`` (never dispatched)."""
        ...
