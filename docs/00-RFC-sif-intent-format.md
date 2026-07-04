# Structured Intent Format (SIF) — Specification v1.0

*The format an AI agent uses to express **what it wants done**, in declared domain vocabulary, without ever expressing **how** to do it. SIF is the contract the agent emits; a deterministic layer translates it into action (or refuses it).*

**Status:** Draft v1.0 (reference specification). **Supersedes** the original seven-verb SIF design (see §9). **Authors:** the agent-platform team.
**Layer:** SIF is the lower layer — it defines *what the agent can say*. The **Agent Control Policy (ACP)** spec ([`01-RFC-agent-control-policy.md`](01-RFC-agent-control-policy.md)) is the upper layer — it defines *what the agent is allowed to do*. ACP references this document for the action kinds and the intent shape.

### Conventions
The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, **MAY** are used as in RFC 2119. A *gateway* is the deterministic component that validates and executes SIF. The *registry* (a.k.a. model) is the declared catalogue of entities, actions, kinds, lifecycle states, and attributes; SIF intents may only reference names the registry declares.

---

## 1. Overview and principle

**The agent never decides how to touch a system. It emits SIF — typed intent over declared vocabulary — and nothing else.** A deterministic executor is the only thing that translates SIF into substrate operations (SQL, an HTTP call, a device command, a payment). The agent cannot express a raw operation: there is no SQL string, no shell, no URL, no "run this" verb anywhere in SIF.

Two consequences follow, and they are the whole point:
- **There is nothing to hijack.** A hallucination or a prompt-injection attack can, at worst, produce a malformed or out-of-scope *intent*, which the gateway rejects — never a raw action.
- **Reach is bounded by the registry.** The agent can only name entities, actions, and values the registry declares. It cannot invent a capability that isn't there.

SIF defines the *shape* of intent. Whether a given intent is *permitted* is the job of ACP (the policy layer); whether it is *correct* is the job of the agent. SIF is containment of *form and reach*, not of judgement.

---

## 2. The five action kinds (canonical definition)

Every SIF operation has exactly one **kind**. The kind is declared per action in the registry; the agent selects from declared actions, it does not invent kinds. These five are frozen — growth happens by adding declared actions, never new kinds.

| Kind | Meaning | Changes the world? |
|---|---|---|
| `observe` | Acquire information: read a record, query data, **passive** sensing, fetch a document. | No (but see note). |
| `assess` | Produce a consequential **judgement**: a score, classification, decision, derived claim. | No external change; creates a new claim others rely on. |
| `record` | Change facts the system owns: create / update / delete / link / unlink stored data. | Owned data only. |
| `effect` | Cause a change in the **external world**: send, dispatch, actuate, pay, drive, transmit — **including emitting sensing** (radar/sonar/probe). | Yes — beyond the system. |
| `transition` | Advance a resource through its declared **lifecycle** (e.g. `draft → signed`). | A declared state change. |

Rules that are part of the format:
- **`observe` is passive only.** Sensing that *emits* (and is thus detectable / consequential) is an `effect`, not an `observe`. The registry's `emission` attribute decides this; the agent does not.
- **`record` built-in actions** are `create`, `update`, `delete`, `link`, `unlink`. (These replace the original SIF's standalone verbs.)
- **`assess`, `effect`, `transition`** name a **registry-declared action** (e.g. `triage`, `pay`, `sign`).
- **A `transition` MUST name a declared transition** whose legal `from`-states the registry defines; the gateway refuses a transition from an illegal state.
- The kind organizes the agent's selection space; it does **not** by itself determine governance weight (that is ACP's concern, via attributes).

---

## 3. The operation shape

A SIF intent is a JSON object. The agent emits one or more operations (§5). Per-operation fields:

| Field | Required for | Meaning |
|---|---|---|
| `kind` | all | One of the five kinds. |
| `entity` | all | The target entity/resource type (a registry name). |
| `action` | `assess`,`effect`,`transition`; and `record` (one of create/update/delete/link/unlink) | The named action within the kind. Omitted for a plain `observe` read. |
| `data` | create/update, `assess`, `effect` | Typed parameter values (field → value), validated against the registry. |
| `filters` | `observe`, and write ops that target by match | Field/value (and dotted-path) constraints selecting the target(s). |
| `resolve` | optional | Resolve a reference by looking up another entity (e.g. set `payee` by finding a Payee by name) — so the agent never handles internal ids. |
| `relations` | `observe` | Related entities to include. |
| `sort`, `limit`, `aggregate` | `observe` | Read modifiers. |

Field/enum values MUST be members of the registry's declared types and value sets. The agent MUST NOT reference a field, entity, action, or value the registry does not declare (§4).

Examples (one operation each):

```json
{ "kind": "observe", "entity": "Patient", "filters": { "name": "John Smith" } }
```
```json
{ "kind": "assess", "entity": "Triage", "action": "triage",
  "data": { "acuity": 2, "explanation": "RR 30, SpO2 88%" } }
```
```json
{ "kind": "record", "entity": "Observation", "action": "create",
  "data": { "type": "bloodPressure", "systolic": 140, "diastolic": 90 },
  "resolve": { "patient": { "entity": "Patient", "filters": { "mrn": "A123" } } } }
```
```json
{ "kind": "effect", "entity": "Payment", "action": "pay",
  "data": { "amount": 800, "currency": "USD" },
  "resolve": { "payee": { "entity": "Payee", "filters": { "name": "Acme" } } } }
```
```json
{ "kind": "transition", "entity": "Order", "action": "sign",
  "filters": { "orderNo": "ORD-77" } }
```

*(Operations are shown unwrapped for brevity. On the wire every intent — even a single operation — is a batch: `{ "operations": [ … ] }`; see §5 and `schema/sif.schema.json`.)*

---

## 4. Vocabulary and enum injection

The set of legal names is **not** open. Entity, action, transition, field, and enum-value names come from the **registry** and are injected into the agent's tool schema as **enums**. Therefore:
- The legal names are **enum-injected** into the agent's tool schema, so a compliant model emits only declared ones; and because every intent is validated against the registry, anything undeclared is **rejected before it can act**.
- There is **no raw-substrate operation** in the surface — no `run_sql`, no `exec`, no free-form command field. Anything that needs real code runs *below* SIF (e.g. as a declared action's handler), authored by the integrator, never by the agent.

Adding a capability means **adding a declared action to the registry**, which flows through the same validated path — not adding a new SIF construct.

> **Authoring the vocabulary (non-normative).** The registry this section presupposes does not have to be written by hand. Drafting it is now largely automatable: an LLM-assisted generator can propose the entities, actions with suggested kinds, and typed attributes from artefacts the integrator already has (an MCP `tools/list`, SQL DDL, an OpenAPI spec), leaving the human the part that was always the real content — reviewing the judgment calls (which kind, what is irreversible, which field is the money, which column is the tenant key). An LLM can assist that review too, by explaining what each declaration will permit, gate, or expose, so the reviewer decides correctly rather than rubber-stamps. The boundary is fixed, however: all such assistance is **authoring-time only** — nothing model-drafted becomes effective without human review and sign-off, and no model ever runs in the enforcement path (ACP invariant: deterministic enforcement). See the registry doc (docs/06 §9) for the shipped generator and its safety rules, and docs/17 §5 for why this dissolves the historical authoring-cost objection to declared vocabularies.

---

## 5. Batching and atomicity

An agent MAY submit several operations as one **batch**:

```json
{ "operations": [ { ...op1... }, { ...op2... } ] }
```

- A batch of `record`/`transition` operations is executed as **one transaction** — all commit or all roll back.
- `resolve` references and ordering within a batch are honoured so later operations can use earlier results.
- `effect` operations cannot be transactionally rolled back; the gateway stages them (commit the intent, then dispatch) and reports the outcome as a `transition` — see ACP §4.4 / the implementation design. SIF only requires that the agent express effects as ordinary operations; durability is the executor's concern.
- Whether a batch is *permitted* is ACP's concern: ACP §12 defines the batch **decision** semantics (the batch is decided atomically — any deny/halt refuses the whole batch before anything commits or stages; a hold stages the effect and commits the batch per ACP §4.4). An agent that wants independent outcomes submits independent intents.

---

## 6. Results and structured errors

Every operation returns either a typed **result** (rows for `observe`, the created/updated entity for `record`, the new state for `transition`, the outcome for `effect`/`assess`) or a **structured error** the agent can learn from.

A SIF error MUST be machine-readable and recoverable, carrying at least:

```json
{ "error": { "code": "UNKNOWN_FIELD", "pointer": "operations[0].filters.owner.email",
              "message": "Patient has no path to 'email'; did you mean 'contactEmail'?" } }
```

The error is returned to the agent as a tool result so it can **self-correct on the next turn** rather than failing opaquely. Refusals from the policy layer (deny / hold / halt) are surfaced in the same recoverable shape (ACP owns their meaning).

---

## 7. Transport bindings

SIF is transport-neutral. Two bindings are defined:
- **SIF-native** — the agent is given a single tool, `submit_intent`, whose schema is generated from the registry (enum-injected names). The agent can emit nothing else; coverage is structural (no other path to act).
- **Interception** — existing tool/MCP calls are mapped to SIF operations by the gateway. Coverage equals what is mapped; an unmapped call MUST be denied.

Transport is otherwise a deployment concern (see the architecture decisions doc).

> **Relation to MCP.** SIF is not a new wire protocol. Concretely, the SIF-native binding is just an **MCP server (or equivalent tool interface) that exposes exactly one tool — `submit_intent` — whose schema is generated from the registry**. The contribution is the *shape* of that surface — a single, registry-typed intent tool with enum-injected names — not the transport: MCP already carries typed tool schemas; SIF narrows the agent to one registry-derived tool instead of many separately-registered ones. The interception binding rides ordinary MCP/tool transport too.

> **On the SIF "schema".** The schema the agent is actually validated against is **generated from the registry** at startup (enum-injected names) — it is not a static file, because it must contain this domain's names. A thin static `schema/sif.schema.json` covers only generic L1 shape-checking. How the registry, SIF, and ACP schemas relate and run at runtime is set out in [`07-artifacts-and-schemas.md`](07-artifacts-and-schemas.md).

---

## 8. Relationship to ACP

SIF and ACP compose and MUST stay consistent:
- **SIF** defines *what the agent can express* — the five kinds, the operation shape, the vocabulary. (Canonical home for the kinds.)
- **ACP** defines *what is permitted* — `allow`/`deny`/`scope`/`gates` over SIF operations, the governance **attributes**, and the decisions (`allow`/`hold`/`deny`/`halt`). ACP references this document for the kinds and the intent shape; it does not redefine them.
- The **gateway** = a SIF executor with ACP enforcement in the path: validate the SIF shape → authorize & gate per ACP → execute → record.

A programmatic client MAY emit SIF directly (the LLM agent is just one SIF producer); ACP governs every producer equally.

---

## 9. What changed from the original SIF design (superseded)

This v1.0 supersedes the original SIF design (the seven-verb, ontology-framed version). Differences:
- **Seven verbs → five kinds.** `find/create/update/delete/link/unlink/transition` becomes `observe / assess / record / effect / transition`. The CRUD verbs collapse into `record`'s built-in actions; **`assess`** (consequential judgement) is added; **`effect`** generalizes side-effecting actions **beyond data** (mail, devices, payments, emitting sensing).
- **"Ontology" → "registry / model."** Same idea (typed domain model with states and attributes), plainer name; no reasoning/OWL semantics implied.
- **Governance attributes and the policy surface moved out** into the ACP spec, leaving SIF focused on the intent format.
- The intent/execution principle, enum injection, structured recoverable errors, and batch-as-transaction are **retained unchanged in spirit**.

---

## 10. Quick reference

**Kinds (frozen):** `observe` · `assess` · `record` (create/update/delete/link/unlink) · `effect` · `transition`
**Operation fields:** `kind` · `entity` · `action` · `data` · `filters` · `resolve` · `relations` · `sort` · `limit` · `aggregate`
**Batch:** `{ "operations": [ … ] }` — record/transition ops are one transaction.
**Invariants:** the agent can only name registry-declared things; there is no raw-substrate verb; errors are structured and recoverable.
**Layering:** SIF = what can be said; **ACP** = what is allowed; the gateway executes SIF under ACP.
