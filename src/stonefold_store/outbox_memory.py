"""In-memory ``OutboxStore`` (design §9) — the test double and single-process
default. Implements the same lifecycle and approval semantics as the Postgres
store so the D1–D4 unit tests and the Postgres integration tests agree.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from stonefold_core.audit import AuditSink
from stonefold_core.gating import ApprovalSpec
from stonefold_core.models import Actor, AuditRecord, Compensation, GateResult, ResolvedAction
from stonefold_core.outbox import (
    ApprovalError,
    KillCheck,
    PendingAction,
    PendingState,
    SelfApprovalError,
    StaleCheck,
    UnknownTicketError,
    cancellation_record,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_pending(
    *,
    resolved: ResolvedAction,
    actor: Actor,
    session_id: str,
    agent: str,
    state: PendingState,
    correlation_id: str | None,
    gates: tuple[GateResult, ...],
    approval: ApprovalSpec | None,
    compensation: Compensation | None,
    expires_at: datetime | None = None,
) -> PendingAction:
    """Construct a staged row with generated id + idempotency key. id/clock
    generation lives here (the I/O layer), not in the pure pipeline (invariant 1)."""
    now = _now()
    return PendingAction(
        id=f"act_{uuid.uuid4().hex[:12]}",
        idempotency_key=uuid.uuid4().hex,
        state=state,
        agent=agent,
        resolved=resolved,
        actor=actor,
        session_id=session_id,
        correlation_id=correlation_id,
        gates=gates,
        approval=approval,
        compensation=compensation,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )


class InMemoryOutboxStore:
    """A dict-backed outbox keyed by ticket id, preserving stage order."""

    def __init__(self, audit: AuditSink | None = None) -> None:
        self._rows: dict[str, PendingAction] = {}
        self._order: list[str] = []
        self._audit = audit

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
        row = build_pending(
            resolved=resolved, actor=actor, session_id=session_id, agent=agent,
            state=state, correlation_id=correlation_id, gates=gates,
            approval=approval, compensation=compensation, expires_at=expires_at,
        )
        self._rows[row.id] = row
        self._order.append(row.id)
        return row

    def get(self, action_id: str) -> PendingAction | None:
        return self._rows.get(action_id)

    def list_by_state(self, state: PendingState) -> list[PendingAction]:
        return [self._rows[i] for i in self._order if self._rows[i].state is state]

    def claim_next_pending(
        self,
        kill_check: KillCheck | None = None,
        stale_check: StaleCheck | None = None,
    ) -> PendingAction | None:
        for action_id in self._order:
            row = self._rows[action_id]
            if row.state is not PendingState.PENDING:
                continue
            if kill_check is not None and kill_check(row):
                # killed inside the claim ⇒ CANCELLED, never dispatched (§8.4).
                self._rows[action_id] = row.model_copy(
                    update={"state": PendingState.CANCELLED, "reason": "kill", "updated_at": _now()}
                )
                continue
            if stale_check is not None and (stale := stale_check(row)) is not None:
                # stale inside the claim (v0.4 CS-017) ⇒ CANCELLED + audited, in
                # this same (logical) transaction; keep scanning for a fresh row.
                cancelled = row.model_copy(
                    update={"state": PendingState.CANCELLED, "reason": stale, "updated_at": _now()}
                )
                self._rows[action_id] = cancelled
                if self._audit is not None:
                    self._audit.write(cancellation_record(cancelled, stale))
                continue
            claimed = row.model_copy(
                update={"state": PendingState.DISPATCHING, "updated_at": _now()}
            )
            self._rows[action_id] = claimed
            return claimed
        return None

    def settle(
        self,
        action_id: str,
        *,
        state: PendingState,
        result: dict[str, Any] | None = None,
        audit: AuditRecord | None = None,
        reason: str | None = None,
    ) -> PendingAction:
        row = self._require(action_id)
        settled = row.model_copy(
            update={"state": state, "result": result, "reason": reason, "updated_at": _now()}
        )
        self._rows[action_id] = settled
        # audit shares this (logical) transaction with the settle (invariant 6).
        if audit is not None and self._audit is not None:
            self._audit.write(audit)
        return settled

    def approve(self, action_id: str, approver_id: str) -> PendingAction:
        row = self._require(action_id)
        if row.state is not PendingState.PENDING_APPROVAL:
            raise ApprovalError(f"{action_id} is {row.state.value}, not awaiting approval")
        spec = row.approval or ApprovalSpec()
        if (spec.dual_auth or spec.distinct_from_actor) and approver_id == row.actor.id:
            raise SelfApprovalError(f"{approver_id} cannot approve its own action")
        approvals = tuple(sorted(set(row.approvals) | {approver_id}))
        new_state = (
            PendingState.PENDING if len(approvals) >= spec.quorum else PendingState.PENDING_APPROVAL
        )
        updated = row.model_copy(
            update={"approvals": approvals, "state": new_state, "updated_at": _now()}
        )
        self._rows[action_id] = updated
        return updated

    def reject(self, action_id: str, approver_id: str) -> PendingAction:
        row = self._require(action_id)
        if row.state is not PendingState.PENDING_APPROVAL:
            raise ApprovalError(f"{action_id} is {row.state.value}, not awaiting approval")
        updated = row.model_copy(
            update={
                "state": PendingState.CANCELLED,
                "reason": f"rejected by {approver_id}",
                "updated_at": _now(),
            }
        )
        self._rows[action_id] = updated
        return updated

    def _require(self, action_id: str) -> PendingAction:
        row = self._rows.get(action_id)
        if row is None:
            raise UnknownTicketError(action_id)
        return row
