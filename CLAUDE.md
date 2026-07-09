# CLAUDE.md — Stonefold Gateway (concept deliverable)

Project conventions and guardrails for any AI agent working in this repository. Start with the sources of truth below (the `docs/` specs), then the code in `src/`. This is a **proof-of-concept**: prioritise a readable codebase and a working demo over throughput/HA — but never relax the invariants below.

## What this project is
The **Stonefold Gateway**: a deterministic enforcement point between an AI agent and the systems it can act on. It validates each attempted action against a declarative policy, injects identity/scope, runs deterministic gates, executes via connectors, records an audit entry, and supports human approval and a kill-switch. **No language model runs in the enforcement path.**

## Sources of truth (priority order)

> **Specs live in one place only:** [stonefold-ai/spec](https://github.com/stonefold-ai/spec),
> vendored here as the **`spec/` git submodule** (clone with `--recurse-submodules`;
> `git submodule update --init` if `spec/` is empty). There are no copies: `spec/docs/…`,
> `spec/schema/…`, `spec/examples/…` below resolve into the submodule. To change spec
> wording, schemas, or fixtures: commit in the spec repo, then bump the submodule pointer
> here. The runnable TCK code is the reverse case: it lives **here**
> (`src/stonefold_tck/`); the spec repo carries only its specification (spec/docs/12).
1. `spec/docs/00-RFC-sif-intent-format.md` — the SIF intent format (the five kinds + the shape the agent emits), **v1.0**. The lower layer; canonical home for the kinds.
2. `spec/docs/01-RFC-agent-control-policy.md` — Stele policy semantics (*what's allowed*), **v0.5** (changelogs at top); references SIF for the kinds. Deltas for older builds: `spec/docs/RFC-changeset-v0.1-to-v0.2.md` through `spec/docs/RFC-changeset-v0.4-to-v0.5.md`, in order. A Change Set wins on any conflict with older wording.
3. `docs/02-implementation-design.md` — mechanism (*how*). Code snippets there are illustrative pseudocode; realise them in the pinned Python stack.
4. `docs/03-architecture-decisions.md` — pinned stack & layout (Python: FastAPI + pydantic + Postgres + Redis).
5. `spec/schema/sif.schema.json`, `spec/schema/stele.schema.json`, `spec/schema/registry.schema.json` — the JSON Schemas for intents, policies, and registries. Every `spec/examples/*` must validate against the matching schema.
6. `spec/registry/stonefold-registry.yaml` (+ `spec/examples/*.registry.yaml`) — the declared vocabulary (resources, actions with their kind/attributes, states, scope predicates, named sets) a policy resolves against.
7. `spec/examples/*.stele.yaml` — the RFC's worked policies, used as fixtures.
8. `docs/05-demo-spec.md` — the runnable Accounts-Payable demo spec (matches the shipped `demo/`: minimal scripted walkthrough plus the tested attack-refusal, invite-attack, and kill paths).
9. `tests/acceptance-scenarios.md` — the acceptance bar.
10. `spec/docs/12-conformance-tck.md` + `src/stonefold_tck/` — the conformance test kit: how ANY gateway (any language) certifies against the RFC. The kit core imports nothing from the reference; the reference is certified by it (`tests/test_tck_reference.py`), in-process and over the wire binding.

Supporting docs (context, not normative): `docs/04-domains-and-use-cases.md`, `spec/docs/06-registry-domain-model.md`, `spec/docs/07-artifacts-and-schemas.md`, `spec/docs/08-glossary.md`, `docs/09-mental-models.md`, `docs/10-positioning-policy-engines.md`, `docs/11-delegation-multi-agent.md` (exploration), `docs/13-who-is-this-for.md` (industries & buyers), `docs/14-eu-ai-act-mapping.md` (DRAFT — citations unverified), `docs/15-benchmark-design.md` (design only; no results exist), `docs/16-incremental-adoption.md`, `spec/docs/17-interception-mapping.md` (how Stonefold interprets ordinary MCP/tool calls via the declared mapping), `docs/18-obligation-checking-pattern.md` (the v0.6 obligation pattern in plain language), `docs/19-obligation-coverage-check.md` (the pattern scored against the two worked examples), `docs/20-agentic-loop-assessment.md` (why CS-029/030/031 exist), `docs/renaming.md` (the executed ACP → Stonefold/Stele rename record).

## Non-negotiable invariants (treat a violation as a P0 bug)
1. **Deterministic enforcement.** No LLM / nondeterminism inside `enforce()`.
2. **Default deny; deny wins.** Unknown action/resource/tool ⇒ deny.
3. **Scope below the model.** Actor identity comes from the authenticated session/transport, never from the agent payload. The agent cannot set or read its own scope.
4. **Effects are staged.** Every external effect goes through the outbox (`pending_actions`) by default. Inline execution is an explicit opt-in only for cancellable effects.
5. **Kill has no race.** The dispatch-time kill check and the `PENDING → DISPATCHING` transition occur in one `SELECT … FOR UPDATE` transaction (design §8.4). Idempotency key on every staged row. (Use Postgres for this — not SQLite.)
6. **Audit everything.** Allow / hold / deny / halt each write a record; for executed effects the audit write shares the transaction with the settle.
7. **Fail closed.** Any dependency failure (registry, scope, hook, kill store, outbox DB) ⇒ deny/halt unless `failureMode: open` is set for that scope.
8. **Frozen vocabulary.** Do not add action kinds, gate types, attribute names, or condition operators. Extensions go in resources, actions, named sets, scope predicates, and hooks.

## Definition of done (every task)
- Tests written first (from `tests/acceptance-scenarios.md` + the cited RFC section) and passing.
- Full suite green, including integration tests against real Postgres + Redis (`testcontainers-python`).
- All `spec/examples/*.stele.yaml` and `spec/examples/*.registry.yaml` still load and validate against their schemas (`spec/schema/stele.schema.json` / `spec/schema/registry.schema.json`).
- `mypy --strict` clean; no invariant above violated; any unavoidable ambiguity marked `# STONEFOLD-AMBIGUITY:` with the RFC reference.
- Public types/functions typed and docstring'd; a short note on which RFC sections the change implements.

## Build & run (pinned stack in docs/03)
- `uv sync` (or `pip install -e ".[dev]"`) — set up the environment.
- `docker compose up -d` — Postgres + Redis for local runs.
- `uvicorn stonefold_gateway.main:app --reload` — start the gateway.
- `pytest` — unit + integration (testcontainers spins up Postgres + Redis).
- `mypy --strict src` — type check.
- `make demo` — run the scripted adversarial demo (`stonefold_demo`) end to end. The real-LLM Accounts-Payable demo lives in `demo/` (`cd demo && make up && make seed`; see `docs/05`).

## Coding conventions
- Python 3.12, type hints everywhere; `pydantic` models for value types; `Enum` for `Kind` / `Decision` / `Outcome`.
- Use `typing.Protocol` for the `Gate`, `Connector`, and store interfaces (structural typing, easy fakes in tests).
- One module per pipeline stage; each stage's output type is the next stage's only input type (trust boundaries are explicit). Keep `stonefold_core` pure: no I/O, no LLM, no framework imports.
- Gates implement a single `Gate` protocol returning `GateResult` (PASS/FAIL/HOLD) — **never raise to signal a policy decision**; a raised exception means a *dependency failure* and triggers `failureMode`.
- Condition engine: a parser + tree-walk evaluator over the frozen grammar. **No `eval`/`exec`/`compile` of policy expressions** — ever.
- Connectors implement one `Connector` protocol; the in-memory / sql / http / email connectors are separate modules.
- `async` for I/O-bound code; keep the pure pipeline synchronous where it simplifies reasoning.

## What NOT to do
- Don't put policy logic inside connectors (connectors only execute + apply the injected scope filter).
- Don't let any tool/connector be reachable except through the gateway (interception coverage check must fail startup otherwise).
- Don't implement a full RBAC/ABAC engine — ship the simple built-in policy + an OPA/IAM seam (protocol only).
- Don't build orchestration/workflow features — out of scope (see RFC non-goals).
- Don't use SQLite for the kill/outbox milestones — its locking won't demonstrate the no-race guarantee.
