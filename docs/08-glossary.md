# Concepts & Glossary — every term, in plain language

Every concept in the system, defined the same way: **one plain sentence + a concrete example**, with a pointer to the spec for detail. If a term anywhere in the docs is unclear, it's defined here.

## The big picture (one paragraph)

An **agent** (an LLM) proposes what it wants as a **SIF intent** (typed, in domain words). The **gateway** checks that intent against the **registry** (is it valid for this domain?) and the **ACP policy** (is it allowed?), injects the caller's **scope**, runs **gates**, and only then **executes** it through a **connector** — recording every attempt in the **audit log**. The agent proposes; a deterministic machine you control disposes.

---

## The three artifacts

- **Registry** (domain model) — *the declared world.* Defines entities, actions, states, attributes, mappings. Authored by the integrator; stable. *Example:* declares that `Payment` has an `amount` and a `pay` action. (See `06`.)
- **ACP policy** — *what's allowed.* `allow`/`deny`/`scope`/`gates` for one agent. Authored by the security officer; changes often; signed. *Example:* "this agent may `pay`, but over $10k needs two approvers." (See `01`.)
- **SIF intent** — *what's wanted.* One batch of typed operations emitted by the agent at runtime; untrusted. *Example:* `{kind:"effect", entity:"Payment", action:"pay", data:{amount:800}}`. (See `00`.)

## The schemas

- **`registry.schema.json`** — validates a registry file (static; you author).
- **`stele.schema.json`** — validates a policy file (static; you author).
- **SIF tool schema** — validates the agent's intent; **generated from the registry** at startup (not a file you write).
- **`sif.schema.json`** — a thin static schema for generic SIF *shape* checks only. (See `07`.)

---

## Action kinds (the 5, frozen)

What kind of thing an action is. *(SIF `00` §2.)*

- **observe** — acquire information, no change. *Example:* read a patient chart; query orders.
- **assess** — produce a consequential judgement. *Example:* compute a triage level; classify a track as hostile.
- **record** — change facts the system owns (create/update/delete/link/unlink). *Example:* log a blood-pressure reading.
- **effect** — cause a change in the external world (incl. emitting sensing). *Example:* send an email; pay a vendor; set a vehicle's speed; switch on radar.
- **transition** — advance a thing through its declared lifecycle. *Example:* `Order: draft → signed`; `Invoice: sent → paid`.

## Governance attributes (the 5)

Declared facts about an action that policies reason over. *(ACP `01` §5.)*

- **reversibility** — `reversible` / `compensable` / `irreversible`. *Example:* a wire is `irreversible`; a refundable charge is `compensable`.
- **emission** — `none` / `emits`. Does the act reveal/transmit even while "just looking"? *Example:* active radar `emits`; reading a file is `none`.
- **operativeForce** — `none` / `low` / `high`. Do others treat the result as authoritative? *Example:* a DNR or a high-alert med order is `high`.
- **resultSensitivity** — `public`…`restricted` (or a domain label). Classification of data a read returns. *Example:* a psychiatric record is `restricted`.
- **explainability** — `none` / `required`. Must the action carry a recorded rationale? *Example:* a triage `assess` is `required`.

## Decisions (the 4)

The gateway's verdict on an action.

- **allow** — permitted; it runs. **hold** — paused for a human (approval). **deny** — refused. **halt** — stopped by the kill-switch.

---

## Gate types (the 14)

Deterministic conditions attached to actions in the policy. Each: what it checks + example. *(ACP `01` §7.)*

- **rate** — frequency ceiling per window. *Ex:* `sendEmail: 20/hour`.
- **quota** — cumulative cap over a longer window/session. *Ex:* `exportReport: 100/day`.
- **valueLimit** — numeric bound on a field. *Ex:* `pay.amount ≤ 1,000,000`.
- **spendLimit** — cost/$ ceiling per task/session. *Ex:* `25/session` — stops retry storms.
- **allowlist / denylist** — membership constraint on a field. *Ex:* recipient domain ∈ corporate-domains; destination ∉ sanctioned-list.
- **precondition** — a named check, or a transition's legal `from`-states. *Ex:* `fiveRightsVerified`; `Order.sign` only from `draft`.
- **contentCheck** — a content hook verdict. *Ex:* `dlp.basic` scans an email body.
- **requireApproval** — pause for a human (conditional). *Ex:* irreversible actions need a supervisor.
- **dualAuthorization** — two distinct humans must approve. *Ex:* wires over $10k need treasury ×2.
- **window** — temporal allow. *Ex:* deploys only Mon–Thu 09:00–16:00.
- **quantityCap** — per-subject cumulative cap. *Ex:* max 3 doses per patient per 24h.
- **disclosure** — bind a read's result classification to allowed sinks. *Ex:* `restricted` records only to `careTeam`.
- **emissionControl** — authorize/deconflict an emitting effect. *Ex:* radar sweep needs `emconAuthorized`.
- **requireExplanation** — the action must carry a rationale. *Ex:* a triage `assess`.

---

## Registered functions (the 5 you implement)

Named code the registry declares and the gateway invokes. Full reference with signatures in `06` §6; in brief:

- **scope predicate** — limits *which records* an actor may touch. *In:* actor → *out:* a filter / authz. *Ex:* `tenantOf(actor)` → `WHERE tenant_id = …`.
- **precondition check** — a yes/no test before an action. *In:* context → *out:* pass/fail. *Ex:* `payeeCoolingOffElapsed`.
- **content hook** — inspects the *payload* of an action. *In:* content → *out:* pass/block. *Ex:* `dlp.basic`.
- **disclosure sink** — a named destination a read's result may flow to. *Ex:* `careTeam`.
- **post-action / effect handler** — the code that performs an effect or a follow-up. *In:* action+context → *out:* result. *Ex:* `ledger-pay` (the wire), `recordLedgerEntry` (the ledger line).

## Connectors / data sources

The adapter that fulfils an action against a real substrate; the agent never sees which. *Examples:* a SQL connector (database), an SMTP connector (email), a method/REST connector (a payment API or external service), a device-driver connector. *(`04` shows many.)*

---

## Core mechanisms

- **Scope injection** — the gateway attaches the actor's scope *below the model*, so the agent can't widen its own access. *Ex:* "find all patients" silently becomes "…on this ward."
- **Enum injection** — the agent's tool schema is generated from the registry, so it can only name declared things — invalid names are unrepresentable. *(SIF `00` §4.)*
- **Outbox / staging** — effects are committed as a `pending` record, then dispatched and settled (`done`/`failed`). This gives durability and is the substrate for **approvals** and **kill**. *(`02` §9.)*
- **Kill-switch** — a flag checked at the chokepoint that turns matching actions into an audited `halt`; stops anything not-yet-dispatched (can't un-send what already left). *(`02` §8.)*
- **Audit log** — every attempt (allow/hold/deny/halt) recorded as a structured entry, transactionally with effects. Answers "what did the AI do, and who let it?" *(ACP `01` §11.)*
- **Approval / dual-authorization** — an action HOLDs as a staged row; a human (or two) releases it. *(`02` §7.)*
- **Named sets** — reusable allow/deny lists referenced by gates. *Ex:* `sanctioned-list`, `corporate-domains`.
- **Value sets** — reusable enums for properties. *Ex:* `currentState: [draft, sent, paid]`.
- **Policy signing** — the gateway only runs policies signed off by an authority, so a tampered or self-modified rulebook can't take effect. *(Status and semantics: `07` §5 — the single home for signing.)*
- **Agent Policy Officer** — the accountable person who reviews, tests, and signs off a policy before it runs.

---

## The pipeline (how a request flows)

Resolve (what is it? — registry) → Authorize (allowed? — ACP `allow`/`deny`) → Scope (inject the actor's limits) → Gates (limits, checks, approvals) → Kill check → Execute (via connector; effects staged) → Record (audit). The first failure stops it; everything is logged. *(ACP `01` §12 / `02` §3.)*

---

## One-line map of the docs

`00` SIF (what the agent says) · `01` ACP (what's allowed) · `02` implementation (how it runs) · `03` architecture (the stack) · `04` domains (it's not just databases) · `05` demo (the runnable money demo) · `06` registry (how to declare a domain) · `07` artifacts & schemas (how the pieces relate) · `08` this glossary · `09` mental models (the confusions, head-on) · `10` positioning (why not OPA/Cedar/IAM alone) · `11` delegation & multi-agent (exploration) · `12` conformance TCK (certify any gateway, any language).
