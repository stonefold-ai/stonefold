"""The demo gateway HTTP/WebSocket service (docs/05 components: ACP Gateway + UI).

Wraps an ``APBundle`` in FastAPI: the SIF-native ``submit_intent`` tool (identity
from headers, never the body — invariant 3), a live trace WebSocket for the UI,
the untrusted invoice inbox, the kill + approvals control planes (reused routers),
an in-process agent runner for the UI's interactive scenarios, and the static UI.

``make_app`` (used by ``uvicorn --factory``) builds the bundle from the
environment: Postgres + Redis when ``DATABASE_URL``/``REDIS_URL`` are set (the
docker-compose demo), otherwise fully in-process (a keyless local run).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from stonefold_core import AuditRecord
from stonefold_core.outbox import ApprovalError, SelfApprovalError, UnknownTicketError
from stonefold_ap_demo.agent import (
    SYSTEM_PROMPT,
    DirectBackend,
    InProcessGatedBackend,
    handled_invoice_ids,
    inbox_payload,
    run_agent,
)
from stonefold_ap_demo.gateway import APBundle, build_inmemory_bundle
from stonefold_ap_demo.llm import select_provider
from stonefold_ap_demo.scenarios import BLOCKED_PROMPT, GLOBEX_PROMPT, HAPPY_PROMPT, INBOX_PROMPT
from stonefold_gateway.kill_api import IssueKillBody, LiftKillBody, scope_from_body
from stonefold_gateway.transport import SifNativeTransport

_ROOT = Path(__file__).resolve().parents[2]
UI_DIR = _ROOT / "demo" / "ui"

_SCENARIO_PROMPTS = {
    "happy": HAPPY_PROMPT,
    "inbox": INBOX_PROMPT,
    "approval": GLOBEX_PROMPT,
    "blocked": BLOCKED_PROMPT,
}


class SubmitIntentBody(BaseModel):
    resource: str
    action: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class AgentRunBody(BaseModel):
    scenario: str | None = None
    prompt: str | None = None
    mode: str = "safe"  # "safe" = through the gateway | "unsafe" = gateway bypassed
    provider: str = "auto"


class ApproverBody(BaseModel):
    approver: str


def create_app(bundle: APBundle, *, default_provider: str = "auto") -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        bundle.start_worker()  # drain staged effects in the background
        try:
            yield
        finally:
            bundle.stop_worker()

    app = FastAPI(title="ACP Accounts-Payable Demo", version="0.1", lifespan=lifespan)
    sif = SifNativeTransport(bundle.gateway)

    @app.get("/tool-schema")
    def tool_schema() -> dict[str, Any]:
        return sif.tool_schema

    @app.get("/inbox")
    def inbox() -> dict[str, Any]:
        # the agent's untrusted input — deliberately NOT a gated resource. Invoices
        # the gateway already settled are tagged handled=true so a re-run is
        # idempotent (no duplicate pay / piled-up approval); a halted one is not.
        return inbox_payload(handled_invoice_ids(bundle))

    @app.post("/submit_intent")
    def submit_intent(
        body: SubmitIntentBody,
        x_actor_id: str = Header(..., alias="X-Actor-Id"),
        x_session_id: str = Header(..., alias="X-Session-Id"),
        x_correlation_id: str | None = Header(None, alias="X-Correlation-Id"),
    ) -> dict[str, Any]:
        result = bundle.submit(
            actor_id=x_actor_id, resource=body.resource, action=body.action,
            data=body.data, session_id=x_session_id,
            correlation_id=x_correlation_id or x_session_id,
        )
        return {
            "decision": result.decision.value, "rule": result.rule,
            "ticket": result.ticket, "scopeApplied": list(result.scope_applied),
            "output": result.output,
        }

    @app.get("/audit")
    def audit_all(
        agent: str | None = None,
        actor: str | None = None,
        resource: str | None = None,
        action: str | None = None,
        decision: str | None = None,
        has_result_ref: bool | None = None,
    ) -> list[dict[str, Any]]:
        """The audit log — the gateway's record so a human/external tool can *find*
        what the agent did (and the ``resultRefs`` handles to reconcile/compensate it
        downstream; RFC §11). Optional filters narrow the hunt; the gateway does not
        perform the reversal itself (boundary note, §11)."""
        records = bundle.audit_records()

        def _keep(r: AuditRecord) -> bool:
            if agent is not None and r.agent != agent:
                return False
            if actor is not None and r.actor != actor:
                return False
            if resource is not None and r.resource != resource:
                return False
            if action is not None and r.action != action:
                return False
            if decision is not None and r.decision.value != decision:
                return False
            if has_result_ref is not None and bool(r.resultRefs) != has_result_ref:
                return False
            return True

        return [r.model_dump(mode="json") for r in records if _keep(r)]

    # --- approvals control plane, routed through the locked bundle --- #
    @app.get("/admin/approvals")
    def approvals() -> list[dict[str, Any]]:
        return [a.model_dump(mode="json") for a in bundle.pending_approvals()]

    @app.get("/admin/trace/{correlation_id}")
    def trace_replay(correlation_id: str) -> list[dict[str, Any]]:
        return [r.model_dump(mode="json") for r in bundle.audit_by_correlation(correlation_id)]

    @app.post("/admin/approvals/{action_id}/approve")
    def approve(action_id: str, body: ApproverBody) -> dict[str, Any]:
        try:
            return bundle.approve(action_id, body.approver).model_dump(mode="json")
        except SelfApprovalError as exc:
            raise HTTPException(409, str(exc))
        except UnknownTicketError:
            raise HTTPException(404, f"no pending action {action_id}")
        except ApprovalError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/admin/approvals/{action_id}/reject")
    def reject(action_id: str, body: ApproverBody) -> dict[str, Any]:
        try:
            return bundle.reject(action_id, body.approver).model_dump(mode="json")
        except UnknownTicketError:
            raise HTTPException(404, f"no pending action {action_id}")
        except ApprovalError as exc:
            raise HTTPException(409, str(exc))

    # --- kill switch control plane, routed through the locked bundle --- #
    # The operator's emergency stop. Issuing a kill flags the chokepoint so every
    # matching action becomes an audited HALT and no staged effect dispatches
    # (invariant 5). Like approvals, these go through the bundle so they serialise
    # on the shared DB connection; the agent never reaches them (operator control).
    @app.get("/admin/kill")
    def list_kills() -> list[dict[str, Any]]:
        return [o.model_dump(mode="json") for o in bundle.active_kills()]

    @app.post("/admin/kill")
    def issue_kill(body: IssueKillBody) -> dict[str, Any]:
        order = bundle.issue_kill(
            scope_from_body(body), issued_by=body.issued_by, predicate=body.predicate
        )
        # Surface the halt on the live trace so the timeline shows it the instant
        # the operator hits the button (the per-action HALTs follow on the next run).
        bundle.trace.publish({
            "type": "decision", "actor": body.issued_by,
            "resource": "agent fleet", "action": "halt", "data": {},
            "decision": "halt", "rule": f"kill:{order.id}", "ticket": order.id,
        })
        return order.model_dump(mode="json")

    @app.post("/admin/kill/{order_id}/lift")
    def lift_kill(order_id: str, body: LiftKillBody) -> dict[str, Any]:
        try:
            order = bundle.lift_kill(order_id, lifted_by=body.lifted_by)
        except KeyError:
            raise HTTPException(404, f"no kill order {order_id}")
        bundle.trace.publish({
            "type": "decision", "actor": body.lifted_by,
            "resource": "agent fleet", "action": "resume", "data": {},
            "decision": "allow", "rule": f"kill-lifted:{order.id}", "ticket": order.id,
        })
        return order.model_dump(mode="json")

    @app.post("/agent/run")
    async def agent_run(body: AgentRunBody) -> dict[str, Any]:
        prompt = body.prompt or _SCENARIO_PROMPTS.get(body.scenario or "", "")
        if not prompt:
            return {"error": "supply a known scenario or a prompt"}
        provider = select_provider(body.provider or default_provider)
        if body.mode == "unsafe":
            backend: Any = DirectBackend(bundle.ledger, session_id="ui-unsafe")
        else:
            backend = InProcessGatedBackend(bundle, session_id=f"ui-{body.scenario or 'prompt'}")
        # the agent loop is blocking (LLM + gateway) — keep the event loop free.
        result = await run_in_threadpool(run_agent, prompt, provider=provider, backend=backend)
        bundle.drain()
        return {
            "provider": provider.label,
            "session": backend.session_id,
            # the raw inputs to the LLM, so the UI can show exactly what was sent
            "system": SYSTEM_PROMPT,
            "prompt": prompt,
            "final_text": result.final_text,
            # the raw tool calls (name + args) and their raw results, in order
            "steps": [{"tool": s.tool, "args": s.args, "result": s.result}
                      for s in result.steps],
            "decisions": result.decisions,
        }

    @app.websocket("/ws/trace")
    async def ws_trace(ws: WebSocket) -> None:
        await ws.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def _on_event(event: Any) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, dict(event))

        unsubscribe = bundle.trace.subscribe(_on_event)
        try:
            for past in bundle.trace.recent():  # backfill so a late UI sees history
                await ws.send_json(dict(past))
            while True:
                await ws.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        finally:
            unsubscribe()

    if UI_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")

    return app


def build_bundle_from_env() -> APBundle:
    """Postgres+Redis bundle when configured, else in-memory (keyless local run)."""
    dsn = os.environ.get("DATABASE_URL")
    redis_url = os.environ.get("REDIS_URL")
    if dsn and redis_url:
        import psycopg
        import redis

        conn = psycopg.connect(dsn, autocommit=True)
        client = redis.from_url(redis_url)
        from stonefold_ap_demo.gateway import build_postgres_bundle

        return build_postgres_bundle(conn, client)
    return build_inmemory_bundle()


def make_app() -> FastAPI:
    return create_app(build_bundle_from_env(),
                      default_provider=os.environ.get("LLM_PROVIDER", "auto"))
