# 06 — Keep your tools: interception without a rewrite

**The question this example answers:** examples 02–05 gave the agent one
SIF tool. But your agent already exists, with its own tools — and you were
promised you would not have to rewrite it. How does the gateway govern the
tools you already have?

**The answer, up front:** a **mapping** — one declared, reviewed YAML table
that gives each existing tool a meaning in the registry's vocabulary. The
gateway's proxy receives the old tool call, looks it up in that table
(a lookup, never interpretation), and runs the exact same pipeline every
other example uses. A tool with no entry is denied and audited. Your agent
does not change; only the URL its tools point at does.

The whole transformation is one line of thinking:

```
old call:  POST /tools/open_ticket   {"customer": "C1", "subject": "billing"}
mapping:   open_ticket -> Ticket.create,  customer -> customerId
becomes:   Ticket.create              {"customerId": "C1", "subject": "billing"}
           ... and from here it is example 02: authorize, scope, gates, audit
```

| File | Role | What it is |
|---|---|---|
| **`mappings.yaml`** | platform / policy team — **the new artifact** | the reviewed table: tool name → declared action, old arg names → declared data keys |
| `registry.yaml` / `policy.stele.yaml` | platform + policy author | unchanged in kind — note the policy never mentions tool names |
| `gateway_service.py` | infra engineer | example 02's service plus the proxy route (`POST /tools/{name}`) |
| **`agent.py`** | **agent developer** | your existing agent, verbatim: old tools, old argument names, **no Stonefold imports** |
| `main.py` | demo driver | runs everything over the wire, checks the audit |

---

## Step 1 (platform team) — declare the mapping in `mappings.yaml`

```yaml
mappings:
  - tool: lookup_customer
    resource: Customer
    action: read
  - tool: open_ticket
    resource: Ticket
    action: create
    argMap: { customer: customerId }
  - tool: export_crm
    resource: Customer
    action: export
```

Three things to read off this table:

1. **It is a lookup table, not a model.** At runtime the gateway translates
   tool name → declared action and renames argument keys — deterministically,
   the same way every time. Nothing in the enforcement path guesses what a
   tool "probably means"; a human declared it here, in advance.
2. **What's absent is load-bearing.** The agent also has a `run_sql` tool.
   It has no entry — so every call to it is **denied and audited**, never
   passed through. If your tool estate grows a new tool tomorrow, it stays
   refused until someone declares what it means.
3. **An LLM can draft this file for you.** Hand it your `tools/list` JSON
   and the registry, and let it propose the table (the registry generator in
   `spec/docs/06` does the same from SQL DDL or OpenAPI). Then a human
   reviews and signs the result — authoring may be assisted; enforcement
   stays a table lookup.

## Step 2 (infra engineer) — one addition to the service

`gateway_service.py` is example 02's service plus:

```python
proxy = MCPProxy(gateway, load_mappings())

@app.post("/tools/{tool}")
def call_tool(tool, args, ...headers...):
    return render(proxy.call_tool(tool, args, actor=..., session=...))
```

Same gateway object, same pipeline, same audit — the proxy is just a second
front door that translates before it submits. (One guard worth knowing:
a mapping flagged `free_form` — a raw-string pass-through like a real
`run_sql` — makes the proxy **refuse to start** unless explicitly
acknowledged. High-risk holes must be opened on purpose, in code review.)

## Step 3 (agent developer) — change nothing

`agent.py` is what it always was: four old tools, old argument names, one
base URL. No SIF, no `submit_intent`, no Stonefold imports.

## Run it

```bash
# terminal 1:
uvicorn --app-dir guide/06_keep_your_tools gateway_service:app --port 8099
# terminal 2:
python guide/06_keep_your_tools/agent.py http://localhost:8099

# or everything in one command:
python guide/06_keep_your_tools/main.py
```

Expected output:

```
agent: my 4 old tools, my old argument names — nothing rewritten
agent: lookup_customer    -> allow  rows=2
agent: open_ticket        -> allow
agent: export_crm         -> deny  rule=default-deny
agent: run_sql            -> deny  rule=unmapped-tool

driver: 4 audit records — the mapping translated, the same pipeline decided
```

## What to notice

1. **The policy never saw a tool name.** It allows `observe: [Customer]`
   and `record: [Ticket]` — semantics, not syntax. Rename `open_ticket` to
   `create_ticket` tomorrow: one line changes in `mappings.yaml`, zero lines
   in the policy.
2. **Two refusals, two different walls.** `export_crm` *was* mapped — the
   policy judged the declared action and refused it (`default-deny`).
   `run_sql` was never declared — refused before any policy ran
   (`unmapped-tool`), same as example 01's undeclared name.
3. **This is the adoption ramp, not a downgrade.** The mapping work *is*
   the registry review: the same entries later generate the one-tool SIF
   schema of example 02, so migrating agent by agent throws nothing away.
   The full ramp is `docs/16-incremental-adoption.md`; the design reasoning
   is `spec/docs/17-interception-mapping.md`.
