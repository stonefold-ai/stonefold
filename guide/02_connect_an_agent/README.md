# 02 — Connect YOUR agent

**The question this example answers:** you already have an agent — an LLM
tool-use loop. What changes in *your* code when Stonefold stands between it
and your systems?

**The answer, up front:** your agent loses its pile of tools and gains
exactly **one** — `submit_intent`, whose schema is generated from the
registry. Identity moves out of the model's hands into your transport. That
is the entire agent-side integration; everything else in this example is
other people's jobs.

> **The one call that lives in your code.** Strip everything away and the
> whole integration is this raw HTTP exchange — request out, decision back:
>
> ```
> POST http://gateway:8099/submit_intent
> X-Actor-Id: rep-7            <- set by YOUR platform's auth, not the model
> X-Session-Id: s1
> {"resource": "Ticket", "action": "create", "data": {"subject": "hi"}}
>
> -> {"decision": "allow", "ticket": null, "output": {...},
>     "reasonCode": "", "retryClass": null, ...}
> ```
>
> Your program receives that response and decides what to do next — retry
> with a fix, wait, move on. That loop is *yours*; Stonefold never runs your
> agent. And note what this implies: **any program can make this call** —
> an LLM tool loop, a cron job, a plain script, another service. The gateway
> doesn't know or care what is calling; it governs the *action*, not the
> caller's architecture.

| File | Role | What it is |
|---|---|---|
| **`agent.py`** | **agent developer — this is YOUR file** | the complete agent side: one HTTP client, one tool, an LLM stand-in. **Imports nothing from Stonefold.** |
| `registry.yaml` | platform / policy team | what exists (from example 01) |
| `policy.stele.yaml` | policy author | what this agent may do |
| `gateway_service.py` | infra engineer | the real HTTP service (`uvicorn gateway_service:app`) |
| `main.py` | nobody (demo driver) | starts the service, runs the agent, checks the wire |

---

## Step 1 (agent developer) — `agent.py`, your side of the wall

Create `agent.py`. Notice the imports:

```python
import json, sys, urllib.request
```

That's all. **No Stonefold imports.** Your agent's world is one base URL.

**1a — the one tool.** A small client that fetches the tool schema and
submits intents:

```python
class GatewayTool:
    def __init__(self, base_url, *, actor, session):
        ...
    def schema(self):          # GET  /tool-schema
    def submit_intent(self, payload):  # POST /submit_intent
```

Why `actor` and `session` are constructor arguments: **who the agent acts as
is decided by the platform that runs it** — your service's authentication —
and travels in the `X-Actor-Id` / `X-Session-Id` headers. The model never
sees or chooses them. Anything identity-shaped the model writes into `data`
is an inert string (the demo proves it: step 2 smuggles `"actor": "admin"`
into the payload and the audit still names `rep-7`).

**1b — the model.** `scripted_llm` stands in for your LLM call:

```python
def scripted_llm(step):
    return {"resource": "Customer", "action": "read", "data": {}}
```

To use a real model: give it the schema from `tool.schema()` as its only
tool, and pass each tool call's arguments to `tool.submit_intent(...)`. The
dicts in this file are byte-for-byte what a model emits against that schema.
(A complete real-LLM loop — Claude/OpenAI providers, retries, an inbox —
is `src/stonefold_ap_demo/agent.py`.)

**1c — read the refusals.** `submit_intent` returns the decision *and* the
v0.6 feedback channel:

```json
{"decision": "deny", "reasonCode": "no-match", "retryClass": "terminal", ...}
```

`retryClass` tells your loop what to do: `retryable` — fix the intent and
resubmit; `terminal` — stop, nothing to fix; `escalate` — stop and hand it
to a human (the class a `hold` usually carries — example 03 shows one).
On a `hold` decision itself, the agent's move is to wait or move on; a human
owns the ticket now. Example 05 runs a full convergence loop on this.

## Step 2 (infra engineer) — `gateway_service.py`, the service

Create `gateway_service.py`. It is an ordinary FastAPI service:

1. **Load the reviewed artifacts** (`registry.yaml`, `policy.stele.yaml`,
   the JSON Schema). A policy with lint errors refuses to load — the service
   won't start permissive.
2. **Wire the stores from the environment**, like any deployment:
   `DATABASE_URL` set → the append-only Postgres `audit_log` (schema created
   idempotently at boot); unset → in-memory with a printed notice.
3. **Build the chokepoint and the app**: `Gateway(...)` +
   `create_app(gateway, audit=..., outbox=...)`, which exposes
   `/tool-schema`, `/submit_intent`, and the operator surfaces
   (`/admin/trace/{id}`, `/admin/approvals`, …).

## Step 3 — run it, for real

```bash
# real infra (optional here, required from example 04):
docker compose -f guide/docker-compose.yml up -d
export DATABASE_URL=postgresql://stonefold:stonefold@localhost:5433/stonefold
# Windows (PowerShell): $env:DATABASE_URL = "postgresql://stonefold:stonefold@localhost:5433/stonefold"

# terminal 1 — the service:
uvicorn --app-dir guide/02_connect_an_agent gateway_service:app --port 8099

# terminal 2 — YOUR agent:
python guide/02_connect_an_agent/agent.py http://localhost:8099
```

Or by hand, the way any HTTP client sees it:

```bash
curl http://localhost:8099/tool-schema
curl -X POST http://localhost:8099/submit_intent \
  -H "Content-Type: application/json" \
  -H "X-Actor-Id: rep-7" -H "X-Session-Id: s1" \
  -d '{"resource": "Customer", "action": "read", "data": {}}'
curl http://localhost:8099/admin/trace/s1        # the audit, over the wire
```

Or everything in one command (starts the service as a subprocess, runs the
agent over real localhost HTTP, verifies, shuts down):

```bash
python guide/02_connect_an_agent/main.py
```

Expected output:

```
agent: got 1 tool (submit_intent), resources = ['Customer', 'Ticket']
agent: Customer.read   -> allow
agent: Ticket.create   -> allow
agent: Ticket.create   -> allow   <- smuggled 'actor', 'role' in data: inert strings, not identity
driver: 3 audit records, every one names actor rep-7
```

The third line is the injected attack from step 1a — and yes, it is
*allowed*: creating a ticket is within policy, so the intent goes through.
What the injection did **not** do is change who acted. The smuggled
`"actor": "admin"` landed in `data`, where it is an ordinary string; the
audit (last line) names the transport identity, `rep-7`, for every record.

## What to notice

1. **The agent file is small and yours.** Securing your agent did not mean
   rewriting it — it meant taking its direct tools away and handing it one.
2. **A hallucinated resource can't even be expressed** — `resource` is an
   enum in the tool schema, generated from the registry.
3. **Identity is not the model's to claim.** The smuggled `"actor": "admin"`
   landed as data; the audit names the transport identity.
4. **Already on MCP, or attached to your existing tools?** You don't have
   to touch your agent at all: interception mode maps each existing tool
   call to a declared action and enforces it; unmapped tools are denied.
   Example [06](../06_keep_your_tools/README.md) runs exactly that.
