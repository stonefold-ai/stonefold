# Architecture Decisions (ADRs) — pinned

These are decided for this **concept deliverable** (proof-of-concept, not a hardened product). Build on them; don't re-open without a strong reason (leave a note if you must).

## Tech stack (Python)
- **Language/runtime:** Python 3.12, full type hints, `mypy --strict` in CI.
- **Gateway framework:** FastAPI + uvicorn — the HTTP surface for the SIF-native tool endpoint, the MCP proxy, the approvals/kill REST API, and the admin-UI backend.
- **Policy parsing/validation:** PyYAML + pydantic v2 for the typed policy model; structural validation via `jsonschema` against `schema/stele.schema.json`; the semantic linter (RFC §13) on top.
- **Durable state:** PostgreSQL (via SQLAlchemy 2.x / asyncpg) — `audit`, `pending_actions` (outbox), `approvals`, `kill_orders`.
- **Hot state / propagation:** Redis (`redis-py`) — rate/quota/spend counters, kill-order cache invalidation (pub/sub), the kill epoch.
- **Dispatch worker:** an async background task (or a separate process) polling `pending_actions`.
- **MCP:** the official Python MCP SDK for the SIF-native tool and the interception proxy.
- **Tests:** `pytest` + `testcontainers-python` (real Postgres + Redis); `docker compose` for local runs. No live LLM calls in CI (use a fake LLM client in the demo agent).
- **Packaging:** `pyproject.toml`, `src/` layout, `uv` (or pip) for envs.

> **Concept-deliverable note.** This is a PoC: optimise for a readable codebase and a working demo, not for throughput or HA. SQLite MAY be used for the earliest milestones to move fast, **but switch to Postgres by M4/M5** — the outbox dispatch and the kill no-race test depend on real `SELECT … FOR UPDATE` row locking, which SQLite does not provide. Don't try to demonstrate the kill race on SQLite.

## Module layout (Python packages under `src/`)
```
stonefold/
├── pyproject.toml
├── src/stonefold_core/        # value types (pydantic), registry, policy compiler, condition engine, pipeline — NO I/O, NO LLM
├── src/stonefold_gates/       # the 14 gate implementations (depend on stonefold_core + store protocols)
├── src/stonefold_store/       # Postgres + Redis adapters: audit, outbox, approvals, kill, counters
├── src/stonefold_connectors/  # Connector protocol + in-memory, sql, http, email/stub connectors
├── src/stonefold_gateway/     # FastAPI app: transports (SIF-native tool + MCP proxy), wiring, REST, dispatch worker
├── src/stonefold_admin_ui/    # minimal UI: trace, approvals inbox, kill button (thin / can come late)
├── src/stonefold_demo/        # the adversarial demo runner + sample agent (fake LLM client)
├── src/stonefold_registry_gen/# AUTHORING-TIME registry drafting (SQL/OpenAPI/MCP -> draft registry; docs/06 §9) — never imported by the enforcement path
└── tests/
```
`stonefold_core` MUST have **no I/O and no LLM dependency**; it is pure and unit-testable in isolation. This is the trust kernel.

## Key decisions
1. **Gateway is the sole path (chokepoint).** Two transports into `stonefold_gateway`:
   - **SIF-native:** one generated tool `submit_intent`; the gateway is its executor. Strong coverage (no other path).
   - **Interception (MCP proxy):** terminate the agent's MCP/tool transport, map each call to a declared action, enforce, forward or refuse. Unmapped tool ⇒ deny. Startup **coverage check** fails if the agent has any tool endpoint that isn't the gateway.
2. **Pipeline is pure and total** (`enforce()` in `stonefold_core`): resolve → authorize → scope → gates → kill → execute, always ending in an audited decision. Implements RFC §12 / design §3. No LLM call anywhere inside it.
3. **Policy is compiled at load** into an indexed matcher; the linter (RFC §13) runs at load; an invalid policy prevents startup (never fall back to defaults).
4. **Effects via outbox** (`pending_actions`) by default; dispatch worker uses `SELECT … FOR UPDATE` + idempotency key (design §8.4, §9). Approvals and kill are transitions on these rows (design §7, §8).
5. **Scope injection below the model**: actor from the session token / transport, never from the agent payload; scope predicate realised per connector (SQL `WHERE` append; HTTP param; method/function arg). Scope-on-effect = pre-resolution authorization check (design §5).
6. **Condition engine** = small AST + tree-walk interpreter over the frozen grammar (design §10); **no `eval`/exec**. Runtime resolution error ⇒ fail-closed for that gate.
7. **Kill** = durable `kill_orders` + per-process in-memory set + Redis pub/sub for speed + epoch polling for self-heal (design §8). Three check points; the authoritative one is inside the dispatch transaction.
8. **Audit** append-only; the app's DB role has no UPDATE/DELETE on the audit table; settle writes outcome + audit in one transaction (design §11).
9. **OPA/IAM and SIEM are seams, not builds.** Define the protocols; ship the simple built-in policy and a Postgres/file audit sink. Real engines plug in later.
10. **Kill is two independent axes** — the operator emergency hard-kill is **unconditional and independent of `killable`**; the `killable` tag is a separate *manner-of-stopping* declaration, never an operator veto. See the dedicated section below.
11. **Identity is a seam too (same rule as decision 9).** An `IdentityProvider` protocol sits *ahead* of the pipeline and is the sole source of the authenticated `actor:`/`agent:` identities the session carries. Built-in and default: the existing session/transport authentication — the gateway is fully standalone. The seam exists so a credential-based verifier (an agent-passport scheme, W3C Verifiable Credentials, SPIFFE, mTLS identity) can stand in the same slot; protocol + fakes only, **no DID/VC stack is built or planned as a dependency**. Invariant unchanged and non-negotiable: identity comes from the authenticated layer below the model — never from the agent payload, whichever provider established it.

### Integration seams — standalone by default, connectable by design

Every place an external system can plug in, and what runs there when nothing does. The gateway MUST be fully functional with only the built-ins (no seam is a required dependency); each plug-in runs at the gateway's chokepoint, under its `failureMode`, onto its audit record.

| Seam | Built-in default (standalone) | What can plug in |
|---|---|---|
| Identity in (`actor:`/`agent:`) | session/transport auth | passport/VC verifier, SPIFFE, mTLS (decision 11) |
| Authorization step (RFC §12 step 2) | built-in matcher | OPA / Cedar / org IAM (decision 9) |
| Scope predicates (RFC §6.3) | registered functions | org entitlement / tenancy service |
| Content hooks (`contentCheck`, §7.7) | none required by default | DLP, moderation, fraud scoring |
| Precondition checks (§7.6) | registered functions | the system of record |
| Outer ring | gateway's own scoped credentials | cloud IAM (docs/10 §3) |
| Audit out (§11) | Postgres/file sink | SIEM, evidence-pack export |
| Transport | SIF-native MCP server / interception | any MCP-speaking agent stack |

## Kill is two axes — operator hard-kill vs `killable`

- **Operator hard-kill is unconditional.** A human operator can always halt a scope (fleet / gateway / agent / session / action-class) immediately and lift it later. It does **not** consult `killable` and a policy cannot opt out — otherwise a compromised agent could declare itself un-killable, and an operator can pull the process/credentials/network regardless. Invariant; built today.
- **`killable` is a *manner-of-stopping* declaration, not an operator veto.** `killable: false` = "a generic mid-flight freeze is unsafe; use the declared safe-stop/compensation." It guards *automated* halts and *informs* the hard-kill (warn/confirm), but never blocks it.
- **Distinct from `reversibility`:** `killable` = may a live-halt stop this at all; `reversibility` = how much a kill can claw back once in motion (§9 scope of guarantee).
- **Status / deferred.** Hard-kill built. `killable` is parsed (`effective_killable()`) but **not wired into `enforce()`** — so everything is killable today (safe by default); the `killable`-respecting graceful path is deferred. RFC §9's wording is reconciled with these axes (CS-022, draft v0.5) — the spec no longer reads as if `killable` gates the operator. Still open before wiring: (a) graceful halt as a built feature or a seam; (b) per-action vs per-agent; (c) `killable: false` ⇒ require a declared safe-stop; (d) one bool vs split `emergencyStoppable` / `liveHaltStrategy`.

## Reversibility ≠ stakes — choose the right axis for approval

- **Approval/hold keys on stakes, not reversibility.** `reversibility` drives *recovery* controls only — the compensation mandate (§13 rule 10), the irreversible fail-closed floor (§10), the §13.4 warning. Whether to involve a human is a *stakes* decision: `operativeForce`, `resultSensitivity`, conditions over `data.*`. Pattern: `ward-nurse` (`operativeForce == high`); `support-assistant` corrected to match. `reversible ≠ safe` (a reversible action can have irreversible consequences); the two axes are determined separately though often correlated.
- **No new vocabulary.** "Stakes" composes existing attributes + data conditions; no severity attribute is added (invariant 8).
- **`compensation` is narrow** — a registry-declared, in-system, gateway-routable action (refund, `discontinue`), **not** an out-of-band procedure (backup-restore, clinical antidote). Where recovery is only out-of-band, classify `irreversible`.
- **reversible vs compensable** — if the undo is a *distinct* action, classify `compensable` (and declare it, §13 rule 10); `reversible` = same-action inverse-data / self-undo. (Authoring guidance; not linter-enforced.)
- **`reversibility` is terminal & static** — the worst-case (most-committed) recoverability; the pre-commit cancellable window is a runtime/connector property (§8.5, §9), not the attribute.
- **Deferred:** §13.4 warns on *any* unguarded irreversible (same proxy) — may later accept a content/rate/DLP gate, or scope to high `operativeForce`; left a WARN for now.

## Multi-effect & cascade — scope and decomposition

- **The unit of enforcement is one resolved action.** Compound/batch intents decompose into N independently-staged effects (each its own decision, kill check, audit, `resultRefs`, compensation); bulk-as-one-effect is out of scope. Aggregate/velocity risk is caught by counter gates (`rate`/`quota`/spend), not per-unit attributes.
- **`resultRefs` is a list** (audit record + connector result) — one action may fan out to several records; it is the cross-system lineage/correlation key (CS-009). A fan-out action's `reversibility` is its **worst** sub-effect; its `compensation` covers only the recoverable part.
- **The gateway governs agent→world, not world→world.** `reversibility`, `compensation`, `resultRefs`, and the kill guarantee describe the **direct** effect only; the cascade a committed effect triggers downstream is outside the chokepoint (kill can't stop it; compensation covers the direct effect). Chasing it would make Stonefold a distributed-transaction coordinator — out of scope; the seam is `resultRefs`/`correlationId` (RFC §9, §11).
- **Sagas (multi-intent transactions) are out of scope** — reconstructable/unwindable via `correlationId` + `resultRefs`, but no atomicity guarantee across intents; fault-triggered rollback needs an actor independent of the agent.
- **Decision freshness (BUILT — v0.4 CS-017).** Gates decide at decision time; a staged `allow` would otherwise be decide-time-valid forever (a payee sanctioned, or a balance drained, between approval and dispatch caught only by a kill). v0.4 bounds it: a decision **TTL** stamped at staging (deployment config; short for irreversible effects) plus dispatch-time re-validation of **volatile** gates (denylist/allowlist, window, precondition/emissionControl; never counters/approvals/content), both inside the claim transaction after the kill re-check. Normative: RFC §12/§4.4 (merged from `docs/RFC-changeset-v0.3-to-v0.4.md`); mechanism + wiring: `docs/02` §9.1; scenarios D5/D6.
- **Scope no-race (BUILT — v0.4 CS-018).** `scope-on-effect` was a decision-time pre-check, not re-asserted at the effect's commit, so a change to the authorizing state (account reassigned) in the check→commit window — widened by staging — could let an effect land on un-authorized state. v0.4 closes it: connectors declare `transactional | window`; transactional ones re-assert the scope predicate **inside the effect's transaction** (zero rows ⇒ `FAILED scope-lost`, analogous to the kill no-race §9), window ones get a pre-dispatch target re-resolve and their declared residual window in the audit. Normative: RFC §6.3; mechanism + wiring: `docs/02` §9.2; scenarios B4/B5. Pure read staleness stays out of scope (read-time correctness only).

## Concurrency notes for Python
- The dispatch worker and the kill no-race property rely on **Postgres transactions and `SELECT … FOR UPDATE`**, not on Python threads — so the GIL is irrelevant to correctness here.
- Use `async` for I/O-bound work (FastAPI handlers, DB, Redis). The dispatch worker can be an async loop; for the kill-race test, ensure the row-lock transaction is genuinely serialised by the database.
- Counters in Redis are atomic (`INCR`/Lua), so rate/quota gates are correct under concurrency without app-level locks.

## Incorporated review fixes (from design §14 — apply as you build)
- Distinguish scope-on-read (filter) vs scope-on-effect (authorization check).
- `disclosure` gate has a **pre-check** (sensitivity known from registry ⇒ block before execute) and a **post-check** (row-dependent ⇒ withhold on return) form.
- Effects are **async by default** (return accepted/pending); inline only for cancellable effects.
- Runtime condition resolution error ⇒ **fail closed** for that gate (distinct from "false").
- Interception: unmapped ⇒ deny; flag free-form-string tools as high-risk pass-throughs.
- Audit write shares the transaction with the state change for executed/settled effects.
- Kill propagation: pub/sub **and** epoch polling.

## Registry dialects & derived attributes — deferred

- **Two registry dialects exist today.** `schema/registry.schema.json` + `docs/06` define the **authoring format** (`domain`/`entities`/`namedSets`/`hooks`, attributes under `attributes:`) used by `examples/*.registry.yaml`; `registry/stonefold-registry.yaml` is the gateway loader's **compact internal dialect** (`resources`/`sets`/`contentHooks`, attributes inline) that the code and tests consume. Both declare the same vocabulary. Unifying them (teach the loader the v1.x authoring format, or generate the compact form from it) is **deferred**; until then the authoring format is the documented one and the compact file carries a header note.
- **`derived` expression grammar is deferred.** Derived attributes/properties (`operativeForce: { derived: "isHighAlert ? 'high' : 'low'" }`) are implementation-defined: pure, deterministic, no I/O (docs/06 §4). Freezing a small derivation grammar (like the §8 condition grammar) is deferred.
- **Content-check delegation — TODO (RFC wording).** The gateway can validate structure, limits, and set membership deterministically, but it **cannot judge content** — so an implementation SHOULD (not MUST) provide hooks that delegate content checking to third-party systems (DLP, moderation, fraud scoring), executed at the chokepoint under the gateway's failure mode and audit. The reference already ships the seam (`contentCheck` → `ContentHookRegistry`; conformance contract docs/06 §6; positioning docs/13). The open item is the explicit **SHOULD** wording in `docs/01` §7.7 at the next RFC revision — today §7.7 defines the hook without stating the implementation obligation.

## SIF catalogue presentation at scale — open design item (from the benchmark)

- **The finding (docs/15, realism battery, 2026-07-03 note).** When both surfaces
  carry production-length capability information, per-tool **structured cards** let
  the model disambiguate look-alike capabilities almost completely (MCP back to
  90/90% at N=10/50), while the bench's SIF surface — the same information flattened
  into one long prose list inside a single tool description — scored 80/70% with 15%
  clarify-hesitation. Same content, worse packaging: models are heavily trained on
  discriminating among separate structured tool definitions; a prose wall is
  out-of-distribution (the risk docs/15 §1 pre-registered).
- **The work item: think about redesigning how the generated SIF surface presents
  the capability catalogue at scale** so per-capability signal reaches the model as
  effectively as N tool cards do — without giving up the single-intent-tool
  structural coverage. Candidates (none chosen): lean on the structured
  `x-stonefold-actions` catalogue rather than description prose (the real
  `submit_intent_schema` already carries it — the bench flattening likely
  *under-sells* real SIF); group the catalogue by resource; carry per-action `data`
  schemas in the generated schema; richer enum member descriptions; or a two-step
  select (resource, then its actions). Constraint: SIF RFC §7's shape (one
  registry-generated tool) is the invariant; this is about the generated schema's
  *presentation*, not a new surface.
- **Acceptance test exists:** the benchmark's `--cards realistic` row
  (docs/15 realism battery) — the redesign wins when SIF matches structured-card
  selection while keeping its ~5× token advantage.
- **Assessment (2026-07-03): the current design can plausibly beat MCP here —
  no RFC change needed.** What lost was an implementation choice the RFC does not
  prescribe: the RFC fixes the *shape* (one registry-generated tool, enum-injected
  names) and leaves the catalogue's *presentation* free. JSON Schema already allows
  card-equivalent structure inside one tool: a `oneOf` of
  `{const: <action>, description: …}` entries gives every capability its own
  card-like description, and per-resource branches can carry per-action `data`
  schemas with typed, documented parameters — N structured cards inside one tool,
  with undeclared names still unrepresentable. Qualifications, recorded honestly:
  (a) buying signal costs tokens — the 5.4× advantage shrinks toward maybe 1.5–2×
  at full card richness, but presentation depth is *generated*, so it becomes a
  per-deployment knob (terse for capable models, rich where a small model needs
  help) that per-tool surfaces don't have; (b) models are trained on the tool-cards
  format, so a residual out-of-distribution gap on small models may survive good
  packaging — measurable via the acceptance row, not arguable; (c) the single-turn
  bench cannot see SIF's structured-error self-correction loop (SIF §6) — a wrong
  pair gets a recoverable "no such pair" while a wrong MCP tool call executes the
  wrong tool; the multi-step extension (#6, designed, unbuilt) is what would
  measure it. Next concrete step: implement the `oneOf` catalogue presentation in
  the bench's SIF surface and re-run `--cards realistic` — one run decides.

## Out of scope for this concept
Full domain-modeling/ontology authoring UX; `assess` explainability tooling beyond the `requireExplanation` gate; multi-agent orchestration / durable workflows; full RBAC/ABAC engine; SaaS multi-tenancy, billing, SSO; auto-generation of policy from a schema; production HA/throughput hardening.
