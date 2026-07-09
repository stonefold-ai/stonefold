"""The thin admin control plane (plan M6 task 4, DoD).

Three operator surfaces over the durable stores — all read/transition the same
``audit_log`` and ``pending_actions`` the pipeline writes:

* ``GET  /admin/trace/{correlationId}`` — the live trace: intent → decision →
  effect for one agent run (the audit replay, RFC §11).
* ``GET  /admin/approvals`` — the approvals inbox: rows held ``PENDING_APPROVAL``.
* ``POST /admin/approvals/{id}/approve|reject`` — a human releases or rejects a
  held action (design §7; dual-auth rejects self-approval).

The kill button is the kill router (``kill_api``); ``main.create_app`` mounts both.
This module is FastAPI-only glue: no policy logic lives here.
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from stonefold_core.models import AuditRecord
from stonefold_core.outbox import (
    ApprovalError,
    OutboxStore,
    PendingState,
    SelfApprovalError,
    UnknownTicketError,
)


class ReplayableAudit(Protocol):
    """An audit sink that can replay a run (``InMemoryAuditSink`` /
    ``PostgresAuditSink`` both satisfy this)."""

    def by_correlation(self, correlation_id: str) -> list[AuditRecord]: ...


class ApproverBody(BaseModel):
    approver: str
    # v0.6 (CS-027): target one release contract by its gate key (e.g.
    # "precondition"); None credits every contract the identity may satisfy.
    gate: str | None = None


def create_admin_router(*, audit: ReplayableAudit, outbox: OutboxStore) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.get("/trace/{correlation_id}")
    def trace(correlation_id: str) -> list[dict[str, Any]]:
        return [r.model_dump(mode="json") for r in audit.by_correlation(correlation_id)]

    @router.get("/approvals")
    def approvals() -> list[dict[str, Any]]:
        held = outbox.list_by_state(PendingState.PENDING_APPROVAL)
        return [a.model_dump(mode="json") for a in held]

    @router.post("/approvals/{action_id}/approve")
    def approve(action_id: str, body: ApproverBody) -> dict[str, Any]:
        try:
            row = outbox.approve(action_id, body.approver, gate=body.gate)
        except SelfApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except UnknownTicketError:
            raise HTTPException(status_code=404, detail=f"no pending action {action_id}")
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return row.model_dump(mode="json")

    @router.post("/approvals/{action_id}/reject")
    def reject(action_id: str, body: ApproverBody) -> dict[str, Any]:
        try:
            row = outbox.reject(action_id, body.approver)
        except UnknownTicketError:
            raise HTTPException(status_code=404, detail=f"no pending action {action_id}")
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return row.model_dump(mode="json")

    return router
