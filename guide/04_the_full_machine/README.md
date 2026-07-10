# 04 — The full machine: real infra, staged effects, humans in charge

**The question this example answers:** what does a production-shaped
deployment look like — real Postgres and Redis, a background dispatch
worker, an approvals inbox, a kill switch — and what does each seat
(agent, operator, infra) actually do?

| File | Role | What it is |
|---|---|---|
| `registry.yaml` / `policy.stele.yaml` | platform + policy author | now govern an **effect** (money moves) |
| `gateway_service.py` | infra engineer | the full wiring: env-driven Postgres/Redis stores + the dispatch worker |
| **`agent.py`** | **agent developer** | same single HTTP call as ever — an *allowed* effect returns a **ticket**, not "done" |
| **`operator_console.py`** | **operator** | approvals + kill, over plain HTTP — pluggable into any console/chat-ops |
| `main.py` | demo driver | plays both seats over the wire and verifies through the audit API |

---

## Step 1 (infra engineer) — the real infrastructure

The gateway's entire durable substrate is two containers:

```bash
docker compose -f guide/docker-compose.yml up -d
export DATABASE_URL=postgresql://stonefold:stonefold@localhost:5433/stonefold
export REDIS_URL=redis://localhost:6380/0
```

- **Postgres** holds the **outbox** (staged effects — the worker claims rows
  with a real `SELECT … FOR UPDATE`, which is what makes the kill-versus-
  dispatch race actually closed), the **append-only `audit_log`**, and the
  **kill orders**.
- **Redis** holds the sliding-window **rate counters**.
- Without the env vars, every store falls back to in-memory and the service
  prints a notice — fine on a laptop, not a deployment.

## Step 2 (infra engineer) — `gateway_service.py`

Three additions over example 03, each explained in the file:

1. **Env-driven stores** (`_stores()`): schemas created idempotently at
   boot; the request path and the worker get **separate connections**,
   exactly as a separate worker process would.
2. **Freshness**: `FreshnessConfig()` stamps a decision TTL on every staged
   row — a decision is only trusted for a bounded time.
3. **The dispatch worker**: a background thread claiming one row at a time;
   inside each claim it re-checks **kill → TTL → volatile gates** before the
   connector sends, and the idempotency key on every row makes retries safe.

## Step 3 (agent developer) — what changes in YOUR agent: almost nothing

Same one call as example 02. The only new thing to understand is the shape
of success for an **effect**:

```json
{"decision": "allow", "ticket": "act_2610644d...", ...}
```

`allow` means **accepted and staged** — money moves when the worker
dispatches it (milliseconds later, normally). `hold` means a ticket is
waiting for a human your agent cannot imitate; the agent's move is to wait
or move on. As always: any program can make these calls, not just an LLM.

## Step 4 (operator) — `operator_console.py`

The human control plane is plain HTTP on the same service:

```bash
python operator_console.py http://localhost:8099 list            # the inbox
python operator_console.py http://localhost:8099 approve act_... manager-1
python operator_console.py http://localhost:8099 reject  act_... manager-1
python operator_console.py http://localhost:8099 kill s1        # halt a session
python operator_console.py http://localhost:8099 lift kill_...
```

`GET /admin/trace/{sessionId}` replays a whole run from the audit — the
driver uses it as its only source of truth, because it is the only truthful
window an outside process has.

## Run it

```bash
python guide/04_the_full_machine/main.py     # both seats, over the wire
```

Expected output:

```
agent[s1]: pay    400 -> allow ticket=act_...
driver: worker dispatched the $400 (audit outcome=success)
agent[s1]: pay   5000 -> hold ticket=act_...
driver: operator approved; worker dispatched the $5000
agent[s1]: pay   9000 -> hold ticket=act_...
driver: operator rejected; nothing dispatched
agent[s1]: pay     50 -> halt
agent[s2]: pay     50 -> allow ticket=act_...
driver: kill halted s1; lift restored; s2 unaffected
```

## What to notice

1. **There is a durable moment between "decided" and "done."** That gap —
   the staged row — is where approval, rejection, TTL expiry, and the kill
   switch all live. Inline execution would make every one of them a race.
2. **Rejection is silence.** The $9,000 never produced a `success` record —
   the driver asserts on its absence, over the audit API.
3. **The kill is scoped.** Session `s1` halted; `s2` kept working; lifting
   restored `s1`. Every one of those transitions is itself audited.
