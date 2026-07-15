# The Stonefold developer's guide

*You cloned the repo. Now what?*

Stonefold is a deterministic gateway that stands between an AI agent and
the systems it can act on: every attempted action is checked against a
reviewed policy, staged, audited, and — where the policy says so — held for
a human. This guide shows you, hands on, what that costs to adopt (one HTTP
call in your agent) and what it buys.

Six step-by-step tutorials, each in its own directory with its own README
and complete files. Every example's `main.py` is executed by the test suite
on every commit (`tests/test_guide_examples.py`), so the guide cannot drift
from the code it teaches.

<a href="architecture.svg"><img src="architecture.svg" alt="How the pieces fit: the agent's only path is a typed submit_intent over HTTP into the deterministic gateway (resolve, authorize, scope, gates, kill, stage, audit); gates call your registered checks/hooks/predicates; allowed effects stage in the outbox and a dispatch worker sends them through your connectors, with the operator holding approvals and the kill switch; v0.6 adds the obligation registry the gateway queries, reserves, consumes and releases; everything lands in the append-only audit log" width="1080"></a>

<sub>Click the diagram to open it full size.</sub>

## Who builds what

Stonefold's file layout mirrors how real teams split the work — every
example keeps these roles in separate files, because they are separate jobs:

| Role | Owns | Writes |
|---|---|---|
| **Agent developer** (probably you) | `agent.py` | one HTTP call: `POST /submit_intent` with the platform-set identity headers. **No Stonefold imports.** Any program can make this call — an LLM loop, a cron job, a script. |
| **Policy author** (security / compliance) | `registry.yaml`, `policy.stele.yaml` | declarative YAML, reviewed and signed. **Never code.** |
| **Function developer** (domain team) | `functions.py`, `erp_adapter.py` | small deterministic functions the gateway calls by declared name |
| **Infra engineer** (platform) | `gateway_service.py`, `docker-compose.yml` | the HTTP service: env-driven stores, worker, `uvicorn gateway_service:app` |
| **Operator** (on-call, approvers) | `operator_console.py` | approvals + kill, over plain HTTP admin endpoints |

If you are one developer evaluating this on a laptop: you play all five
roles. The split is about files and review boundaries, not headcount — it
exists so that when a team does adopt this, each file already belongs to
the right owner.

None of it ties you to Python. The agent is any program, in any language,
that can make an HTTP request; the policy and registry are YAML. Python
appears in the other seats only because this guide runs the **reference**
gateway, which happens to be written in Python — the spec is
language-neutral, and a gateway implemented in any other language presents
the same HTTP surface and proves it with the conformance kit
(`spec/docs/12-conformance-tck.md`).

The agent talks to the gateway **over the network** — a real port, a real
HTTP call, two separate processes. Nothing agent-side ever runs inside the
gateway (example 01 is the single exception: it opens the gateway's own
internals so you understand what the service does, and contains no agent at
all).

## Setup

You need Python 3.11+ and, from example 02 on, Docker.

```bash
git clone --recurse-submodules https://github.com/stonefold-ai/stonefold
cd stonefold
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q -m "not integration"                   # verify: green

# real infrastructure (optional from example 02 on, required from 04):
docker compose -f guide/docker-compose.yml up -d
export DATABASE_URL=postgresql://stonefold:stonefold@localhost:5433/stonefold
export REDIS_URL=redis://localhost:6380/0
```

On Windows (PowerShell), the environment variables are set like this:

```powershell
$env:DATABASE_URL = "postgresql://stonefold:stonefold@localhost:5433/stonefold"
$env:REDIS_URL = "redis://localhost:6380/0"
```

Without the env vars every service falls back to in-memory stores and says
so — fine on a laptop, not a deployment. There is no PyPI package yet; you
install from the checkout. The `spec/` submodule must be populated
(`git submodule update --init` if it's empty).

## The tutorials

| # | Tutorial | You learn |
|---|---|---|
| [01 — Hello, gateway](01_hello_gateway/README.md) | the registry, the simplest policy, default deny, the audit — the gateway's *inside* (no agent here, on purpose) |
| [02 — Connect YOUR agent](02_connect_an_agent/README.md) | the one HTTP call that lives in your agent, the tool schema, identity from the transport — the real service on a real port |
| [03 — Registered functions](03_registered_functions/README.md) | the code the gateway calls: scope predicates, content hooks, precondition checks (incl. the v0.6 three-valued *hold*) |
| [04 — The full machine](04_the_full_machine/README.md) | Postgres outbox + Redis counters, the dispatch worker, approvals and the kill switch over the operator API |
| [05 — Obligation matching](05_obligation_matching/README.md) | v0.6: the ERP adapter, `requireMatch`, reserve→consume→release, and the agent loop that converges on `retryClass` |
| [06 — Keep your tools](06_keep_your_tools/README.md) | interception mode: your existing agent, unchanged — a reviewed mapping table translates its old tool calls into declared actions; unmapped tools are denied |

Read them in order the first time; each README states which files changed
since the previous example and which role owns the change.

## The vocabulary, up front

Each term is explained where it first appears; this table is here so a
skim survives contact with the prose. (The full glossary is
`spec/docs/08-glossary.md`.)

| Term | Meaning |
|---|---|
| **intent** | one attempted action, as JSON: resource, action, data |
| **kind** | an action's category (`observe`, `record`, `effect`, `transition`, `assess`) — what policies reason about |
| **gate** | one deterministic check in the pipeline (value limit, rate, approval, match, …) |
| **decision** | the gateway's verdict on an intent: `allow`, `deny`, `hold`, or `halt` |
| **hold** | the decision that parks an action for a named human; nothing executes meanwhile |
| **staged / outbox** | an allowed effect is written to a durable table first; a worker dispatches it — the gap is where approval, TTL, and kill live |
| **settle** | the dispatch worker's final write after the effect executed: outcome + audit, in one transaction |
| **fail closed** | any dependency failure is treated as a deny/halt, never as a pass |
| **`reasonCode` / `retryClass`** | machine-readable refusal feedback: which rule refused, and whether the agent should fix-and-resubmit (`retryable`), stop (`terminal`), or surface to a human on its side (`escalate`) |
| **obligation** | an external record (e.g. a purchase order line) a payment must match and spend — v0.6, example 05 |

## Where to go next

- **`docs/02-implementation-design.md`** — how the pipeline works inside.
- **`spec/docs/01-RFC-agent-control-policy.md`** — Stele, normatively: all
  fifteen gates, the condition language, the linter rules.
- **`spec/examples/*.stele.yaml`** — five worked policies (payments,
  clinical, legal, support, defence) that load and lint clean.
- **`docs/05-demo-spec.md` + `demo/`** — the full real-LLM demo with UI,
  approvals inbox, live trace, and kill switch.
- **`spec/docs/12-conformance-tck.md`** — certify a gateway you wrote
  yourself, in any language, against the same spec.
