# 01 — Hello, gateway

**What you build:** the smallest possible Stonefold deployment — a registry,
a policy, and one `enforce()` call — and you watch default-deny work.

> **Scope note — this is the gateway's inside.** `RawCall` and `enforce()`
> are the gateway's *internal* machinery, shown here so you understand what
> the service does. **An agent never calls them and never runs in the
> gateway's process**: from example [02](../02_connect_an_agent/README.md)
> onward the gateway is a real HTTP service on a port, and the agent is a
> separate process that speaks `POST /submit_intent` over the network —
> nothing else.

**Who owns which file** (this split is the point of the whole project):

| File | Role | In production |
|---|---|---|
| `registry.yaml` | platform / policy team | a reviewed artifact in its own repo; the catalogue of everything that *exists* |
| `policy.stele.yaml` | policy author (security / compliance) | a reviewed, signed rulebook — **no code, ever** |
| `main.py` | infra engineer | the gateway service's startup wiring (example 02 shows it as a real service) |

---

## Step 1 — the registry (`registry.yaml`)

Create `registry.yaml`. This declares everything that can even be *said*:

```yaml
connectors: [in_memory]

resources:
  Note:
    connector: in_memory
    actions:
      read:   { kind: observe }
      create: { kind: record }
```

Why it looks like this:

- **`connectors`** names the adapters the gateway may execute through. Here
  one in-memory adapter stands in for your database; production declares
  `sql`, `http`, `email`, or your own.
- **`resources` → `actions`** is the whole vocabulary. Each action declares
  its **kind** — `observe` (look), `record` (write internal data), plus
  `effect`, `transition`, `assess` in later examples. The kind is what
  policies reason about.
- Anything *not* in this file — a resource, an action — is **unsayable**.
  The gateway refuses it before any policy runs. That's your outermost wall,
  and it's why this file is a reviewed artifact, not developer code.

## Step 2 — the policy (`policy.stele.yaml`)

Create `policy.stele.yaml`. This is the rulebook, written in Stele:

```yaml
apiVersion: stele/v0.1
agent: hello-agent

allow:
  - observe: [Note]
```

Why it looks like this:

- **`agent`** binds the policy to one agent identity.
- **`allow`** lists permissions by *kind*: this agent may `observe` the
  `Note` resource. Full stop.
- Everything else is implicit: the gateway's ground state is **default
  deny**. `create` is not listed → denied. There is no "deny by default"
  switch to remember; there is no other mode.

A security reviewer reads this file in seconds. That's the design goal: the
policy is the artifact you argue about and sign, and nobody hides logic in it
because there is nowhere to hide logic — it's declarative.

## Step 3 — the gateway wiring (`main.py`)

Create `main.py`. Walk the pieces in order:

**The imports.** Everything the gateway service needs at boot:

```python
from stonefold_core import (
    Actor, Connectors, Decision, InMemoryAuditSink,
    RawCall, Session, enforce, load_policy, load_registry,
)
from stonefold_connectors import InMemoryConnector
```

- `load_registry` / `load_policy` turn the two YAML files into validated,
  indexed objects. `load_policy` also runs the **linter** — a policy with
  errors refuses to load; the gateway would rather not start than start
  permissive.
- `Connectors` + `InMemoryConnector`: the adapter that actually touches the
  system behind a resource. Connectors execute and apply scope — they hold
  **no policy logic**.
- `InMemoryAuditSink`: every decision writes a record here. Production uses
  the Postgres sink; the interface is identical.
- `Actor` / `Session`: **who** is acting. These come from *your* transport's
  authentication, per request — never from anything the agent says.
- `enforce`: the pipeline itself. One call per attempted action, and every
  call ends in an audited decision.

**Startup** — load, validate, wire:

```python
registry = load_registry(load_yaml("registry.yaml"))
policy   = load_policy(load_yaml("policy.stele.yaml"), registry, schema=schema)
world    = InMemoryConnector({"Note": [{"id": "N1", "text": "hello"}]})
audit    = InMemoryAuditSink()
```

**Per request** — one `enforce()` call:

```python
result = enforce(
    RawCall(resource="Note", action="read", data={}),
    actor, session,
    registry=registry, audit=audit, policy=policy, connectors=connectors,
)
```

## Run it

```bash
python guide/01_hello_gateway/main.py
```

Expected output:

```
read (allowed by policy)   -> allow rows=[{'id': 'N1', 'text': 'hello'}]
create (not in the policy) -> deny  rule=default-deny
undeclared name            -> deny  rule=unknown-action
audit log: 3 records, refusals included
```

## What to notice

1. **Two different refusals.** `create` was *declared but not allowed* →
   `default-deny`. `Database.dropAllTables` was *never declared* →
   `unknown-action`, refused before the policy even ran.
2. **The refusals are on the record.** Auditing failed attempts is not a
   nice-to-have — the attempt itself is the security signal.
3. **Your agent is deliberately absent.** Nothing in this example is agent
   code — that's the point of the architecture: the walls exist before and
   independently of any agent. If you came here to secure *your* agent, go
   straight to [02](../02_connect_an_agent/README.md): it shows exactly what
   changes in your code (one file, no Stonefold imports) and what never
   touches it again.
