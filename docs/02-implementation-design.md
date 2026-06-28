# ACP Implementation Design — Engineering Companion to the RFC

*The Agent Control Policy RFC says **what** a policy means. This paper says **how** the gateway actually executes it — data structures, control flow, where state lives, how each gate is computed, and (worked in full) how the kill-switch operates. It is written so an engineer can build from it and a reviewer can tell whether a given claim in the RFC is mechanically real. Throughout, **Design notes** flag specific decisions, trade-offs, and failure modes to engineer against.*

Reference stack (pinned in `docs/03`): a **Python** gateway (FastAPI + pydantic), PostgreSQL for durable state (audit, outbox, approvals, kill orders), and Redis for hot counters and kill propagation. None of it is load-bearing — swap equivalents freely. **The code snippets below are illustrative pseudocode** (written in a record/`switch` style for clarity); realise them in the pinned Python stack — records → `pydantic`/`dataclass` models, sealed interfaces → `typing.Protocol` + `enum`, the `switch` in §10 → a tree-walk over the AST. The *mechanism* is what matters, not the syntax.

---

## 0. The single most important implementation fact

Everything below depends on one property: **the gateway is the only path from the agent to any connector.** Every guarantee in the RFC — scope, gates, kill — is really a check performed at that one chokepoint. So the first job of an implementation is to *physically guarantee the chokepoint*, and the second is to make the checks at it correct and fast.

If an agent has any route to a side effect that does not pass through the gateway, none of the RFC applies to that route. This is why §1 (topology) comes before everything else.

---

## 1. Where the gateway runs, and how actions are intercepted

There are two integration modes (RFC §3). They differ entirely in *how the action reaches the gateway*, which determines coverage.

### 1.1 SIF-native mode
The agent is given exactly **one** tool, `submit_intent`, whose schema is generated from the registry (enum-injected resource/action names). The agent can emit nothing else. The gateway *is* the executor of that tool.

```
LLM --tool_call: submit_intent({op, resource, action, data})--> Gateway --> connector
                                                          (returns tool_result)
```
Coverage is **structural**: there is no other tool, so there is no other path. This is the strong tier.

### 1.2 Interception mode
The agent keeps its existing tools/MCP servers, but its tool traffic is routed **through** the gateway, which speaks the same protocol on both sides — an **MCP/tool proxy**.

```
LLM --tool_call--> [Gateway proxy] --(if allowed)--> real MCP server / tool --> system
                         |  intercepts, maps the call to an ACP action,
                         |  enforces, forwards or refuses, logs
```
Technically the gateway is a reverse proxy for the tool transport: for HTTP/SSE-based MCP it terminates the agent's connection and holds upstream connections to the real servers; for stdio MCP it sits as a middleware process. Each intercepted tool call is **mapped to an ACP action** via a per-tool mapping (`tool name + args  →  kind/resource/action/data`).

> **Design note.** Interception's coverage is only as good as the mapping and the routing. Two failure modes to engineer against: (1) a tool the gateway doesn't know about (unmapped) — policy: **unmapped ⇒ deny** by default, never pass-through; (2) network paths that bypass the proxy — must be closed at deployment (egress policy / the agent runtime only has the gateway endpoint). There should be a "coverage check" that fails startup if the agent has any configured tool endpoint that isn't the gateway.

> **Design note.** Be explicit in the product: interception gives "stop/bound/log any action that flows through the gateway"; it does **not** give the SIF-native "no escape hatch" property, because a mapped tool could itself be a raw `run_sql`. The mapping layer should flag tools whose arguments are free-form strings as *high-risk pass-throughs* and require explicit acknowledgement.

---

## 2. Runtime objects (the data model)

The pipeline passes a small set of typed objects. Concretely:

```java
// Immutable description resolved from the registry for the attempted action
record ResolvedAction(
    Kind kind,                 // OBSERVE, ASSESS, RECORD, EFFECT, TRANSITION
    String resource,           // "Customer"
    String action,             // "sendEmail" | null for generic CRUD
    Map<String,Object> data,   // parameters the agent supplied (typed)
    Attributes attrs,          // reversibility, emission, operativeForce, resultSensitivity, explainability
    Connector connector        // which adapter will fulfil it
) {}

record Actor(String id, Set<String> roles, Map<String,Object> claims) {}  // from session, NOT the model

enum Decision { ALLOW, HOLD, DENY, HALT }

record GateResult(String gate, Outcome outcome, String reason) {}  // PASS | FAIL | HOLD

record EvalResult(Decision decision, List<GateResult> gates, String rule) {}
```

The **registry** is loaded once at startup (and on change) into an in-memory, indexed structure: `resource → {actions → attributes, lifecycle states, connector}`. Resolution is an O(1) map lookup; an unknown name short-circuits to DENY before any policy runs.

---

## 3. The enforcement pipeline (the spine)

This is the literal control flow for one action. It is the implementation of RFC §12.

```java
EvalResult enforce(RawCall call, Actor actor, Session s) {
  // 1. RESOLVE
  ResolvedAction a = registry.resolve(call);          // unknown -> throw -> DENY + audit
  // 2. AUTHORIZE  (compiled matcher, deny-wins, default deny)
  if (policy.denies(a))      return terminal(DENY, "deny-rule", a, actor);
  if (!policy.allows(a))     return terminal(DENY, "default-deny", a, actor);
  // 3. SCOPE  (attach predicate; resolved from actor, never from model)
  ScopePredicate scope = policy.scopeFor(a.resource(), actor);
  // 4. GATES  (cheap & deterministic first; approval last)
  List<GateResult> g = gateEngine.evaluate(a, actor, s, scope);
  if (g.anyFail())   return terminal(DENY, firstFail(g), a, actor, g);
  // 5. KILL  (see §8 — checked here AND again at dispatch)
  if (killState.matches(a, s, actor)) return terminal(HALT, "kill", a, actor, g);
  if (g.anyHold())   return hold(a, actor, scope, g);   // -> staged, await approval (§7)
  // 6. EXECUTE  (effects staged via outbox; reads/records may run inline)
  return execute(a, actor, scope, g);                   // -> ALLOW + audit + result
}
```

Key implementation points:
- Steps 1–5 are pure and fast (in-memory + a couple of store reads). **No model is invoked anywhere in here.**
- The function is total: every path ends in an audited terminal decision (RFC §11 requires a record for *every* outcome, including refusals).
- `terminal(...)` and `hold(...)` both write the audit record before returning.

> **Design note.** The ordering of step 4 vs 5 matters. Killing *before* gates would waste no work, but killing *after* gates means the audit shows "this would have passed/failed, and then was halted," which is better forensics. The adopted compromise: a **cheap global kill pre-check** at the very top (is the whole agent/session killed?) to short-circuit, plus the **authoritative per-action kill check at step 5** and again at dispatch. So kill is effectively checked three times; that's deliberate (see §8.4).

---

## 4. Authorization: compiling allow/deny to a matcher

Parsing YAML per request is too slow and too error-prone. At policy load the gateway **compiles** `allow`/`deny`/`scope`/`gates` into an indexed structure:

```
Map<(Kind, Resource, ActionOrStar), Rule>   // for allow and deny separately
```

Matching an action is then: look up `(kind, resource, action)`, then `(kind, resource, *)`, then `(kind, *, *)` — most-specific first. `deny` is consulted first and wins unconditionally (RFC §6.2). `extends` fragments are merged at compile time with the "more restrictive wins / deny wins" rule, so runtime sees one flattened, validated policy.

> **Design note.** Compilation is also where the **linter** (RFC §13) runs. A policy that fails validation must not load — the gateway should refuse to start with a bad policy rather than fall back to defaults, because a silently-degraded policy is the classic way a control plane fails open by accident.

---

## 5. Scope injection: turning a predicate into a real filter

This is the implementation of the RFC's "enforcement below the model." The agent's intent contains **no** scope; the gateway adds it.

1. The actor identity comes from the **session/transport** (an authenticated header / token verified by the gateway), never from the agent's payload. The agent literally cannot set `actor`.
2. `policy.scopeFor(resource, actor)` returns a **named, registered** `ScopePredicate` (not a free expression) — e.g. `assignedToCurrentUser`.
3. The predicate is realised **per connector** as a constraint the connector applies. Concretely:
   - **SQL connector:** the predicate compiles to an additional `WHERE` clause appended by the connector *after* translation: `... AND owner_id = :actorId`. The agent's intent never contained `owner_id`.
   - **HTTP/REST connector:** the predicate becomes a mandatory query/path parameter or a server-side filter the connector injects.
   - **Method-call connector:** the predicate becomes a method argument supplied by the gateway.

Because the predicate is added by the connector below the gateway, a prompt-injected agent that *asks* for "all customers" still gets only `WHERE owner_id = :actorId` rows — it cannot widen what it cannot name.

```java
// SqlConnector
String sql = translate(resolvedOp);                 // built from intent
sql = scope.applyTo(sql, actor);                    // appends AND owner_id = :p
return jdbc.query(sql, bind(actor));
```

> **Design note.** Scope on **reads** (`observe`) is a query filter, which is clean. Scope on **effects** is different — there's often nothing to "filter," the scope is really an authorization predicate ("may this actor act on this resolved target?"). Implementation: for effects, resolve the target first (a scoped `observe` under the hood), and if the target isn't in the actor's scoped set, DENY before dispatch. So scope-for-effects = a pre-resolution check, not a WHERE clause. This should be stated explicitly; the RFC currently blurs it.

---

## 6. Implementing the fourteen gates

Each gate is a small deterministic function `GateResult eval(ResolvedAction, Actor, Session, Scope)`. Where each keeps state:

| Gate | Where state lives | How computed | Cost |
|---|---|---|---|
| `rate` | Redis counter, sliding window key `agent:action[:per]` | `INCR` + window expiry; compare to limit | ~1 Redis op |
| `quota` | Redis/DB counter per window/session | same, longer TTL | ~1 op |
| `valueLimit` | none (stateless) | read `data.field`, compare | in-memory |
| `spendLimit` | Redis accumulator per session | add estimated cost, compare | ~1 op |
| `allowlist`/`denylist` | named sets cached in memory (refreshed) | set membership on `data.field` | in-memory |
| `precondition` | registry (transition from-states) + registered check fns | call check(s); transition: read current state, test ∈ from | 0–1 read |
| `contentCheck` | external hook (DLP svc) | sync call, deterministic verdict pass/block | network call |
| `requireApproval` | DB (approval request) | returns HOLD; resolved out-of-band (§7) | 1 DB write |
| `dualAuthorization` | DB (2 approvals, distinct ids) | returns HOLD until 2 distinct approve | DB |
| `window` | none | compare `now()` to window | in-memory |
| `quantityCap` | Redis/DB counter per subject | counter key `per:subject:of` in window | ~1 op |
| `disclosure` | post-execution check on result | compare result classification to allowed sink | in-memory |
| `emissionControl` | registered checks | like precondition; may HOLD for authz | 0–1 read |
| `requireExplanation` | none | assert `data.explanation` present/non-empty | in-memory |

Two gates need special implementation care:

**`disclosure` runs around the result, not just the request.** For an `observe`, you often can't know the result's sensitivity until you've fetched it. Implementation: execute the read, then before returning the result to the agent, the gate inspects the result's classification (from the registry or row-level labels) against the allowed sink; if it fails, the gateway **drops the result and returns a refusal**, and the audit records "read executed, result withheld." This is the anti-exfiltration control and it must sit on the *return* path.

> **Design note.** That means the read *did* hit the database even when disclosure fails. For most cases fine; for the most sensitive data you want to avoid even executing. So `disclosure` should support a *pre-check* form when sensitivity is known from the registry (block before execution) and a *post-check* form when it's row-dependent (block on return). The RFC should distinguish these two; right now it implies one.

**`contentCheck` is the only gate that calls out synchronously.** That makes it the latency and availability risk. Implementation: bounded timeout; on timeout/error apply `failureMode` (closed ⇒ treat as block). Cache verdicts by content hash where safe.

> **Design note.** From a product view, `contentCheck` and `requireApproval` are the two gates that can make the agent feel slow or stuck. Both should be **async-friendly** (see §7) so the agent's turn doesn't block a UI thread, surfacing "pending DLP / pending approval" states rather than spinning.

---

## 7. Approvals and the "hold" — how a synchronous call becomes async

A `requireApproval`/`dualAuthorization` gate returns **HOLD**, not pass/fail. Implementing HOLD is the subtle part, because the agent issued what looks like a synchronous tool call.

The implementation reuses the **staging/outbox** machinery (§9). On HOLD:

1. The action is **staged**: persisted to the `pending_actions` table in state `PENDING_APPROVAL`, with the full resolved action, scope, actor, and gate results.
2. The agent's tool call returns immediately with a structured receipt: `{status: "pending_approval", ticket: "act_123"}`. This is a normal tool result the agent reads; the conversation can continue or end.
3. A human resolves the ticket via the approvals UI/API. On approve, the row moves `PENDING_APPROVAL → PENDING` (or `→ DENIED` on reject). For `dualAuthorization`, two distinct approver ids must record approval; the gateway enforces `approver.id != actor.id` and distinctness.
4. The **dispatch worker** (§9) picks up `PENDING` rows and executes them — exactly the same path a normal effect takes. The outcome is recorded and, if the session is still live, pushed back to the agent/user as a follow-up.

So an approval is just "a staged action whose release requires a human event instead of an automatic one." No separate machinery.

```
agent ──submit──▶ gateway ──HOLD──▶ pending_actions[PENDING_APPROVAL]
                                   │
human ──approve──▶ approvals API ──┘ ──▶ [PENDING] ──▶ dispatch worker ──▶ connector ──▶ audit
```

> **Design note.** This unification is the key insight of the whole implementation: **approvals and kill are both just transitions on staged actions.** Approval = a human releases it; kill = an operator cancels it. Once you model every consequential effect as a staged row with a lifecycle, both features fall out of the same table. That's why effects must be staged (RFC §4.4) — not only for durability, but because staging is the substrate for approval *and* kill.

---

## 8. The kill-switch, in full (the part that was unclear)

The kill-switch is the question the RFC left as a one-liner. Here is the actual mechanism.

### 8.1 What "kill" is
A kill is **a flag, checked at the chokepoint, that turns matching actions into an audited `HALT` and prevents any not-yet-dispatched effect from dispatching.** Its strength comes from §0: the gateway is the only path to effects. It is *not* the ability to reverse what already happened.

### 8.2 Kill state
```java
record KillOrder(
   String id, Scope scope,        // GLOBAL | AGENT(id) | SESSION(id) | ACTION_CLASS(kind,resource,action)
   String predicate,              // optional extra condition (a §8 expression)
   String issuedBy, Instant at, Instant liftedAt /*nullable*/
) {}
```
- **Durable** in Postgres (`kill_orders`), so a kill survives a gateway restart.
- **Hot** in every gateway instance's memory as an indexed set, so the hot-path check is O(1) with no network hop.
- **Propagated** across instances via Redis pub/sub (or a LISTEN/NOTIFY): writing a kill publishes an invalidation; every instance updates its in-memory set within milliseconds. A monotonic `kill_epoch` lets an instance detect missed messages and reload.

Issuing a kill is itself an audited operator action (who/when/scope) and is reversible (set `liftedAt`).

### 8.3 Where the check happens (three points, on purpose)
1. **Top-of-pipeline global/agent/session pre-check** — short-circuits a fully-killed agent before doing any work.
2. **Step 5 per-action check** — matches `ACTION_CLASS` orders and predicates; this is where a normal action becomes `HALT`.
3. **Dispatch-worker pre-send check** — the authoritative last check, *inside the same DB transaction that moves the staged row to `DISPATCHING`* (see §8.4). This is the one that actually prevents a send.

### 8.4 The race condition, and how it's closed
The dangerous window is between "checked kill" and "effect actually sent." Closing it relies on staging + a transactional state transition with a row lock:

```sql
-- dispatch worker, per pending row
BEGIN;
SELECT * FROM pending_actions WHERE id = :id AND state = 'PENDING' FOR UPDATE;  -- row lock
-- re-evaluate kill INSIDE the transaction:
IF kill_matches(row) THEN
   UPDATE pending_actions SET state='CANCELLED', reason='kill' WHERE id=:id;
   COMMIT;  -- never dispatched
ELSE
   UPDATE pending_actions SET state='DISPATCHING' WHERE id=:id;
   COMMIT;  -- now this worker owns the send
END IF;
```
After the row is `DISPATCHING`, the worker calls the connector. Because the kill check and the state change are in one locked transaction, a kill either (a) is seen first ⇒ `CANCELLED`, or (b) arrives after `DISPATCHING` ⇒ the send is already committed. There is no in-between where an action has both "passed kill" and "not yet been sent."

An **idempotency key** on each pending row makes the connector send safe under worker retries and guarantees a `CANCELLED` row can never later dispatch.

### 8.5 Aborting an *already-dispatching* action
For the (b) case — already handed to a connector — the gateway keeps a registry of **in-flight connector calls** keyed by session/action, each holding a cancellation handle (HTTP request abort, JDBC `Statement.cancel()`, a job-queue cancel). On kill, it invokes those handles. Whether the external world honors it is connector-dependent and must be declared per connector:
- **Cancellable** (uncommitted DB tx, abortable HTTP, queued-but-unsent job) ⇒ kill cancels it.
- **Point-of-no-return** (SMTP already accepted the message, a fired command) ⇒ kill cannot reverse; the gateway instead triggers the action's **declared compensation** (`refund`, `recall`, `cancelOrder`) if one exists, as a new staged effect.

### 8.6 What the agent sees
A killed action returns a structured `HALT` tool result (same shape as a recoverable error). Because the kill order persists, the agent cannot retry past it — every retry re-matches the order and re-`HALT`s, all audited.

### 8.7 Propagation beyond the chokepoint (defense in depth)
In addition to blocking at the gateway, a kill optionally:
- calls the **agent runtime's cancel API** / revokes the session token, so the LLM loop stops burning compute;
- **rotates/disables the connector credentials** the gateway uses downstream, so even a code bug can't dispatch;
- at proxy deployments, **drops the agent's egress**.

### 8.8 Sequence (session kill during a payment)
```
operator ──kill(SESSION s)──▶ gateway: write kill_orders; publish invalidation
                                         all instances update in-memory set (≈ms)
agent ──submit pay──▶ pipeline: step5 kill.matches(s)=true ──▶ HALT (audited), nothing staged
            (or, if pay was already staged PENDING:)
dispatch worker: BEGIN; SELECT ... FOR UPDATE; kill_matches=true
                 ──▶ state=CANCELLED; COMMIT  (never sent)
            (or, if already DISPATCHING:)
gateway: cancel in-flight handle; if SMTP already accepted ──▶ stage compensation if declared
```

### 8.9 Latency & failure
The in-memory set check is sub-microsecond. The transactional check is one indexed read inside a transaction the worker already runs. If the **kill store is unreachable**, the gateway treats kill as *possibly active for effects* and fails **closed** for irreversible actions (configurable) — a kill you can't read must not be assumed absent.

> **Design note.** §8.4 + §8.5 is exactly the honesty the product needs: "no *new* or *un-dispatched* action proceeds after kill; in-flight calls are cancelled where cancellable and compensated where declared; a committed external effect is never claimed to be reversed." That sentence is defensible to an auditor. "Big red stop button that undoes everything" is not, and it should never be implied.

> **Design note.** One more: kill of a `GLOBAL` scope across many instances needs the pub/sub to be reliable. Pub/sub alone is not enough — every instance also re-reads the `kill_epoch` on each request from its local cache and does a periodic authoritative reload, so a dropped invalidation message self-heals within the reload interval. Pub/sub for speed, polling for safety.

---

## 9. Effect durability — the outbox (backbone of approvals and kill)

Effects can't be transaction-rolled-back, so they're staged:

1. **Stage:** within the agent's request transaction, write the effect to `pending_actions` (state `PENDING`) — this commits atomically with any `record` parts of the same batch. Nothing external has happened yet.
2. **Dispatch:** a worker polls `PENDING` rows, runs the §8.4 locked transition, and calls the connector with an idempotency key.
3. **Settle:** on success/failure the row moves to `DONE`/`FAILED` (a `transition`), recording the connector result. A `FAILED` irreversible effect with a declared compensation can auto-stage the compensating effect.

This gives at-least-once dispatch with idempotency (effectively once), a cancellation window for kill, an approval hold point, and a durable audit of attempts — all from one table.

> **Design note.** The cost is that effects are now **asynchronous**: the agent gets "accepted/pending," not "sent," on the first turn. For most enterprise actions that's correct (and matches how humans work). For the rare effect that must be synchronous and is safely cancellable, allow an inline fast-path that still writes the audit — but default to staging. The RFC should make "effects are staged by default, inline is an opt-in for cancellable effects" explicit.

---

## 10. The condition engine (`when:`)

`when:` expressions (RFC §8) are compiled once into an AST and evaluated against a context map; there is **no `eval`, no host-language execution** — it's a tiny tree-walk interpreter over a frozen grammar, which is what keeps it safe and deterministic.

```java
boolean eval(Expr e, Ctx ctx) {
  return switch (e) {
    case And a   -> eval(a.l, ctx) && eval(a.r, ctx);
    case Or  o   -> eval(o.l, ctx) || eval(o.r, ctx);
    case Not n   -> !eval(n.e, ctx);
    case Cmp c   -> compare(resolve(c.l, ctx), c.op, resolve(c.r, ctx));
    case In  i   -> asList(resolve(i.r, ctx)).contains(resolve(i.l, ctx));
    case Exists x-> ctx.has(x.path);
  };
}
```
`resolve` looks up `action.*`, `data.*`, `resource.*`, `actor.*`, `context.*` from the context the pipeline already built. Unknown paths ⇒ validation error at load (RFC §13.9), not a runtime surprise. The four allowed functions (`count`, `now`, `window`, `spend`) are registered host functions with fixed signatures.

> **Design note.** Because conditions can gate safety decisions, the engine must treat a *resolution error at runtime* (e.g., a missing `resource.field`) as **fail-closed for that gate**, not "condition false." The compile-time check catches unknown paths, but a null value at runtime still needs a defined, conservative behavior.

---

## 11. Audit implementation

The audit record (RFC §11) is written by `terminal()`/`hold()`/settle on **every** outcome. It's append-only (Postgres table, no UPDATE/DELETE grant for the app role; or an append-only log/WORM store for regulated tiers). Records carry a `correlationId` (session) and an action `id` so a full agent run replays as one ordered query. The record is written **before** the result is returned to the agent for refusals/holds, and **after** settle for executed effects (with the connector outcome).

> **Design note.** "The audit is the product's evidence" — so the write must be on the same transaction as the state change wherever possible (e.g., the dispatch settle writes outcome + audit in one tx), so you can never have an effect that happened with no record, or a record of something that didn't. No best-effort logging on a side channel.

---

## 12. Failure mode implementation (`failureMode`)

A "dependency failure" is concretely: registry unavailable, scope-resolution failure, a `contentCheck` hook timeout/error, the kill store unreachable, or the outbox DB unavailable. For each, `failureMode: closed` (default) ⇒ the action resolves DENY/HALT and is audited with the failure reason. `open` ⇒ allow (only sane for low-stakes deployments). The override granularity is per kind/action; an `open` override on an `irreversible` action is a load-time error unless explicitly acknowledged (RFC §13.5). Implementation: wrap each external dependency call in a typed result (`Ok | Unavailable`) and branch on `failureMode` — never let an exception bubble into an implicit allow.

---

## 13. Concurrency & performance summary

- **Hot path (steps 1–5):** all in-memory or single Redis ops; target single-digit milliseconds excluding the connector. The compiled policy, registry, and named sets are cached and refreshed on change.
- **Locks:** only one — the `FOR UPDATE` on the pending row during dispatch (§8.4). Everything else is lock-free.
- **State stores:** Redis for ephemeral counters and kill propagation (lose it ⇒ fail-closed counters, not silent allow); Postgres for the things that must be durable (audit, outbox, approvals, kill orders).
- **Scaling:** gateways are stateless except for caches; scale horizontally; shared state is Redis + Postgres. Kill and policy changes propagate via pub/sub + epoch polling.

---

## 14. Engineering review — issues found and recommended RFC changes

Consolidated findings from the implementation review (all incorporated into RFC v0.2):

1. **Scope means two different things** (read filter vs. effect authorization). *Recommend:* the RFC should define scope-on-effect as a pre-resolution authorization check, distinct from the WHERE-clause form. (§5)
2. **`disclosure` has a pre-check and a post-check form.** *Recommend:* the RFC should name both — block-before-execution when sensitivity is known from the registry; withhold-on-return when it's row-dependent. (§6)
3. **Effects are asynchronous by default.** *Recommend:* the RFC should state that effects are staged (accepted/pending) by default, with an inline fast-path only for cancellable effects — because approvals and kill both depend on staging. (§7, §9)
4. **Kill needs three check points and a transactional dispatch.** *Recommend:* the RFC's §9 should reference the staged-row + `FOR UPDATE` mechanism and explicitly scope the guarantee: prevents new/un-dispatched actions; cancels cancellable in-flight; compensates declared irreversibles; never reverses committed effects. (§8)
5. **Runtime condition resolution errors must fail closed per-gate.** *Recommend:* add to RFC §8/§10 that a null/missing path at runtime denies the gate, distinct from "condition evaluated false." (§10)
6. **Unmapped tools in interception mode must deny, and free-form-string tools must be flagged.** *Recommend:* add an interception-coverage section to the RFC: unmapped ⇒ deny; pass-through tools require explicit acknowledgement; startup coverage check. (§1)
7. **Audit must be transactional with state changes.** *Recommend:* RFC §11 should require the audit write to share the transaction with the state change for executed/settled effects, forbidding side-channel best-effort logging. (§11)
8. **Kill propagation needs pub/sub + polling.** *Recommend:* RFC §9 should specify both fast propagation and a self-healing authoritative reload (epoch), not pub/sub alone. (§8.9)

None of these change the *policy language*; they sharpen the *guarantees and mechanics* behind it — which was exactly the gap the owner pointed at with the kill question.

---

## 15. One-paragraph mental model

Build one chokepoint and make it the only way out. Compile the policy to a fast matcher; resolve every action to a typed object with declared attributes; run a pure, model-free pipeline (resolve → authorize → scope → gates → kill → execute) that always ends in an audited decision. Make every consequential effect a **staged row with a lifecycle**, because that single design choice is what makes durability, **approvals**, and **kill** all work: approval releases the row, kill cancels it, the dispatch worker sends it under a row lock with an idempotency key so the kill/send race has no gap. Everything the RFC promises is, mechanically, a check at the chokepoint plus a transition on a staged row — and the honest boundary of the kill is precisely the boundary of that staging: it stops anything not yet sent, and compensates, but does not reverse, anything already gone.
