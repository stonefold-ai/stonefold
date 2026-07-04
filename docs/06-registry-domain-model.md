# Registry (Domain Model) — Specification v1.0

*The registry is where a domain is **declared**: its entities, their properties, the actions you can take on them (each with a **kind** and governance **attributes**), lifecycle states, value sets, and the connectors/predicates the gateway uses. SIF draws the agent's vocabulary from it; Stele reads attributes from it; the gateway validates every intent against it.*

**Status:** Draft v1.1. **Foundational layer** — read alongside the SIF RFC ([`00`](00-RFC-sif-intent-format.md)); Stele ([`01`](01-RFC-agent-control-policy.md)) and the policies reference the names declared here.

> **Changelog v1.0 → v1.1** (spec-review fixes, alongside Stele v0.3): attribute **defaults corrected** — undeclared attributes default to the *benign* end (`reversibility: reversible`), not the dangerous end as v1.0 stated (§4); `compensation` added to the action shape and to `schema/registry.schema.json` (§4); **action-name uniqueness** guidance and its lint consequence added (§8); scope-predicate **argument forms** defined (§5); the `derived` expression boundary made explicit (§4); the v1.0 "**exception for `deny`**" (undeclared names allowed in `deny`) **removed** — Stele §13.1 applies to `deny` too (Stele CS-016; the linter already enforced this).

### Conventions
Keywords per RFC 2119. The registry is YAML. Every name a policy or SIF intent references (entity, action, transition, field, value set, scope predicate, hook, named set, connector) **MUST** be declared here, or the policy fails to load (Stele §13.1).

---

## 1. Why it exists

A policy says *"the agent may `effect: pay`"* — but what **is** `pay`? What fields does a `Payment` have? Is `pay` reversible? Which connector actually moves the money? None of that belongs in the policy (which is about *permission*) or in SIF (which is about *intent shape*). It belongs in the **registry** — the single declaration of the domain that both layers build on.

Think of it as the typed schema of your world: like a database schema or a set of TypeScript types, but it also records each action's *kind*, its *governance attributes*, and its *lifecycle*.

---

## 2. Top-level structure

```yaml
apiVersion: registry/v1.0
domain: payments

connectors:        { … }   # named effect bindings / data sources
scopePredicates:   [ … ]   # names the gateway implements (e.g. tenantOf)
preconditionChecks:[ … ]   # named deterministic checks (e.g. payeeCoolingOffElapsed)
handlers:          [ … ]   # named post-action / effect handlers (e.g. recordLedgerEntry)
hooks:             [ … ]   # named content hooks (e.g. dlp.basic)
sinks:             [ … ]   # named disclosure sinks (e.g. careTeam)
namedSets:         { … }   # allow/deny lists (e.g. sanctioned-list)
valueSets:         { … }   # reusable enums
entities:          { … }   # the domain itself
```

`connectors`, `scopePredicates`, `preconditionChecks`, `handlers`, `hooks`, `sinks` are **declarations of names the integrator implements in code** — listing them here lets the linter verify policies and tells implementers exactly what to build (the `[REGISTER]` items from `examples/README.md`).

---

## 3. Entities

Each entity declares its properties, optional lifecycle states, its storage/connector mapping, and its actions.

```yaml
entities:
  Payment:
    label: "An outbound payment"
    dataSource: ledger-sql          # which connector serves observe/record/transition
    table: payments                 # storage mapping (connector-specific)
    properties:
      amount:             { type: decimal, required: true }
      currency:           { type: string }
      destinationCountry: { type: string }
      newPayee:           { type: boolean }
      payee:              { type: Payee }          # reference to another entity
      currentState:       { values: [pending, dispatching, done, failed, cancelled] }
    actions:
      pay: { … }                     # see §4
```

- **`properties`** — `name: { type, required?, … }`. Types are primitives (`string`, `decimal`, `int`, `boolean`, `dateTime`), another entity (a reference), or `{ values: [...] }` for an inline enum. A `currentState` property with `values` declares the lifecycle's states.
- **`dataSource` / `table`** — the connector that serves reads/records/transitions for this entity, and its storage handle.

---

## 4. Actions (name → kind + attributes)

Actions are declared **under their entity**. This is where `kind` and `attributes` live.

```yaml
    actions:
      pay:
        kind: effect
        attributes:
          reversibility: irreversible
          operativeForce: high
        connector: ledger-pay        # the effect binding that performs it
        data:                        # parameters the agent supplies (validated)
          amount:   { type: decimal, required: true }
          currency: { type: string }
        resolve:                     # references the agent fills by lookup
          payee: { entity: Payee }
```

For a **transition**, declare its legal states:
```yaml
      markPaid:
        kind: transition
        from: [sent]
        to: paid
```

Rules:
- `kind` is one of `observe / assess / record / effect / transition` (SIF §2).
- **`observe` and `record` are implicit per entity** — declaring an entity makes it readable/writable; you only declare explicit `observe`/`record` actions to name a special query or restrict them. A policy grants them by **listing the entity** (`observe: [Payment]`).
- **`assess`, `effect`, `transition` MUST be explicitly declared** as named actions; a policy grants them by **action name** (`effect: [pay]`) or via the map form (`transition: { Invoice: [markPaid] }`).
- **`attributes`** are the five governance attributes (Stele §5). Any not declared default to the **benign** end: `reversibility: reversible`, `emission: none`, `operativeForce: none`, `resultSensitivity: internal`, `explainability: none`. **Danger is declared, never assumed:** an action that is in fact irreversible, emitting, or operative MUST declare it — it is the Stonefold linter (unguarded-irreversible §13.4, open-on-irreversible §13.5, compensable-needs-compensation §13.10), not a pessimistic default, that guards the dangerous end, and the linter can only see what is declared. (A worst-case default would drown every registry in irreversible-warnings and train authors to ignore them.)
- An action MAY declare **`compensation: { resource, action }`** — the in-system undo the gateway can route to (auto-staged on a failed irreversible dispatch, design §9). **Required** when `reversibility: compensable` (Stele §13 rule 10); the named resource+action must exist in this registry.
- **The `{ derived: … }` form is implementation-defined in this draft.** A derived attribute/property expression MUST be a pure, deterministic projection of the record/action context (no I/O, no side effects); it is **not** the Stele §8 condition grammar (note the ternary in the examples). A frozen derivation grammar is deferred — see `docs/03`.
- `resultSensitivity` is often **per-record**, not per-action; declare a default on the action/entity and/or a derivation (e.g. `resultSensitivity: { derived: record.confidentialFlag }`) so the `disclosure` gate's pre-check/post-check (Stele §7.12) can resolve it. A domain that substitutes its **own classification labels** MUST declare them as an **ordered** value set (order is list position, lowest first) — `disclosure.maxClassification` compares by that declared order, and a value missing from the order fails closed (Stele §7.12, CS-024). The built-in order is `public < internal < confidential < restricted`.
- An action MAY declare **intrinsic `preconditions`** (named checks that must pass for *anyone*, always) and **`postActions`** (named handlers that run after it succeeds). These differ from Stele policy gates — see §6.

---

## 5. Supporting declarations

```yaml
connectors:
  ledger-sql: { type: sql }                 # serves observe/record/transition
  ledger-pay:
    type: method                            # the effect binding for `pay`
    digest: "sha256:9f2b…"                  # OPTIONAL: pins the implementing artifact
scopePredicates:   [ tenantOf ]             # implemented in the gateway
preconditionChecks:[ payeeCoolingOffElapsed ]  # named checks (pass/fail)
handlers:          [ recordLedgerEntry ]       # named post-action handlers
hooks:             [ dlp.basic ]               # named content hooks
sinks:             [ ]                          # named disclosure sinks
namedSets:
  sanctioned-list: { source: "sets/sanctioned-countries.txt" }
```

These names are exactly what a policy or an action references (`scope: { Payment: tenantOf(actor) }`, `denylist: { set: sanctioned-list }`, `precondition: [payeeCoolingOffElapsed]`, `postActions: [recordLedgerEntry]`). The registry declares them so they can be validated and so implementers have a checklist. Each is a function the integrator implements (see §6).

**Scope-reassertion capability (CS-018).** Besides its registry declaration, each connector declares its scope-reassertion capability — `transactional` or `window` (Stele §6.3) — **in gateway code, alongside the connector implementation**, the same way scope-predicate bindings are registered. It is deliberately *not* a registry-YAML field: the capability is a property of the connector's code and is reviewed with that code. An implementation that declares nothing is treated as `window:undeclared` — fail-safe, and labelled honestly in the audit record.

**Digest pinning (`digest`, optional).** A connector MAY pin the artifact that implements it by content digest (`sha256:…` over the connector's code artifact, as built/deployed). When a digest is declared, the gateway MUST verify the loaded implementation against it **at policy load and at dispatch**; a mismatch is a dependency failure under the policy's `failureMode` rules (Stele §10) — fail closed by default, with an audit record. The point: the registry already declares *what* a connector does; the digest declares *which code* is trusted to do it, so silently replacing a connector's implementation stops being invisible — changing connector code requires a registry change, which is a reviewed, versioned artifact. Production deployments handling irreversible effects SHOULD pin their effect connectors. How the digest is computed and artifacts are signed is deployment tooling, not registry semantics — the registry only carries the declaration. (Trust boundary discussion: docs/13.)

---

## 6. Preconditions, post-actions & handlers — what's automatic vs. what you implement

A common question: do `preconditions` / `postActions` generate code skeletons, or are they enforced automatically? **Both ideas apply, to different things.** Split them into two buckets.

**Bucket A — declarative, enforced automatically (no code).** The framework enforces these entirely from the declaration:
- transition **`from`-states**, enum/value membership, `valueLimit`, `allowlist`/`denylist` (against `namedSets`), `rate`/`quota`/`quantityCap`, `window`, and the **approval mechanism** itself (`requireApproval`/`dualAuthorization`).
You write the declaration; the gateway enforces it. No handler exists.

**Bucket B — named functions you implement; the framework invokes them.** The framework guarantees **when** they run and treats their result deterministically, but the **body is your code**:
- **precondition checks** (`payeeCoolingOffElapsed`, `fiveRightsVerified`) — run before execution; any failure ⇒ DENY,
- **content hooks** (`dlp.basic`) — return pass/block,
- **scope predicates** (`tenantOf`) — return a filter / authorization decision,
- **post-actions / effect handlers** — the `connector` and any `postActions` — run after the action passes all gates; they perform the effect and may set derived fields.

So: **invocation and ordering are automatic; the logic is hand-written.** The framework cannot know what "five rights verified" means — you implement it.

**Skeletons.** From the names declared in the registry, the build can **generate handler skeletons/interfaces** the developer fills in (this is what the original OntoCortex did with its `_generated/` stubs). It's optional DX, not part of enforcement — the contract is simply "register a function with this name and signature; the framework calls it." Suggested signatures:

| Kind | Signature (illustrative) | Must be |
|---|---|---|
| precondition check | `bool check(Context ctx)` → pass/fail (+reason) | pure / deterministic, no side effects |
| content hook | `Verdict hook(Content c)` → pass/block | deterministic verdict |
| scope predicate | `Filter\|Authz scope(Actor a)` | deterministic |
| post-action / effect handler | `Result handle(ResolvedAction a, Context ctx)` | may call external systems; runs after gates, in the staged dispatch |

**Where they live — registry vs. policy.** Both can carry checks/handlers, and the gateway runs both:
- **Registry (intrinsic):** truths that must hold for *everyone*, always — a transition's `from`-states, a domain safety invariant, a mandatory `postAction`. Declared on the action.
- **Stele policy (imposed):** per-agent / per-deployment gates — *this* agent needs approval over $10k, or must pass `fiveRightsVerified`. Declared as gates.

Order at runtime: registry intrinsic preconditions and policy precondition-gates must **all** pass before execution; post-actions/handlers run **after** the action succeeds (for effects, via the staged dispatch). Same recoverable-error path on any failure.

### Registered functions reference

The five kinds of named function the registry can declare. Each: what it is · what it receives → returns · when it fires · example.

**Scope predicate** — declared in `scopePredicates`.
- *What:* limits **which records** an actor may touch (access scope).
- *Receives → returns:* the actor (identity + claims) → a **filter** (for reads/writes) or an **authorization decision** (for effects).
- *When:* the scope-injection step, before execution — injected below the model so the agent can't widen it. For effects, a pre-resolution check on the target.
- *Example:* `tenantOf(actor)` → SQL filter `tenant_id = :actorTenant`; `inWard(actor.ward)` → `ward_id = 'Ward-3B'`. Referenced by `scope: { Account: tenantOf(actor) }`.
- *Argument form:* predicates are declared and resolved by **bare name**; the parenthesised argument a policy writes (`tenantOf(actor)`, `inWard(actor.ward)`) selects the actor claim the predicate reads and MUST be `actor` or a dotted `actor.<claim>` path — never a free expression. The gateway supplies the actor itself; the argument is validated against the predicate's declared signature at load.

**Precondition check** — declared in `preconditionChecks`; referenced by an action's `preconditions` or a Stele `precondition` gate.
- *What:* a deterministic yes/no test that must hold before an action runs.
- *Receives → returns:* the resolved action + target + data + actor (a context) → **pass / fail(reason)**.
- *When:* the gate step, before execution; any fail ⇒ DENY.
- *Example:* `payeeCoolingOffElapsed(ctx)` → false when the payee was created < 24h ago. Must be pure/deterministic, no side effects.

**Content hook** — declared in `hooks`; referenced by a Stele `contentCheck` gate.
- *What:* inspects the **payload/content** of an action and returns a verdict (DLP, PII, classification scan).
- *Receives → returns:* the action's content/data → **pass / block**.
- *When:* the gate step, before execution (e.g. before an email is sent).
- *Example:* `dlp.basic(emailBody)` → block if it contains card numbers or secrets. May call an external DLP service, but returns a deterministic verdict.

**Disclosure sink** — declared in `sinks`; referenced by a Stele `disclosure` gate's `allowSink`.
- *What:* a named **destination** a read's result is allowed to flow to; the gate checks the result's sensitivity against the allowed sinks.
- *Receives → returns:* the result's classification + intended destination → **allowed / withhold**.
- *When:* on a read — before execution if sensitivity is known from the registry (pre-check), else on the return path (post-check). See Stele §7.12.
- *Example:* `careTeam` — a `restricted` patient record may only be returned to the care team; any other sink ⇒ result withheld.

**Post-action / effect handler** — declared in `handlers` (and the action's `connector`); referenced by an action's `postActions` or `connector`.
- *What:* the code that **actually performs** an effect, or a follow-up step after one.
- *Receives → returns:* the resolved action + context → a **result** (success/failure); may set derived fields.
- *When:* after the action passes all gates; for effects, inside the staged dispatch (outbox).
- *Example:* the `ledger-pay` connector executes the wire; `recordLedgerEntry` writes the double-entry line afterward; `dispatchSMTP` sends the email. May call external systems.

### Registered functions are part of the trust surface — conformance & review

Bucket B is hand-written, security-critical code the gateway invokes on every matching action, so it gets the same treatment as a policy:

1. **Prefer the stock factories.** The common shapes ship pre-written and pre-verified in `stonefold_gates.stock` — `resource_state_in` (state membership), `cooling_off_elapsed` (the new-payee pattern, RFC §14.4), `data_field_present` (explanation-required). Each is pure, deterministic, and **fails closed** (missing field / unparsable value / absent injected clock ⇒ `False`, never an exception). Most deployments should write no bespoke check code at all.
2. **Run the conformance kit over anything bespoke.** `stonefold_gates.conformance` is a test-time harness (`check_precondition` / `check_content_hook` / `check_scope_predicate` + `assert_conformant`) that holds each function to the contract this section states: **deterministic** (same input ⇒ same result), **total** over its golden cases (an exception is a *dependency failure* that trips `failureMode` — never how a verdict is expressed), **read-only** (inputs are not mutated), and **golden-pinned** (the author declares expected results for known inputs). A deployment SHOULD keep a conformance test per registered function in its own suite.
3. **Review and sign like a policy.** A registered function can widen scope or pass a gate just as surely as an `allow` line; where policy signing is enabled (docs/07 §5), the registered-function set SHOULD be part of the signed bundle, and a change to one SHOULD get the same review as a policy change.

---

## 7. How the three layers line up

For the `pay` action:

| Layer | What it says about `pay` |
|---|---|
| **Registry** (here) | `Payment.pay` is an `effect`, irreversible, high operative force, parameters `{amount, currency}`, resolves `payee`, served by the `ledger-pay` connector. |
| **SIF** (intent) | the agent may emit `{ kind:"effect", entity:"Payment", action:"pay", data:{…}, resolve:{payee:…} }`. |
| **Stele** (policy) | `allow: effect:[pay]` + gates (`valueLimit`, `dualAuthorization` over $10k, sanctions `denylist`, new-payee `precondition`). |

One name, three concerns: *defined* in the registry, *expressed* via SIF, *governed* by Stele.

---

## 8. Validation
A registry MUST pass `schema/registry.schema.json` (structure) plus these checks: every `type`/`entity` reference resolves; every `transition` has `from`/`to` within the entity's declared states; every `connector`/`scopePredicate`/`preconditionCheck`/`hook`/`sink`/`namedSet` referenced by an action or by a companion policy is declared; every declared `compensation` names a resource+action that exists (Stele §13 rule 10); action `kind`s are valid; attribute values are in their allowed sets (SIF §2 / Stele §5).

**Action-name uniqueness.** Action names SHOULD be unique per kind across the registry. A name declared by more than one resource (e.g. an `effect` called `exportData` on two entities) makes a policy's bare-name grant apply everywhere the name is declared — which is what a bare-name `deny` wants, but makes a bare-name `allow` ambiguous (Stele §6.1 / lint rule 12); policies over such a registry should use the `{ Entity: [names] }` map form.

**`deny` names must exist too (Stele §13.1, CS-016).** v1.0 carved out an exception letting `deny` reference undeclared actions ("forbid it before it exists"). It is removed: a deny of an unknown name adds no protection — default-deny already refuses anything undeclared — and is almost always a typo that would silently become a no-op. **You deny things that exist**: to pre-forbid a capability, declare the action in the registry and deny it in the policy (the pattern the worked registries use — `Prescribing.prescribe`/`discontinue` exist precisely so the ward-nurse policy can deny them). Adding a dangerous action to the registry then surfaces every policy that must be reviewed, instead of silently activating alongside a stale deny.

Worked registries: [`../examples/payments.registry.yaml`](../examples/payments.registry.yaml) (the demo domain) and [`../examples/ward-nurse.registry.yaml`](../examples/ward-nurse.registry.yaml). Each pairs with the policy of the same name.

---

## 9. Authoring tooling — drafting a registry from what you already have

Writing a registry from scratch is the adoption cost of the whole model, so the repo ships an **authoring-time generator**, `src/stonefold_registry_gen/` (`python -m stonefold_registry_gen`). It drafts a registry in this spec's format from artefacts an integrator already has:

| Input | What it becomes |
|---|---|
| **SQL DDL** (`CREATE TABLE` dump) | entities with typed `properties` (SQL → `int`/`decimal`/`boolean`/`dateTime`/`string`, `NOT NULL` → `required`); `tenant_id`-style columns get a *scope-key* hint, `*_id` columns a *reference* hint |
| **OpenAPI spec** | `GET` → the entity only (reads are implicit, §4); `PUT`/`PATCH`/`DELETE` → `record` actions; `POST` → kind guessed from the `operationId` verb; request-body schemas → typed `data` |
| **MCP tool list** (`tools/list` output) | each tool name split verb + entity (`send_email` → an `effect` on `Email`); `inputSchema` → typed `data` |

Three rules keep it safe:

1. **The output is a draft, and looks like one.** Every guessed kind and every suggested attribute carries a `TODO(review)` marker; the header is a review checklist. A human MUST review, complete, and sign the result — exactly like a hand-written registry.
2. **Unknown verbs draft as `effect`** — the most-gated kind — so an unrecognised capability is *over*-governed until a human classifies it, never under-governed. Dangerous-looking verbs (`send`, `pay`, `wipe`, …) get a suggested `reversibility: irreversible` to confirm.
3. **The generator is never in the enforcement path.** It runs at authoring time only; drafts are schema-validated (`schema/registry.schema.json`) before they are written, and the linter still gates the reviewed result at load.

```
python -m stonefold_registry_gen sql     schema.sql --domain payments -o draft.registry.yaml
python -m stonefold_registry_gen openapi api.yaml   --domain ledger
python -m stonefold_registry_gen mcp     tools.json --domain crm
```

**Handler stubs (the code behind the declaration).** Drafting the registry solves the
blank-page problem for *what* the domain declares; the larger adoption cost is the *code*
that implements it — the connectors, scope predicates, and precondition checks of §5–6.
The generator drafts that too, from the same inputs: `--stubs handlers.py` on any draw
command emits a **CRUD connector** stub (SQL) or an **HTTP-dispatch** stub (OpenAPI/MCP)
plus a **scope-predicate** stub for every tenancy/ownership column, and the `stubs`
command emits a signature stub for every name an existing registry already declares
(connectors, `scopePredicates`, `preconditionChecks`, `hooks`):

```
python -m stonefold_registry_gen sql   schema.sql --domain payments --stubs handlers.py
python -m stonefold_registry_gen stubs payments.registry.yaml -o handlers.py
```

The three safety rules above apply unchanged: the stubs are **authoring-time only** (never
imported by the enforcement path), each generated body **raises `NotImplementedError`**
under a `TODO(review)` marker so an un-completed handler is loud and over-governed (a raised
handler is a dependency failure the gateway fails closed on, invariant 7) rather than a
silent allow, and the emitted code is **syntax-validated** before it is written. A reviewer
implements and signs each handler, and keeps a conformance test per handler (§6).
