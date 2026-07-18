# SPDX-License-Identifier: Apache-2.0
"""The operator kill REST surface (design §8, RFC §9, plan M5 task 4).

Three endpoints over a ``KillService`` — issue a kill, lift one, list the active
orders. Issuing/lifting is audited inside the service (an operator action,
design §8.2). The agent never reaches these routes; they are an operator/admin
control plane. ``HALT`` is surfaced to the agent through the enforcement
transport (the pipeline returns ``Decision.HALT``), not here.

FastAPI is the pinned transport (ADR docs/03). ``create_kill_router`` takes the
service explicitly so the app wiring and the tests share one construction path.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from stonefold_core.enums import Kind
from stonefold_core.kill import KillScope, KillScopeKind
from stonefold_gateway.kill_service import KillService


class IssueKillBody(BaseModel):
    """Operator request to issue a kill. ``scope`` selects which facets apply."""

    scope: KillScopeKind
    issued_by: str
    agent: str | None = None
    session_id: str | None = None
    action_kind: Kind | None = None
    resource: str | None = None
    action: str | None = None
    predicate: str | None = None


class LiftKillBody(BaseModel):
    lifted_by: str


def scope_from_body(body: IssueKillBody) -> KillScope:
    """Build the durable ``KillScope`` for an ``IssueKillBody``, validating that the
    facets the chosen ``scope`` kind requires are present (raises ``HTTPException``
    422 otherwise). Shared by this router and the demo's kill control plane so the
    request shape and validation have a single source of truth."""
    if body.scope is KillScopeKind.GLOBAL:
        return KillScope.for_global()
    if body.scope is KillScopeKind.AGENT:
        if not body.agent:
            raise HTTPException(status_code=422, detail="agent required for an AGENT kill")
        return KillScope.for_agent(body.agent)
    if body.scope is KillScopeKind.SESSION:
        if not body.session_id:
            raise HTTPException(status_code=422, detail="session_id required for a SESSION kill")
        return KillScope.for_session(body.session_id)
    # ACTION_CLASS
    if body.action_kind is None and not body.resource and not body.action:
        raise HTTPException(
            status_code=422, detail="an ACTION_CLASS kill must fix at least one facet"
        )
    return KillScope.for_action_class(
        kind=body.action_kind, resource=body.resource, action=body.action
    )


def create_kill_router(service: KillService) -> APIRouter:
    router = APIRouter(prefix="/kill", tags=["kill"])

    @router.post("")
    def issue_kill(body: IssueKillBody) -> dict[str, object]:
        order = service.issue(scope_from_body(body), issued_by=body.issued_by,
                              predicate=body.predicate)
        return order.model_dump(mode="json")

    @router.get("")
    def list_active() -> list[dict[str, object]]:
        return [o.model_dump(mode="json") for o in service.active()]

    @router.post("/{order_id}/lift")
    def lift_kill(order_id: str, body: LiftKillBody) -> dict[str, object]:
        try:
            order = service.lift(order_id, lifted_by=body.lifted_by)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no kill order {order_id}")
        return order.model_dump(mode="json")

    return router
