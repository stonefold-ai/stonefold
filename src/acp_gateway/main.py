"""The gateway application factory (plan M6, design §0/§1).

``create_app`` wires the one chokepoint behind FastAPI: the SIF-native
``submit_intent`` tool, the kill control plane (``kill_api``), and the thin admin
UI (``admin_api``). Every route ends in the *same* ``Gateway.submit`` →
``enforce`` call (design §0) — the transports cannot diverge.

Identity is resolved by the **``IdentityProvider`` seam** (CS-021,
``acp_gateway.identity``) from the authenticated transport (the ``X-Actor-Id`` /
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

from acp_gateway.admin_api import ReplayableAudit, create_admin_router
from acp_gateway.identity import (
    IdentityProvider,
    IdentityRejected,
    SessionIdentityProvider,
    TransportCredential,
)
from acp_gateway.kill_api import create_kill_router
from acp_gateway.kill_service import KillService
from acp_gateway.transport import Gateway, SifNativeTransport
from acp_core.outbox import OutboxStore


class SubmitIntentBody(BaseModel):
    """The agent's intent — *what*, never *who* (invariant 3). Any ``actor`` /
    ``owner_id`` keys inside ``data`` are opaque parameters, never identity."""

    resource: str
    action: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


def create_app(
    gateway: Gateway,
    *,
    kill_service: KillService | None = None,
    audit: ReplayableAudit | None = None,
    outbox: OutboxStore | None = None,
    identity: IdentityProvider | None = None,
) -> FastAPI:
    app = FastAPI(title="ACP Gateway", version="0.1")
    sif = SifNativeTransport(gateway)
    # CS-021: identity enters through the seam ahead of the pipeline. The default
    # is the standalone built-in (transport-authenticated ids verbatim) — so an
    # unconfigured gateway behaves exactly as before; a credential verifier plugs
    # into the same slot without touching the route.
    identity_provider: IdentityProvider = identity or SessionIdentityProvider()

    if kill_service is not None:
        app.include_router(create_kill_router(kill_service))
    if audit is not None and outbox is not None:
        app.include_router(create_admin_router(audit=audit, outbox=outbox))

    @app.get("/tool-schema")
    def tool_schema() -> dict[str, Any]:
        """The single SIF-native tool schema, generated from the registry."""
        return sif.tool_schema

    @app.post("/submit_intent")
    def submit_intent(
        body: SubmitIntentBody,
        x_actor_id: str = Header(..., alias="X-Actor-Id"),
        x_session_id: str = Header(..., alias="X-Session-Id"),
        x_correlation_id: str | None = Header(None, alias="X-Correlation-Id"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict[str, Any]:
        # identity from the authenticated transport via the seam, NOT the body
        # (invariant 3, binding on every provider — CS-021).
        try:
            who = identity_provider.identify(
                TransportCredential(
                    actor_id=x_actor_id, session_id=x_session_id,
                    correlation_id=x_correlation_id or x_session_id,
                    credential=authorization,
                )
            )
        except IdentityRejected as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        result = sif.submit_intent(
            {"resource": body.resource, "action": body.action, "data": body.data},
            actor=who.actor, session=who.session,
        )
        return {
            "decision": result.decision.value,
            "rule": result.rule,
            "ticket": result.ticket,
            "output": result.output,
            "scopeApplied": list(result.scope_applied),
        }

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _ADMIN_HTML

    return app


# A deliberately tiny single-file console: a trace viewer, the approvals inbox,
# and a global kill button — enough to show intent → decision → effect, approve a
# held action, and halt a session (M6 DoD). Not a product UI.
_ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>ACP Gateway — console</title>
<style>
 body{font:14px system-ui,sans-serif;margin:2rem;max-width:60rem}
 h1{font-size:1.2rem} section{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}
 button{padding:.4rem .8rem;cursor:pointer} input{padding:.3rem}
 pre{background:#f6f6f6;padding:.6rem;border-radius:6px;overflow:auto}
 .kill{background:#b00020;color:#fff;border:0;border-radius:6px}
</style></head><body>
<h1>ACP Gateway — operator console</h1>

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
