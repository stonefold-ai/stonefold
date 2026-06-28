# Agent Control Policy (ACP) — Specification v0.2

*The policy language for the SIF gateway: the declarative file that decides, deterministically, what an AI agent is permitted to do, and what is recorded when it tries.*

> **Layering.** ACP is the upper layer. The lower layer — **what the agent can express** (the five action kinds and the intent shape) — is defined in the **SIF RFC** ([`00-RFC-sif-intent-format.md`](00-RFC-sif-intent-format.md)). ACP references SIF for the kinds and the operation shape; it does not redefine them. SIF = *what can be said*; ACP = *what is allowed*.

**Status:** Draft v0.2 (reference specification; supersedes v0.1). **Authors:** the agent-platform team.
**Audience:** engineers implementing or writing policies, and reviewers (security, compliance) who must read and certify them.

> **Compatibility:** v0.2 changes are **semantic/behavioural clarifications only — no policy-file syntax changed.** Existing `apiVersion: acp/v0.1` policy files remain valid as-is; the JSON Schema is unchanged. If you have already implemented v0.1, apply the focused **Change Set** (`docs/RFC-changeset-v0.1-to-v0.2.md`) rather than re-reading this whole document.

## Changelog — v0.1 → v0.2

| ID | Type | §  | Summary |
|----|------|----|---------|
| CS-001 | CLARIFIED | §6.3 | Scope means two things: a **filter** for reads/writes, a **pre-resolution authorization check** for effects. |
| CS-002 | CLARIFIED | §7.12 | `disclosure` has a **pre-check** (block before execute, sensitivity known from registry) and a **post-check** (withhold on return, row-dependent) form. |
| CS-003 | CHANGED | §4.4 | Effects are **staged (accepted/pending) by default**; inline execution is opt-in for cancellable effects only. Staging is the substrate for approvals and kill. (`SHOULD` → `MUST`.) |
| CS-004 | ADDED | §9 | Kill **no-race guarantee**: dispatch-time kill check and the `pending → dispatching` transition occur in one serialised transaction; three check points; idempotency key; explicit guarantee scope (prevents new/un-dispatched, cancels cancellable in-flight, compensates declared irreversibles, does **not** reverse committed effects). |
| CS-005 | ADDED | §8 | A condition path that is **absent/null at runtime** makes its gate **fail closed**, distinct from evaluating `false`. |
| CS-006 | ADDED | §11 | The audit write for an executed/settled effect **MUST share the transaction** with the state change (no effect-without-record, no record-without-effect). |
| CS-007 | ADDED | §9 | Kill **propagation** across gateway instances MUST be prompt and **self-healing** (fast notify + authoritative re-read); kill store unreachable ⇒ fail closed for irreversible effects. |

*One review item (interception-mode coverage: unmapped tools deny, free-form-string tools flagged) is a transport/architecture concern and lives in `docs/03-architecture-decisions.md`, not in this policy spec.*

### Conventions

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are used as in RFC 2119. A *gateway* is the deterministic enforcement point; a *policy* is one ACP document governing one agent (or a reusable fragment). The *model registry* is the declared catalogue of resources and actions the gateway knows about; the policy references names from it.

---

## 1. Overview and design principles

An ACP document binds an **agent identity** to a set of **permissions** (what it may attempt) and **gates** (deterministic conditions every attempt must satisfy). The gateway evaluates the policy on every action; **no language model runs inside the evaluation**.

Five principles constrain the whole format. They are the reason the language stays small and certifiable.

1. **Default deny.** Anything not explicitly allowed is refused.
2. **Deny wins.** An explicit `deny` overrides any `allow`, always.
3. **Enforcement below the model.** Scope and gates are applied by the gateway after the agent acts; the agent never sees or supplies them.
4. **Deterministic gates only.** Every gate resolves to pass / fail / hold by code or a typed hook — never by model judgement. (A hook MAY call out, e.g. a DLP service, but returns a deterministic verdict.)
5. **Frozen shape.** The vocabulary of *kinds*, *gate types*, and *condition operators* is fixed. Growth happens by adding resources, actions, and named hooks — never new language constructs. (See §13.)

---

## 2. Core concepts

| Concept | Meaning |
|---|---|
| **Agent** | The identity the policy governs (e.g. `support-assistant`). One policy per agent, possibly composed from fragments (§3.2). |
| **Actor** | The end principal on whose behalf the agent acts (the human user / session identity). Drives `scope` and approvals. |
| **Resource** | A thing the agent can act on or about — a record type, file, device, channel, sensor (e.g. `Customer`, `Vehicle`, `Email`). Declared in the model registry. |
| **Action** | A named operation of a given **kind** over a resource (e.g. `sendEmail`, `administer`, `engage`). Declared in the registry with its **governance attributes**. |
| **Kind** | One of five fixed categories every action belongs to (§4). |
| **Governance attributes** | Fixed, declared facts about an action that policies reason over: reversibility, emission, operative force, result sensitivity, explainability (§5). |
| **Gate** | A deterministic condition an attempted action must pass (§7). |
| **Decision** | The gateway's verdict: `allow`, `hold` (await approval), `deny`, or `halt`. |

---

## 3. Action kinds — full enumeration

The five kinds are defined canonically in the **SIF RFC** ([`00-RFC-sif-intent-format.md`](00-RFC-sif-intent-format.md) §2); this section describes their **policy relevance** (which gates matter, where severity comes from). Every action belongs to **exactly one** of these five kinds. The kind is declared in the registry, not chosen by the policy or the agent. The kind shapes which gates are meaningful; it does **not**, by itself, determine severity (that comes from attributes, §5).

### 4.1 `observe` — acquire information, no change to the world
Reading a record, querying data, **passive** sensing, fetching a document. Returns data; changes nothing externally.
- **Primary risk:** disclosure / exfiltration. Reads can leak across tenants or classification levels.
- **Most relevant gates:** `scope`, `disclosure` (result sink), `allowlist`/`denylist`, `rate`, `requireApproval` (e.g. break-glass).
- **Note:** "just reading" is not automatically low-stakes (e.g. accessing a sealed medical record). And **active** sensing that *emits* (radar, sonar, a network probe) is **not** `observe` — it is `effect` (§4.4).

### 4.2 `assess` — produce a consequential judgement
Computing a decision, score, classification, or derived claim others rely on: a triage level, a risk score, a combat identification, a credit decision.
- **Primary risk:** a wrong/biased/unexplained decision that downstream actions trust.
- **Mandatory:** an `assess` action **MUST** declare its inputs and method; high-stakes `assess` **SHOULD** require explanation and/or human confirmation before any `effect` may rely on it.
- **Most relevant gates:** `requireExplanation`, `requireApproval` (`mode: confirm`), `dualAuthorization`, `disclosure`.

### 4.3 `record` — change facts the system owns
Create / update / link / delete stored data (the classic CRUD), expressed as named actions.
- **Primary risk:** a record with **operative force** (a DNR, a target designation, a signed diagnosis) is mechanically a `record` but governs real consequences — gate it by its `operativeForce` attribute, not by the kind.
- **Most relevant gates:** `scope`, `precondition`, `valueLimit`, `requireApproval` (when `operativeForce == high`), `rate`/`quota`.

### 4.4 `effect` — cause a change in the external world
Send, dispatch, actuate, pay, drive, transmit — anything reaching beyond the system, **including emitting sensing** (radar/sonar/probe).
- **Primary risk:** irreversibility and blast radius. This is the kind the product exists to govern.
- **Most relevant gates:** all of them; especially `valueLimit`, `spendLimit`, `allowlist`, `precondition`, `contentCheck`, `requireApproval`, `dualAuthorization`, `window`, `quantityCap`, `emissionControl`.
- **Durability rule (CS-003):** because an `effect` cannot be transactionally rolled back, effects are **staged by default**. The gateway **MUST** record the intent and commit it (atomically with any `record` ops in the same batch), return an *accepted/pending* result, then dispatch asynchronously and represent the outcome as a `transition` (`pending → done / failed`) with a declared compensation where one exists. Inline (synchronous) execution is an explicit opt-in permitted **only** for cancellable effects. Staging is also the substrate for approvals (§7.8) and the kill-switch (§9).

### 4.5 `transition` — advance a resource through its declared lifecycle
Move a thing from one declared state to another (`draft → signed`, `conflict_check → active`, `identified → designated`).
- **Primary risk:** performing a step out of order. The legal **from-states** are the institution's permitted process, declared once.
- **Mandatory:** a `transition` action **MUST** declare its legal `from` states; the gateway **MUST** refuse a transition whose current state is not in that set (this is a built-in `precondition`, not optional policy).
- **Most relevant gates:** `precondition` (from-states, built-in), `requireApproval`, `dualAuthorization`, `window`.

> **All five kinds appear, with gates, in the worked examples of §12.**

---

## 5. Governance attributes — full enumeration

Attributes are declared on each action in the registry and are **read-only** to the policy; conditions reference them (e.g. `when action.reversibility == irreversible`). They are how a policy applies severity uniformly without naming every action.

| Attribute | Allowed values | Meaning / typical use |
|---|---|---|
| `reversibility` | `reversible`, `compensable`, `irreversible` | How recoverable the action is. Drives approval/gate strength. `compensable` = a declared undo exists (refund, cancel). |
| `emission` | `none`, `emits` | Whether the act reveals/transmits into the world even while "just looking." `emits` forces `observe`-looking sensing into `effect` handling. |
| `operativeForce` | `none`, `low`, `high` | Whether parties treat the result as authoritative and act on it (a DNR, a designation). |
| `resultSensitivity` | `public`, `internal`, `confidential`, `restricted`, or a domain classification label | Classification of data an `observe`/`assess` returns. Drives `disclosure`. |
| `explainability` | `none`, `required` | Whether the action (typically `assess`) must carry a recorded rationale. |

Domains MAY extend the *value sets* (e.g. add classification labels) but MUST NOT add new attribute *names*.

---

## 6. File structure — top-level keys

A policy document is YAML. Top-level keys:

| Key | Required | Purpose | Section |
|---|---|---|---|
| `apiVersion` | SHOULD | Spec version, e.g. `acp/v0.1`. | — |
| `agent` | **MUST** | The agent identity this policy governs. | §2 |
| `extends` | MAY | List of fragment policies to compose/inherit. | §3.2 |
| `defaults` | MAY | Document-wide defaults (`failureMode`, `audit`, `killable`). | §10, §11, §12 |
| `allow` | **MUST** | Permissions: actions the agent MAY attempt, by kind. | §6 |
| `deny` | MAY | Explicit prohibitions; override `allow`. | §6 |
| `scope` | MAY | Per-resource scope predicates injected below the model. | §6.3 |
| `gates` | MAY | Deterministic conditions per action / kind / `'*'`. | §7 |
| `standing` | MAY | Time/quantity-conditioned authorizations (ROE, PRN). | §7.12 |
| `killable` | SHOULD | Whether the agent/its actions can be halted live. | §9 |
| `audit` | SHOULD | Audit level: `none` \| `basic` \| `full`. | §11 |

### 3.2 Composition (`extends`)
A policy MAY list fragments in `extends`; the gateway merges them in order, then applies this document last. Merge rules: `allow`/`deny`/`gates`/`scope` are **unioned**; on conflict, **`deny` always wins** and the **more restrictive** gate value wins (lower limit, narrower allowlist). Composition MUST NOT be able to *widen* a permission a fragment denied.

---

## 6. Authorization: `allow`, `deny`, `scope`

### 6.1 Syntax
`allow` and `deny` are maps keyed by **kind**, valued by a list of resources or named actions, or `'*'`.

```yaml
allow:
  - observe:    [Customer, Order, Invoice]     # any read of these resources
  - record:     [Note]                          # may create/update Notes
  - effect:     [sendEmail]                      # a specific named effect
  - transition: { Order: [confirm, ship] }       # named transitions on Order
deny:
  - effect:     [refund, exportData]             # never, regardless of allow
  - transition: { Order: [cancel] }
```

- A bare list under `observe` / `record` names **entities** — granting reads/writes of those entities (these kinds are implicit per entity; see the Registry spec §4).
- A bare list under `assess` / `effect` / `transition` names **declared actions** (each bound to an entity in the registry), e.g. `effect: [pay]`.
- A `{ Entity: [names] }` map grants only the **named** actions on that entity (works for any kind), e.g. `transition: { Invoice: [markPaid] }`.
- `'*'` as the value grants the whole kind (use sparingly; the linter warns).

### 6.2 Precedence and defaults
The gateway MUST evaluate authorization as:

1. **Default `deny`.** No match ⇒ refused.
2. If any `deny` rule matches the action ⇒ **DENY** (deny always wins).
3. Else if any `allow` rule matches ⇒ proceed to scope and gates.
4. Among competing `allow` matches, the **most specific** (named action > resource > `'*'`) governs which gates apply.

### 6.3 `scope`
`scope` maps a resource to a **named scope predicate** resolved by the gateway from the actor's identity and **injected after the model**. The agent cannot read or set it.

```yaml
scope:
  Customer: assignedToCurrentUser     # only rows owned by the actor
  Matter:   clientOf(actor)
  Patient:  inWard(actor.ward)
  Track:    inCompartment(actor.clearance)
```

Scope predicates are declared/registered in the gateway (not free expressions). A scope on a resource applies to **every** kind touching it (an `observe`, a `record`, a `transition`). If a resource has a scope and the actor resolves to an empty set, matching actions return empty / are refused — never widened.

**Reads vs effects (CS-001).** For `observe`/`record`/`transition` that read or write owned data, the predicate is realised as a **filter** (e.g. an injected `WHERE` clause) applied by the connector below the gateway. For an `effect` — where there is nothing to "filter" — the same predicate is enforced as a **pre-resolution authorization check**: the gateway resolves the effect's target first, and if the target is not in the actor's scoped set the action is **DENIED before dispatch**. Either way the agent never supplies or sees its own scope.

---

## 7. Gate catalog — full enumeration

Gates attach under `gates`, keyed by a **named action**, a **kind**, or `'*'` (all actions). All gates that match an action are combined with **AND** — every one MUST pass. Each gate resolves to `pass`, `fail` (⇒ DENY), or `hold` (⇒ await approval). Any gate value MAY be made conditional with `when:` (§8).

```yaml
gates:
  sendEmail:           # by named action
    rate: 20/hour
  effect:              # by kind (applies to all effects)
    spendLimit: 50/session
  '*':                 # global
    requireApproval: { when: "action.reversibility == irreversible" }
```

The complete gate set (unchanged in v0.2):

| # | Gate | Resolves | Purpose |
|---|---|---|---|
| 1 | `rate` | pass/fail | Frequency ceiling per window. |
| 2 | `quota` | pass/fail | Cumulative cap over window/session. |
| 3 | `valueLimit` | pass/fail | Numeric bound on a parameter/field. |
| 4 | `spendLimit` | pass/fail | Cost/$ ceiling per task/session. |
| 5 | `allowlist` / `denylist` | pass/fail | Membership constraint on a field. |
| 6 | `precondition` | pass/fail | Named deterministic check / lifecycle from-states. |
| 7 | `contentCheck` | pass/fail | Typed hook (DLP, PII, classification scan). |
| 8 | `requireApproval` | pass/hold | Human sign-off, optionally conditional. |
| 9 | `dualAuthorization` | pass/hold | Two distinct identities required. |
| 10 | `window` | pass/fail | Temporal allow (hours, date range). |
| 11 | `quantityCap` | pass/fail | Per-subject cumulative cap (e.g. per patient). |
| 12 | `disclosure` | pass/fail | Result classification ↔ allowed recipients/sinks (reads). |
| 13 | `emissionControl` | pass/hold | Deconfliction/authorization for emitting effects. |
| 14 | `requireExplanation` | pass/fail | Action must carry a recorded rationale (assess). |

### 7.1 `rate`
`N/window` where window ∈ `second|minute|hour|day` (or `Ns/Nm/Nh/Nd`). Optional `per:` to scope the count.
```yaml
sendEmail:
  rate: 20/hour
  # or per-target:
charge:
  rate: { limit: 3/day, per: resource.customerId }
```

### 7.2 `quota`
Cumulative cap over a longer horizon or per session.
```yaml
exportReport:
  quota: 100/day
```

### 7.3 `valueLimit`
Bounds a numeric parameter. Supports `max`, `min`, or both.
```yaml
pay:
  valueLimit: { field: data.amount, max: 10000, currency: USD }
setSpeed:
  valueLimit: { field: data.kph, max: 130, min: 0 }
```

### 7.4 `spendLimit`
Cost ceiling for the agent's run; stops retry storms.
```yaml
effect:
  spendLimit: 25/session        # $ or token-cost units, gateway-configured
```

### 7.5 `allowlist` / `denylist`
Membership on a field. Lists MAY reference named sets (`allowlist:corporate-domains`).
```yaml
sendEmail:
  allowlist: { field: data.recipientDomain, set: corporate-domains }
eFile:
  allowlist: { field: data.court, set: approved-court-systems }
transferFunds:
  denylist:  { field: data.destinationCountry, set: sanctioned-list }
```

### 7.6 `precondition`
A named, registered deterministic check, or — for a `transition` — the legal `from` states (the latter is built in and MUST always hold).
```yaml
administer:
  precondition: [fiveRightsVerified, notDiscontinued]
Order.ship:
  precondition: { from: [packed] }          # transition from-states
engage:
  precondition: [positiveIdentification]
```

### 7.7 `contentCheck`
A typed hook returning pass/block. Hooks are registered code; the policy names one.
```yaml
sendEmail:
  contentCheck: dlp.basic
publish:
  contentCheck: [pii.scan, classification.scan]
```

### 7.8 `requireApproval`
Holds the action for a human. Fields: `when` (condition; default always), `approvers` (role or set), `quorum` (default 1), `timeout`, `onTimeout` (`deny` default | `allow`), `mode` (`approve` default | `confirm`).
```yaml
'*':
  requireApproval:
    when: "action.reversibility == irreversible"
    approvers: role:supervisor
    timeout: 30m
    onTimeout: deny
refund:
  requireApproval: { approvers: role:finance-manager }
```

### 7.9 `dualAuthorization`
Two **distinct** identities must approve (the actor cannot self-approve). Fields: `approvers`, `quorum: 2` implied, `distinctFrom: actor`.
```yaml
engage:
  dualAuthorization: { approvers: role:weapons-release-authority }
wireTransfer:
  dualAuthorization: { when: "data.amount > 50000", approvers: role:treasury }
```

### 7.10 `window`
Temporal allow. A match outside the window ⇒ fail.
```yaml
deploy:
  window: { days: [Mon,Tue,Wed,Thu], hours: "09:00-16:00", tz: "Europe/Bratislava" }
```

### 7.11 `quantityCap`
Per-subject cumulative cap over a window — the PRN/standing-order pattern.
```yaml
administer:
  quantityCap: { per: resource.patientId, limit: 3, window: 24h, of: data.drug }
```

### 7.12 `disclosure`
Binds the **result classification** of a read to permitted recipients/sinks. Prevents the exfiltration/spillage leg.
```yaml
observe:
  disclosure:
    when: "action.resultSensitivity == restricted"
    allowSink: [careTeam]                 # named sinks; default-deny others
readIntel:
  disclosure: { maxClassification: actor.clearance }
```
**Two forms (CS-002).** `disclosure` is enforced in whichever form the data allows: a **pre-check** when the result's sensitivity is known from the registry (the read is **blocked before execution**), and a **post-check** when sensitivity is row-dependent (the read executes, but a disallowed result is **withheld on return** and the decision recorded as `deny` with "result withheld"). The gateway MUST use the pre-check form whenever it can determine sensitivity without executing.

### 7.13 `emissionControl`
For `effect` actions with `emission == emits`: require deconfliction/authorization before the emission.
```yaml
radarSweep:
  emissionControl: { precondition: [emconAuthorized, deconflicted] }
```

### 7.14 `requireExplanation`
For `assess`: the action MUST produce a recorded rationale (and SHOULD pair with `requireApproval: {mode: confirm}` when high-stakes).
```yaml
triage:
  requireExplanation: true
combatId:
  requireExplanation: true
  requireApproval: { mode: confirm, approvers: role:tactical-officer }
```

### 7.15 `standing` (top-level conditional authorizations)
`standing` declares grants that are *off by default* and switched on by context — ROE states, shift windows, PRN orders. They are evaluated as additional `allow` + gate conditions.
```yaml
standing:
  - name: weapons-free
    when: "context.roeState == 'weapons_free'"
    enables: { effect: [engage] }
  - name: clinic-hours-orders
    when: "context.time in window('08:00-18:00')"
    enables: { transition: { Order: [sign] } }
```

---

## 8. Condition expression language

Conditions (`when:`) are a **small, frozen** boolean language. No loops, no assignment, no arithmetic beyond comparison. Grammar (EBNF):

```
condition  := orExpr
orExpr     := andExpr ("or" andExpr)*
andExpr    := unary ("and" unary)*
unary      := "not" unary | comparison | "(" condition ")"
comparison := operand op operand
            | operand ("in" | "not in") list
            | "exists" path
op         := "==" | "!=" | "<" | "<=" | ">" | ">=" | "matches"
operand    := path | literal | function
path       := ident ("." ident)*
function   := ident "(" [ literal ("," literal)* ] ")"
literal    := string | number | boolean | duration | list
duration   := number ("s" | "m" | "h" | "d")
list       := "[" [ literal ("," literal)* ] "]"
```

**Reference namespaces** (read-only):

| Namespace | Examples | Meaning |
|---|---|---|
| `action.*` | `action.kind`, `action.name`, `action.resource`, `action.reversibility`, `action.emission`, `action.operativeForce`, `action.resultSensitivity`, `action.explainability` | The attempted action and its declared attributes. |
| `data.*` | `data.amount`, `data.recipientDomain`, `data.kph` | Parameters the agent supplied. |
| `resource.*` | `resource.patientId`, `resource.currentState`, `resource.ownerId` | Properties of the resolved target. |
| `actor.*` | `actor.id`, `actor.role`, `actor.clearance`, `actor.ward` | The principal the agent acts for. |
| `context.*` | `context.now`, `context.time`, `context.roeState`, `context.sessionSpend` | Ambient state. |

**Functions** (the complete set): `count(scope, window)`, `now()`, `window("HH:MM-HH:MM")`, `spend(window)`. No others.

**Runtime resolution (CS-005).** Unknown paths are rejected at policy load (§13.9). If a referenced path is **absent or null at runtime** (e.g. `resource.foo` is missing on the resolved target), the gate whose condition referenced it **fails closed** (resolves DENY) — this is distinct from the condition evaluating to `false`. A condition error MUST NOT silently pass a gate.

---

## 9. Kill-switch (`killable`)
`killable: true` (default SHOULD be true for non-trivial agents) lets an operator issue a `halt` that:
- stops in-flight actions for an **action class**, a **session**, or the **agent**;
- causes subsequent matched attempts to resolve `halt` (not `deny`) — a distinct, audited terminal state;
- is itself an audited operator action (who halted, when, scope) and is reversible (the order can be lifted).

**No-race guarantee (CS-004).** A `halt` MUST take effect before the connector dispatch of any pending `effect`. The gateway MUST evaluate the kill at three points — entry (whole-agent/session short-circuit), per-action (pipeline step 5), and **at dispatch**, where the kill re-check and the staged action's `pending → dispatching` transition MUST occur in **one serialised transaction** (e.g. a row-locked update) so there is no window in which an action has both passed the kill check and remains un-dispatched. Each staged action carries an **idempotency key** so a cancelled action can never later dispatch.

**Scope of the guarantee (CS-004).** Kill prevents any *new* or *not-yet-dispatched* action; cancels in-flight actions whose connector is cancellable; and triggers declared **compensation** for irreversible effects already dispatched. It does **not** reverse an external effect that has already committed.

**Propagation (CS-007).** A kill MUST take effect across all gateway instances **promptly and reliably** — by fast notification (e.g. pub/sub) plus a self-healing authoritative re-read (e.g. an epoch counter) so a dropped notification cannot leave an instance unaware. If the kill store is unreachable, the gateway MUST **fail closed** for irreversible effects.

*Mechanism detail (state stores, the locked-transition transaction, in-flight cancellation) is in the implementation design §8.*

---

## 10. Failure mode (`defaults.failureMode`)
If the gateway, a `contentCheck` hook, or a scope/approval dependency is **unavailable or errors**, behavior is governed by `failureMode`:

```yaml
defaults:
  failureMode: closed        # closed (default) | open
```
- `closed` — the action is **denied** (regulated/safety default). MUST be the default.
- `open` — the action is allowed (only for low-stakes deployments).
`failureMode` MAY be overridden per kind/action; an `open` override on an `irreversible` action MUST be a linter error unless explicitly acknowledged.

---

## 11. Audit (`audit`)
Levels: `none` | `basic` (decisions only) | `full` (decisions + parameters + gate results). Regulated deployments SHOULD use `full`. Every evaluated action — **allowed, held, denied, or halted** — produces one append-only record. Required fields at `full`:

| Field | Description |
|---|---|
| `id`, `timestamp` | Unique id and time. |
| `agent`, `actor` | Governing agent and the principal it acted for. |
| `kind`, `resource`, `action` | The attempted action. |
| `parameters` | Typed parameters supplied (subject to redaction policy). |
| `scopeApplied` | Scope predicate(s) injected. |
| `gates` | Each gate evaluated and its result (pass/fail/hold). |
| `decision` | `allow` \| `hold` \| `deny` \| `halt`, with the deciding rule/gate. |
| `approval` | Approver(s), quorum, outcome, timing — if applicable. |
| `outcome` | Connector result: `success` \| `failure` (+ reason) \| `not_executed`. |
| `correlationId` | Session/transaction id for replay. |

**Transactional audit (CS-006).** For an executed or settled `effect`, the audit record **MUST** be written in the **same transaction** as the state change it records (the outbox settle), so there can be neither an effect that occurred without a record nor a record of an effect that did not occur. Refusals and holds are recorded **before** the result is returned to the agent. Best-effort side-channel logging is **not** sufficient for the audit log.

---

## 12. Evaluation order (the pipeline)
For each attempted action the gateway MUST proceed strictly in this order, stopping at the first terminal verdict:

1. **Resolve** the action's kind, resource, name, and attributes from the registry. Unknown ⇒ DENY.
2. **Authorize** (§6.2): default deny → deny-wins → allow-match.
3. **Inject scope** (§6.3).
4. **Evaluate gates** (§7), cheapest/deterministic first; `requireApproval`/`dualAuthorization` last. Any `fail` ⇒ DENY; else any `hold` ⇒ HOLD (await approval, then re-enter at step 5 on grant).
5. **Check kill-switch** (§9). Active ⇒ HALT.
6. **Execute** via the connector as one transaction (effects staged per §4.4 durability rule).
7. **Record** the audit entry (§11) — for every outcome, including refusals.

On any dependency error, apply `failureMode` (§10).

---

## 13. Validation rules (what the linter MUST check)
1. Every resource/action/scope/hook name referenced exists in the registry.
2. No `allow` and `deny` that *only* a human could disambiguate — `deny` always wins, but overlapping intent SHOULD warn.
3. Every `transition` action referenced has declared `from` states.
4. Actions with `reversibility == irreversible` and no `requireApproval`/`dualAuthorization`/`precondition` ⇒ **warn**.
5. `failureMode: open` on an `irreversible` action ⇒ **error** unless explicitly acknowledged.
6. `'*'` grants ⇒ **warn** (encourage explicit enumeration).
7. `assess` actions with `explainability: required` but no `requireExplanation` gate ⇒ **error**.
8. Reads of `resultSensitivity > internal` with no `disclosure` gate ⇒ **warn**.
9. Condition expressions parse against the grammar (§8) and reference only known namespaces/functions.

---

## 14. Worked examples (non-trivial, all kinds, multiple domains)

Each example exercises several kinds and gates. Together they cover all five kinds and the full gate catalog.

### 14.1 Customer support assistant (data / business)
*All reads scoped to the user's own customers; may email within corporate domains under a rate limit and DLP; may never refund or export; anything irreversible needs a supervisor.*
```yaml
apiVersion: acp/v0.1
agent: support-assistant
defaults: { failureMode: closed, audit: full }
killable: true

allow:
  - observe:    [Customer, Order, Invoice]
  - record:     [Note]
  - effect:     [sendEmail]
  - transition: { Order: [confirm] }
deny:
  - effect:     [refund, exportData]
  - transition: { Order: [cancel] }

scope:
  Customer: assignedToCurrentUser
  Order:    customerAssignedToCurrentUser

gates:
  sendEmail:
    rate: 20/hour
    allowlist:    { field: data.recipientDomain, set: corporate-domains }
    contentCheck: dlp.basic
  Order.confirm:
    precondition: { from: [pending_confirmation] }
  '*':
    requireApproval:
      when: "action.reversibility == irreversible"
      approvers: role:support-supervisor
      timeout: 30m
      onTimeout: deny
```

### 14.2 Ward nurse assistant (healthcare — observe, assess, record, effect, transition)
*Reads scoped to the nurse's ward; sealed records need break-glass; triage is an explained, confirmed assessment; administration enforces five-rights and a per-patient dose cap and is irreversible; signing an order is a gated transition; prescribing is forbidden.*
```yaml
apiVersion: acp/v0.1
agent: ward-nurse-assistant
defaults: { failureMode: closed, audit: full }
killable: true

allow:
  - observe:    [Patient, Medication, Observation, Order]
  - assess:     [triage]
  - record:     [Observation]                      # e.g. vitals
  - effect:     [administer, pageOnCall]
  - transition: { Order: [sign], Encounter: [discharge] }
deny:
  - effect:     [prescribe, discontinue]
  - transition: { Medication: [prescribe] }

scope:
  Patient:     inWard(actor.ward)
  Observation: forPatientInWard(actor.ward)

gates:
  observe:
    disclosure:
      when: "action.resultSensitivity == restricted"     # e.g. psych/HIV records
      allowSink: [careTeam]
    requireApproval:
      when: "action.resultSensitivity == restricted and not exists context.breakGlass"
      approvers: role:charge-nurse
  triage:                                    # assess
    requireExplanation: true
    requireApproval: { when: "data.acuity <= 2", mode: confirm, approvers: role:clinician }
  administer:                                # effect, irreversible
    precondition: [fiveRightsVerified, notDiscontinued]
    quantityCap:  { per: resource.patientId, limit: 3, window: 24h, of: data.drug }
    requireApproval: { when: "action.operativeForce == high", approvers: role:clinician }
  Order.sign:                               # transition, operative
    precondition: { from: [draft] }
    requireApproval: { approvers: role:clinician }
```

### 14.3 Air/maritime track operator (defence — observe vs emitting effect, assess, transition, gated kinetic effect)
*Passive reads are clearance-scoped with disclosure control; an active radar sweep is an emitting `effect` needing deconfliction; combat-ID is an explained, dual-confirmed assessment; engagement is enabled only under a standing ROE state and requires positive ID, a collateral ceiling, and dual authorization — and is denied otherwise.*
```yaml
apiVersion: acp/v0.1
agent: track-operator-assistant
defaults: { failureMode: closed, audit: full }
killable: true

allow:
  - observe:    [Track, IntelRecord]            # passive
  - assess:     [combatId, collateralEstimate]
  - record:     [TrackAnnotation]
  - effect:     [radarSweep]                     # emits
  - transition: { Track: [identify, designate] }
deny:
  - effect:     [engage]                         # default-denied; only 'standing' enables

standing:
  - name: weapons-free
    when: "context.roeState == 'weapons_free'"
    enables: { effect: [engage] }

scope:
  Track:       inCompartment(actor.clearance)
  IntelRecord: inCompartment(actor.clearance)

gates:
  observe:
    disclosure: { maxClassification: actor.clearance }
  radarSweep:                                   # emitting effect
    emissionControl: { precondition: [emconAuthorized, deconflicted] }
  combatId:                                     # assess
    requireExplanation: true
    requireApproval: { mode: confirm, approvers: role:tactical-officer }
  Track.designate:
    precondition: { from: [identified] }
  engage:                                       # effect, irreversible, kinetic
    precondition:       [positiveIdentification]
    valueLimit:         { field: data.collateralEstimate, max: 1 }   # CDE threshold
    dualAuthorization:  { approvers: role:weapons-release-authority }
    window:             { hours: "always" }
```

### 14.4 Payments operations agent (finance — tiered effects, dual-auth, sanctions, transition)
*Reads tenant-scoped; small payments auto-clear, mid-size need approval, large need dual authorization and a new-payee hold; sanctioned destinations are denied; export is forbidden.*
```yaml
apiVersion: acp/v0.1
agent: payments-ops-agent
defaults: { failureMode: closed, audit: full }
killable: true

allow:
  - observe:    [Account, Payment, Payee]
  - record:     [LedgerEntry]
  - effect:     [pay]
  - transition: { Invoice: [send, markPaid] }
deny:
  - effect:     [exportData]

scope:
  Account: tenantOf(actor)
  Payment: tenantOf(actor)

gates:
  pay:
    denylist:   { field: data.destinationCountry, set: sanctioned-list }
    valueLimit: { field: data.amount, max: 1000000, currency: USD }
    rate:       { limit: 50/hour, per: resource.payeeId }
    requireApproval:
      when: "data.amount > 1000 and data.amount <= 10000"
      approvers: role:payments-manager
    dualAuthorization:
      when: "data.amount > 10000"
      approvers: role:treasury
    precondition:
      when: "exists data.newPayee"
      checks: [payeeCoolingOffElapsed]      # new-payee hold
  Invoice.markPaid:
    precondition: { from: [sent] }
```

### 14.5 Legal matter assistant (data / business — ties to the repo demo)
*Reads scoped to the client; time entries and tasks are routine records; the `Engage` transition is legal only from `conflict_check` (the exact behaviour the repo already demonstrates); e-filing is allow-listed to approved courts; email is DLP-checked.*
```yaml
apiVersion: acp/v0.1
agent: legal-matter-assistant
defaults: { failureMode: closed, audit: full }
killable: true

allow:
  - observe:    [Matter, TimeEntry, Task, Staff]
  - record:     [TimeEntry, Task]
  - effect:     [sendEmail, eFile]
  - transition: { Matter: [engage, close] }
deny:
  - effect:     [deleteMatter]

scope:
  Matter:    clientOf(actor)
  TimeEntry: forMatterOfClient(actor)

gates:
  Matter.engage:
    precondition: { from: [conflict_check] }      # refuses from 'active', etc.
  eFile:
    allowlist:    { field: data.court, set: approved-court-systems }
    requireApproval: { approvers: role:supervising-partner }
  sendEmail:
    contentCheck: dlp.basic
    rate: 30/hour
```

### 14.6 Industrial vehicle controller (cyber-physical — bounded continuous effect)
*Setting target speed is inert; applying it is a safety-gated effect bounded by sensors and posted limits; the vehicle lifecycle is a transition; everything is killable.*
```yaml
apiVersion: acp/v0.1
agent: vehicle-controller
defaults: { failureMode: closed, audit: full }
killable: true

allow:
  - observe:    [Vehicle, Sensor]              # passive reads
  - record:     [Vehicle]                       # write target setpoint (inert)
  - effect:     [applySpeed]
  - transition: { Vehicle: [start, stop] }

gates:
  applySpeed:                                   # effect, continuous/safety-critical
    valueLimit:   { field: data.kph, max: 130, min: 0 }
    precondition: [surroundingsClear, withinPostedLimit, withinTractionLimits]
  Vehicle.start:
    precondition: { from: [stopped] }
```

---

## 15. Quick reference

**Kinds:** `observe` · `assess` · `record` · `effect` · `transition`
**Attributes:** `reversibility` · `emission` · `operativeForce` · `resultSensitivity` · `explainability`
**Gates:** `rate` · `quota` · `valueLimit` · `spendLimit` · `allowlist`/`denylist` · `precondition` · `contentCheck` · `requireApproval` · `dualAuthorization` · `window` · `quantityCap` · `disclosure` · `emissionControl` · `requireExplanation`
**Decisions:** `allow` · `hold` · `deny` · `halt`
**Top-level keys:** `apiVersion` · `agent` · `extends` · `defaults` · `allow` · `deny` · `scope` · `gates` · `standing` · `killable` · `audit`
**Precedence:** default deny → deny wins → most-specific allow → all matching gates AND → kill-switch → execute → record.
**Frozen in v0.2:** the five kinds, the five attribute names, the fourteen gate types, and the condition operators/functions (unchanged from v0.1 — v0.2 adds no syntax). Growth is by adding resources, actions, scope predicates, named sets, and hooks — never new language constructs.
