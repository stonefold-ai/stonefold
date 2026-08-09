# SPDX-License-Identifier: Apache-2.0
"""The gateway application factory (plan M6, design §0/§1).

``create_app`` wires the one chokepoint behind FastAPI: the intent endpoint
(``submit_intent``), the MCP surface (``/mcp/tools``, ``/mcp/search``,
``/mcp/call`` — design §1.2), the kill control plane (``kill_api``), and the thin
admin UI (``admin_api``). Every route ends in the *same* ``Gateway.submit`` →
``enforce`` call (design §0) — the transports cannot diverge. Retrieval changes
what an agent is shown, never what it may do.

Identity is resolved by the **``IdentityProvider`` seam** (CS-021,
``stonefold_gateway.identity``) from the authenticated transport (the ``X-Actor-Id`` /
``X-Session-Id`` headers by default), never from the request body (invariant 3: the
agent cannot set its own scope). The body carries only ``resource``/``action``/
``data``. The default provider is the standalone built-in; a credential verifier
plugs into the same slot without touching the route.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from stonefold_gateway.admin_api import ReplayableAudit, create_admin_router
from stonefold_gateway.identity import (
    IdentityProvider,
    IdentityRejected,
    SessionIdentityProvider,
    TransportCredential,
)
from stonefold_gateway.kill_api import create_kill_router
from stonefold_gateway.kill_service import KillService
from stonefold_gateway.mcp_search import search_response
from stonefold_gateway.transport import (
    Gateway,
    InvalidIntentError,
    MCPProxy,
    SifNativeTransport,
    ToolMapping,
    mcp_tool_schemas,
    validate_intent_data,
)
from stonefold_core.outbox import OutboxStore


class SubmitIntentBody(BaseModel):
    """The agent's intent — *what*, never *who* (invariant 3). Any ``actor`` /
    ``owner_id`` keys inside ``data`` are opaque parameters, never identity."""

    resource: str
    action: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class SubmitBatchBody(BaseModel):
    """The SIF wire form (SIF §5): ``{"operations": [...]}``. Decided atomically
    per RFC §12 / CS-023 — any DENY/HALT refuses the whole batch before anything
    commits or stages."""

    operations: list[SubmitIntentBody] = Field(min_length=1)


class McpCallBody(BaseModel):
    """An intercepted MCP tool call (design §1.2): the tool's name and its
    arguments. Identity comes from the transport here as everywhere else."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


def _render(result: Any) -> dict[str, Any]:
    """One operation's result in the wire shape (shared by single and batch).

    ``reasonCode``/``retryClass`` are the v0.6 (CS-029) convergence signal —
    an HTTP agent needs them to tell fix-and-resubmit from give-up; the result
    arriving here is already the redacted agent view (CS-030)."""
    return {
        "decision": result.decision.value,
        "rule": result.rule,
        "ticket": result.ticket,
        "output": result.output,
        "scopeApplied": list(result.scope_applied),
        "reasonCode": result.reason_code,
        "retryClass": result.retry_class.value if result.retry_class else None,
    }


def create_app(
    gateway: Gateway,
    *,
    kill_service: KillService | None = None,
    audit: ReplayableAudit | None = None,
    outbox: OutboxStore | None = None,
    identity: IdentityProvider | None = None,
) -> FastAPI:
    app = FastAPI(title="Stonefold Gateway", version="0.1")
    sif = SifNativeTransport(gateway)
    # The MCP surface (design §1.2): one typed tool per declared action, and a
    # proxy that maps each back to the action it was generated from. Generated
    # once at startup — the registry does not change under a running gateway,
    # and an unmapped tool is denied rather than passed through.
    mcp_tools = mcp_tool_schemas(gateway.registry)
    mcp_proxy = MCPProxy(
        gateway,
        [
            ToolMapping(
                tool=tool["name"],
                resource=tool["name"].split(".", 1)[0],
                action=tool["name"].split(".", 1)[1],
            )
            for tool in mcp_tools
        ],
    )
    # CS-021: identity enters through the seam ahead of the pipeline. The default
    # is the standalone built-in (transport-authenticated ids verbatim) — so an
    # unconfigured gateway behaves exactly as before; a credential verifier plugs
    # into the same slot without touching the route.
    identity_provider: IdentityProvider = identity or SessionIdentityProvider()

    if kill_service is not None:
        app.include_router(create_kill_router(kill_service))
    if audit is not None and outbox is not None:
        app.include_router(create_admin_router(audit=audit, outbox=outbox))

    def _identify(
        actor_id: str,
        session_id: str,
        correlation_id: str | None,
        credential: str | None,
    ) -> Any:
        # identity from the authenticated transport via the seam, NOT the body
        # (invariant 3, binding on every provider — CS-021). Shared by every
        # transport so none of them can resolve identity its own way.
        try:
            return identity_provider.identify(
                TransportCredential(
                    actor_id=actor_id, session_id=session_id,
                    correlation_id=correlation_id or session_id,
                    credential=credential,
                )
            )
        except IdentityRejected as exc:
            raise HTTPException(status_code=401, detail=str(exc))

    def _invalid(exc: InvalidIntentError) -> dict[str, Any]:
        """A malformed call is a shape error, not a decision — no rule ran, so
        it is not reported as a refusal (SIF §6 carries the pointer)."""
        return {
            "decision": "error",
            "rule": "invalid-data",
            "reasonCode": exc.code,
            "error": exc.as_error(),
        }

    @app.get("/tool-schema")
    def tool_schema() -> dict[str, Any]:
        """The single SIF-native tool schema, generated from the registry."""
        return sif.tool_schema

    @app.get("/mcp/tools")
    def mcp_tools_route() -> dict[str, Any]:
        """The whole surface: one typed tool per declared action.

        Correct for a small estate. At several hundred actions, prefer
        ``/mcp/search`` — a model choosing among hundreds of tools chooses worse,
        and this endpoint has no way to help it.
        """
        return {"tools": mcp_tools, "of": len(mcp_tools)}

    @app.get("/mcp/search")
    def mcp_search_route(q: str = "", limit: int = 5) -> dict[str, Any]:
        """The short candidate list for one step the agent is about to take.

        Retrieval only decides what the model is shown. Whether the action it
        then picks is allowed is decided by policy, on the way through
        ``/mcp/call``.
        """
        return search_response(mcp_tools, q, limit=limit)

    @app.post("/mcp/call")
    def mcp_call(
        body: McpCallBody,
        x_actor_id: str = Header(..., alias="X-Actor-Id"),
        x_session_id: str = Header(..., alias="X-Session-Id"),
        x_correlation_id: str | None = Header(None, alias="X-Correlation-Id"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict[str, Any]:
        """An intercepted tool call, enforced. An unmapped tool is denied and
        audited by the proxy — never passed through."""
        who = _identify(x_actor_id, x_session_id, x_correlation_id, authorization)
        # Same shape check as the intent endpoint, so the two surfaces reject a
        # malformed call identically.
        resource, _, action = body.tool.partition(".")
        try:
            validate_intent_data(gateway.registry, resource, action, body.arguments)
        except InvalidIntentError as exc:
            return _invalid(exc)
        result = mcp_proxy.call_tool(
            body.tool, body.arguments, actor=who.actor, session=who.session
        )
        return _render(result)

    @app.post("/submit_intent")
    def submit_intent(
        body: SubmitBatchBody | SubmitIntentBody,
        x_actor_id: str = Header(..., alias="X-Actor-Id"),
        x_session_id: str = Header(..., alias="X-Session-Id"),
        x_correlation_id: str | None = Header(None, alias="X-Correlation-Id"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict[str, Any]:
        who = _identify(x_actor_id, x_session_id, x_correlation_id, authorization)

        if isinstance(body, SubmitBatchBody):
            # SIF wire form (SIF §5): the batch is decided atomically (CS-023);
            # a refusal's structured error names the failing operation (SIF §6).
            try:
                batch = sif.submit_intent_batch(
                    [op.model_dump() for op in body.operations],
                    actor=who.actor, session=who.session,
                )
            except InvalidIntentError as exc:
                return _invalid(exc)
            response: dict[str, Any] = {
                "decision": batch.decision.value,
                "operations": [_render(r) for r in batch.results],
            }
            if batch.failing_index is not None:
                failing = batch.results[batch.failing_index]
                response["error"] = {
                    "code": "BATCH_REFUSED",
                    "pointer": f"operations[{batch.failing_index}]",
                    "message": (
                        f"operation {batch.failing_index} was refused "
                        f"({failing.decision.value}: {failing.rule}); "
                        "a batch commits atomically — nothing was applied "
                        "(submit independent intents for independent outcomes)"
                    ),
                }
            return response

        try:
            result = sif.submit_intent(
                {"resource": body.resource, "action": body.action, "data": body.data},
                actor=who.actor, session=who.session,
            )
        except InvalidIntentError as exc:
            return _invalid(exc)
        return _render(result)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _ADMIN_HTML

    return app


# A deliberately tiny single-file console: a trace viewer, the approvals inbox,
# and a global kill button — enough to show intent → decision → effect, approve a
# held action, and halt a session (M6 DoD). Not a product UI.
_ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Stonefold Gateway — console</title>
<style>
 body{font:14px system-ui,sans-serif;margin:2rem;max-width:60rem}
 h1{font-size:1.2rem} section{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}
 button{padding:.4rem .8rem;cursor:pointer} input{padding:.3rem}
 pre{background:#f6f6f6;padding:.6rem;border-radius:6px;overflow:auto}
 .kill{background:#b00020;color:#fff;border:0;border-radius:6px}
</style></head><body>
<h1>Stonefold Gateway — operator console</h1>

<section><h2>Trace (intent → decision → effect)</h2>
 <input id="cid" placeholder="correlationId"/>
 <button onclick="trace()">Replay</button>
 <pre id="trace">—</pre></section>

<section><h2>Approvals inbox</h2>
 <button onclick="inbox()">Refresh</button>
 <pre id="inbox">—</pre>
 <input id="aid" placeholder="action id"/><input id="approver" placeholder="approver"/>
 <button onclick="approve()">Approve</button>
 <button onclick="reject()">Reject</button></section>

<section><h2>Kill switch</h2>
 <input id="ksession" placeholder="session id (blank = global)"/>
 <button class="kill" onclick="kill()">HALT</button>
 <pre id="killout">—</pre></section>

<script>
 const show=(id,d)=>document.getElementById(id).textContent=JSON.stringify(d,null,2);
 async function trace(){const c=cid.value;show('trace',await (await fetch('/admin/trace/'+c)).json());}
 async function inbox(){show('inbox',await (await fetch('/admin/approvals')).json());}
 async function approve(){show('inbox',await (await fetch('/admin/approvals/'+aid.value+'/approve',
   {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({approver:approver.value})})).json());}
 async function reject(){show('inbox',await (await fetch('/admin/approvals/'+aid.value+'/reject',
   {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({approver:approver.value})})).json());}
 async function kill(){const s=ksession.value;const b=s?{scope:'session',session_id:s,issued_by:'console'}
   :{scope:'global',issued_by:'console'};
   show('killout',await (await fetch('/kill',{method:'POST',headers:{'content-type':'application/json'},
   body:JSON.stringify(b)})).json());}
</script></body></html>"""
