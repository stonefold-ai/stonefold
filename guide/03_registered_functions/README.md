# 03 — Registered functions: the code the gateway calls

**The question this example answers:** built-in gates compare intent fields
against constants. Where does *your domain knowledge* go — "is this order
active?", "does this payload leak?", "whose rows are these?" — and how does
the gateway call it?

**The answer:** three kinds of small deterministic functions, written by the
function developer in one file, **declared by name** in the registry,
**used by name** in the policy, and **registered** on the engine by the
infra service. Four files, four owners:

| File | Role | What changes vs example 02 |
|---|---|---|
| `registry.yaml` | platform / policy team | now DECLARES the function names (and each check's hold capability + reason codes) |
| `policy.stele.yaml` | policy author | now USES the names in `scope:` and `gates:` — still zero Python |
| **`functions.py`** | **function developer — the new file** | the three implementations; no gateway wiring |
| `gateway_service.py` | infra engineer | registers the implementations + resolves target facts per request |
| `agent.py` | agent developer | **unchanged shape** — the agent never knows these functions exist |

---

## Step 1 (platform team) — declare the names in `registry.yaml`

```yaml
scopePredicates: [ownedBy]
contentHooks: [no.secrets]
preconditionChecks:
  - targetIsActive
  - name: inventoryAvailable
    holdCapable: true
    reasonCodes:
      stock-uncertain: escalate
```

Why declaration matters: the **linter** refuses any policy that references an
undeclared name, and the **gateway** refuses behaviour outside the
declaration — a check that holds without `holdCapable: true`, or holds
without a declared reason code, is treated as an implementation error and
fails closed. The declaration is the contract your reviewer signs.

## Step 2 (policy author) — use the names in `policy.stele.yaml`

```yaml
scope:
  Note: ownedBy(actor)
gates:
  Note.create:
    contentCheck: no.secrets
  ship:
    precondition:
      checks: [targetIsActive, inventoryAvailable]
      resolvers: role:warehouse-lead
```

`resolvers:` names who may release a hold this gate raises — a role at your
identity layer, like approvers.

## Step 3 (function developer) — implement them in `functions.py`

Create `functions.py`. The only Stonefold imports a function developer needs:

```python
from stonefold_gates.base import CheckResult, GateContext, check_hold
```

Then the three signatures, one per kind:

```python
def no_secrets(content) -> bool: ...                       # content hook
def target_is_active(ctx: GateContext) -> bool: ...       # check (2-valued)
def inventory_available(ctx) -> bool | CheckResult: ...    # check (may HOLD)
```

The house rules the gateway holds you to:

1. **Deterministic.** Same inputs, same answer. No model calls, ever.
2. **Read from your systems, never the payload.** `ctx.env.resource` carries
   the target's facts, resolved by the *gateway* (step 4). If the agent could
   supply the data you check, it would be checking itself.
3. **Return your verdict; never raise to say no.** A raised exception means
   "my dependency is down" — the gateway fails closed for you.
4. **A hold carries a declared code.** `check_hold("stock-uncertain", ...)`
   works because the registry declared that code. A code-less hold resolves
   fail (an uninformative interruption is worse than a deny).

When are they called? Content hooks and checks run at **decision time**;
checks run **again inside the dispatch claim** for staged effects (the world
may have moved between decision and dispatch — example 04); scope predicates
run on **every read** and as a pre-check on targeted effects.

## Step 4 (infra engineer) — register them in `gateway_service.py`

Two additions over example 02:

```python
engine = DefaultGateEngine(
    registry,
    hooks=ContentHookRegistry(functions.HOOKS),
    preconditions=functions.CHECKS,
)
scopes = make_scope_resolver(policy, ScopeRegistry({
    "ownedBy": AttributeScope("ownedBy", "owner_id", "id"),
}))
```

…and the **env factory** — for each request the gateway resolves the
target's current row from the system of record, so `ctx.env.resource` is the
world's answer, not the agent's:

```python
def env_factory(raw: RawCall) -> RequestEnv:
    row = look_up(raw.resource, raw.data.get("id"))   # YOUR system of record
    return RequestEnv(resource=row)
```

## Run it

```bash
# terminal 1:
uvicorn --app-dir guide/03_registered_functions gateway_service:app --port 8099
# terminal 2:
python guide/03_registered_functions/agent.py http://localhost:8099

# or everything in one command:
python guide/03_registered_functions/main.py
```

Expected output (the agent's view, over the wire):

```
agent: Note.read          -> allow  rows=1 (of 2 in the table)
agent: Note.create        -> deny  rule=gate:contentCheck
agent: Order.ship O1     -> allow
agent: Order.ship O2     -> deny  code=gate:precondition class=terminal
agent: Order.ship O3     -> hold  code=stock-uncertain class=escalate
```

## What to notice

1. **The agent didn't change.** Same one tool as example 02; the missing row,
   the blocked payload, and the hold all happened below it.
2. **Three verdicts, three worlds.** O1 is active and stocked → allow. O2 is
   cancelled → deny (fail closed). O3's stock is *readable but ambiguous* →
   **hold**, queued for `role:warehouse-lead` — a human owns it, the agent's
   `retryClass` says `escalate`, and nothing dispatched.
3. **The declaration is load-bearing.** Delete `holdCapable: true` from the
   registry and restart: the O3 hold now resolves **deny** — the gateway
   refuses undeclared behaviour rather than trusting the check's good
   manners.
