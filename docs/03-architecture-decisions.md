# Architecture Decisions (ADRs) — pinned

These are decided for this **concept deliverable** (proof-of-concept, not a hardened product). Build on them; don't re-open without a strong reason (leave a note if you must).

## Tech stack (Python)
- **Language/runtime:** Python 3.12, full type hints, `mypy --strict` in CI.
- **Gateway framework:** FastAPI + uvicorn — the HTTP surface for the SIF-native tool endpoint, the MCP proxy, the approvals/kill REST API, and the admin-UI backend.
- **Policy parsing/validation:** PyYAML + pydantic v2 for the typed policy model; structural validation via `jsonschema` against `schema/acp.schema.json`; the semantic linter (RFC §13) on top.
- **Durable state:** PostgreSQL (via SQLAlchemy 2.x / asyncpg) — `audit`, `pending_actions` (outbox), `approvals`, `kill_orders`.
- **Hot state / propagation:** Redis (`redis-py`) — rate/quota/spend counters, kill-order cache invalidation (pub/sub), the kill epoch.
- **Dispatch worker:** an async background task (or a separate process) polling `pending_actions`.
- **MCP:** the official Python MCP SDK for the SIF-native tool and the interception proxy.
- **Tests:** `pytest` + `testcontainers-python` (real Postgres + Redis); `docker compose` for local runs. No live LLM calls in CI (use a fake LLM client in the demo agent).
- **Packaging:** `pyproject.toml`, `src/` layout, `uv` (or pip) for envs.

> **Concept-deliverable note.** This is a PoC: optimise for a readable codebase and a working demo, not for throughput or HA. SQLite MAY be used for the earliest milestones to move fast, **but switch to Postgres by M4/M5** — the outbox dispatch and the kill no-race test depend on real `SELECT … FOR UPDATE` row locking, which SQLite does not provide. Don't try to demonstrate the kill race on SQLite.

## Module layout (Python packages under `src/`)
```
acp/
├── pyproject.toml
├── src/acp_core/        # value types (pydantic), registry, policy compiler, condition engine, pipeline — NO I/O, NO LLM
├── src/acp_gates/       # the 14 gate implementations (depend on acp_core + store protocols)
├── src/acp_store/       # Postgres + Redis adapters: audit, outbox, approvals, kill, counters
├── src/acp_connectors/  # Connector protocol + in-memory, sql, http, email/stub connectors
├── src/acp_gateway/     # FastAPI app: transports (SIF-native tool + MCP proxy), wiring, REST, dispatch worker
├── src/acp_admin_ui/    # minimal UI: trace, approvals inbox, kill button (thin / can come late)
├── src/acp_demo/        # the adversarial demo runner + sample agent (fake LLM client)
└── tests/
```
`acp_core` MUST have **no I/O and no LLM dependency**; it is pure and unit-testable in isolation. This is the trust kernel.

## Key decisions
1. **Gateway is the sole path (chokepoint).** Two transports into `acp_gateway`:
   - **SIF-native:** one generated tool `submit_intent`; the gateway is its executor. Strong coverage (no other path).
   - **Interception (MCP proxy):** terminate the agent's MCP/tool transport, map each call to an ACP action, enforce, forward or refuse. Unmapped tool ⇒ deny. Startup **coverage check** fails if the agent has any tool endpoint that isn't the gateway.
2. **Pipeline is pure and total** (`enforce()` in `acp_core`): resolve → authorize → scope → gates → kill → execute, always ending in an audited decision. Implements RFC §12 / design §3. No LLM call anywhere inside it.
3. **Policy is compiled at load** into an indexed matcher; the linter (RFC §13) runs at load; an invalid policy prevents startup (never fall back to defaults).
4. **Effects via outbox** (`pending_actions`) by default; dispatch worker uses `SELECT … FOR UPDATE` + idempotency key (design §8.4, §9). Approvals and kill are transitions on these rows (design §7, §8).
5. **Scope injection below the model**: actor from the session token / transport, never from the agent payload; scope predicate realised per connector (SQL `WHERE` append; HTTP param; method/function arg). Scope-on-effect = pre-resolution authorization check (design §5).
6. **Condition engine** = small AST + tree-walk interpreter over the frozen grammar (design §10); **no `eval`/exec**. Runtime resolution error ⇒ fail-closed for that gate.
7. **Kill** = durable `kill_orders` + per-process in-memory set + Redis pub/sub for speed + epoch polling for self-heal (design §8). Three check points; the authoritative one is inside the dispatch transaction.
8. **Audit** append-only; the app's DB role has no UPDATE/DELETE on the audit table; settle writes outcome + audit in one transaction (design §11).
9. **OPA/IAM and SIEM are seams, not builds.** Define the protocols; ship the simple built-in policy and a Postgres/file audit sink. Real engines plug in later.

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

## Out of scope for this concept
Full domain-modeling/ontology authoring UX; `assess` explainability tooling beyond the `requireExplanation` gate; multi-agent orchestration / durable workflows; full RBAC/ABAC engine; SaaS multi-tenancy, billing, SSO; auto-generation of policy from a schema; production HA/throughput hardening.
