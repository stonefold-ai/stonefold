# 12 — The ACP Conformance Test Kit (TCK)

*How a gateway — in **any language** — proves it conforms to the ACP RFC.*

The TCK (`src/acp_tck/`) is an implementation-independent, black-box test suite. You do not port the reference gateway's tests; you implement ONE small adapter — the **driver** — and the kit runs every acceptance scenario against your gateway, then reports which **conformance profiles** you certify. The Python reference implementation is certified by the same kit (`tests/test_tck_reference.py`), both in-process and through the wire binding.

---

## 1. How to certify a new gateway (the short version)

**If your gateway is Python:** implement the `acp_tck.driver.ConformanceDriver` protocol (≈200 lines of test-only glue — `acp_tck/adapters/reference.py` is the worked example) and run:

```python
from acp_tck import run_conformance
from my_gateway.tck_adapter import MyDriver

report = run_conformance(MyDriver(), implementation="my-gateway 0.1")
print(report.render())
```

**If your gateway is Java / Go / Rust / anything else:** expose the **TCK harness API** (§6) in a *test build* of your gateway — fifteen small JSON endpoints — start it, and run:

```python
from acp_tck import run_conformance
from acp_tck.http_driver import HttpDriver

driver = HttpDriver("http://localhost:9099")
report = run_conformance(driver, implementation=driver.implementation_name())
print(report.render())
```

Either way the output is the same report:

```
ACP TCK conformance report -- implementation: my-gateway 0.1
[core]    CERTIFIED -- 12 pass, 0 fail, 0 skip
[lint]    CERTIFIED -- 6 pass, 0 fail, 0 skip
...
Certified profiles: core, lint, scope, staging, kill, audit, freshness
```

A profile is **certified** only when every one of its checks passed. A check skipped for a missing capability leaves the profile *incomplete* — a skip is never a pass, so a certification claim is always exactly as strong as what actually ran.

**The conformance claim format:** *"`<implementation>` certifies ACP TCK profiles `<list>` at RFC `<version>`, kit version `<git ref>`."* Publish the rendered report alongside.

---

## 2. What the driver is (and is not)

The driver is a **test-only adapter** over your gateway: it loads a registry+policy, seeds rows into the store behind the connectors, submits intents *as an authenticated actor*, steps the dispatch worker, and exposes what happened (effects that left, audit records written). It is the *test harness's* hands — it is **not** part of your gateway, and the harness API must never exist in a production build (it can reset state and seed data by design).

Driver obligations (the contract is `acp_tck/driver.py`, one docstring per method):

| Method | Obligation |
|---|---|
| `load(registry_yaml, policy_yaml)` | (Re)configure with these fixtures; **reset all state**; refuse invalid policies (`ok=False`) |
| `set_clock(now)` | Pin the injected clock every time-based gate reads (the RFC already mandates an injected clock) |
| `seed(resource, rows)` | Load rows into the store behind that entity's connector |
| `submit(actor, session_id, op)` | Submit one operation; **identity comes from this call, never the payload** (invariant 3) |
| `approve/reject(ticket, approver)` | Resolve a held action; `False` when refused (e.g. self-approval) |
| `dispatch_once()` | Run the staged-effect worker **synchronously to completion** |
| `effects()` | Every effect that actually left the gateway, in order |
| `kill(...)/lift(id)` | Issue/lift kill orders (global/agent/session/action_class) |
| `audit()` | The decision log since `load` (decision, resource, action, outcome, reason) — `reason` is the deciding rule/settle reason; the v0.4 reasons (`stale-decision`, `stale-guard:<gate>`, `scope-lost`) are normative and MUST be populated by drivers claiming the v0.4 capabilities |
| `inject_dispatch_failure(action)` | Make the next dispatch of that action fail at the connector |
| `update_named_set(name, values)` | Replace a registry named set's values at runtime — a sanctions update landing between decision and dispatch (`freshness` capability) |
| `capabilities()` | Which optional obligations you support (missing ⇒ dependent checks SKIP) |

Two capabilities are the v0.4 opt-ins: **`freshness`** declares that decision TTLs + volatile-gate re-validation are wired *with the REQUIRED TCK config* — default TTL **24 hours**, irreversible TTL **30 minutes** (the D5/D6 checks advance the clock against exactly these values, the same way §3 fixes the registered-function semantics); **`scope-reassert`** declares that the scope predicate is re-asserted at dispatch (either declared form — the TCK observes only the shared outcome).

Determinism is the design principle: `dispatch_once` steps the worker instead of racing a background thread, and `set_clock` removes wall-time — so every check is reproducible on any implementation.

## 3. Required registered-function semantics

The fixture pack (`acp_tck/fixtures.py`) references five registered names. Your driver must register implementations with **exactly** these semantics for the TCK run (they are deliberately trivial — the kit tests your *gateway*, not your DLP vendor):

| Name | Kind | Required behaviour |
|---|---|---|
| `tckOwnedBy` | scope predicate | row visible iff `row.owner_id == actor.id` |
| `tckTenantOf` | scope predicate | row visible iff `row.tenant == actor.claims["tenant"]` |
| `tck.rejectMarker` | content hook | BLOCK iff the payload contains the string `BLOCK-ME` |
| `tck.flagSet` | precondition check | pass iff the resolved target's `flag` is true |
| `tckSink` | disclosure sink | the only sink a `restricted` read may flow to |

The fixture registry ships in the **authoring format** (docs/06) — the spec's format — so every implementation adapts from the same artefact. (The reference driver converts it to its loader dialect in ~30 lines; see `authoring_to_compact`.)

## 4. Profiles and what they prove

| Profile | Checks | Proves |
|---|---|---|
| `core` | A1–A3, C1–C9 | default deny, deny-wins, gate AND-combination; valueLimit, rate, allow/denylist, from-states, quantityCap, disclosure, contentCheck, fail-closed conditions, named preconditions |
| `lint` | A4–A8 | invalid policies refuse to load (open-on-irreversible, unknown names incl. `deny`, standing∩deny, dual-auth quorum); warnings surfaced |
| `scope` | B1–B3 | scope injected below the model; effects on out-of-scope targets denied pre-dispatch; payloads cannot widen scope |
| `staging` | D1–D4 | effects staged by default and dispatched exactly once (idempotent); approvals hold/release/reject; dual-auth needs two distinct non-actor identities; failed irreversibles stage their declared compensation |
| `kill` | E1, E2s, E6 | session/agent/action-class kills → HALT; kill before the dispatch step cancels; a committed effect is never claimed reversed; lifting restores |
| `audit` | F1, F2c | every decision leaves a record; executed effects and success-audit records agree exactly |
| `freshness` | D5, D5b, D6, D6b, D6c, B4 | v0.4 (CS-017/018): an expired decision cancels at claim (`stale-decision`) and a late approval cannot resurrect it; a denylist update between decision and dispatch cancels (`stale-guard:denylist`); counters and approval grants are NOT re-run; a target reassigned after the decision never receives the effect (`scope-lost`) |

## 5. What the TCK deliberately does NOT test (and why)

Honesty is the product's brand; it is also the kit's. Three RFC guarantees are not black-box observable, and pretending otherwise would sell false certification:

1. **The true kill no-race (E2, CS-004).** Whether the kill re-check and the `pending → dispatching` transition share one serialised transaction is a concurrency property *inside* your dispatcher. The TCK asserts the serialized contract at both interleaving boundaries (`E2s`); the concurrent race test remains an implementation-internal obligation (the reference keeps one over real Postgres row locks — `tests/test_m5_kill_race.py`).
2. **Transactional audit (F2, CS-006).** Crash-consistency between the settle and the audit write needs fault injection inside your process. The TCK asserts the observable consequence (`F2c`: effects ⇔ success records, exactly); keep a crash-consistency test in your own suite.
3. **Multi-instance kill propagation (E3, CS-007)** and **dependency-failure modes (C7/E5/F3)** need infrastructure control the kit doesn't assume. Capability hooks may add these later.
4. **The declared residual window in the audit record (B5's second clause, CS-018).** The TCK's normalized audit shape doesn't carry `scopeApplied`, so which reassertion *form* ran is not asserted black-box; both forms are covered through their shared observable (the effect does not land, settle reason `scope-lost`). Keep a window-declaration test in your own suite (the reference does — `test_v04_scope_norace.py`).

A certification claim therefore reads "certifies TCK profiles X, Y, Z" — never "proves the RFC".

## 6. The wire binding (multi-language)

The harness API is the driver contract as fifteen JSON endpoints — the full table with request/response shapes is in `acp_tck/http_driver.py`'s module docstring, and `acp_tck/adapters/http_harness.py` is the golden FastAPI example serving the reference. A non-Python gateway implements the same endpoints in its test build; `HttpDriver` does the rest. The whole suite runs through this path in CI (`test_wire_binding_certifies_end_to_end`), so the wire protocol itself is conformance-tested.

Rules: the harness is **test builds only**; every endpoint returns 200 with a JSON body; timestamps are ISO-8601; a capability you don't advertise may leave its endpoint unimplemented.

## 7. Versioning

The kit certifies against the RFC version pinned in this repo (v0.4 today). This is also the worked example of how the kit absorbs an RFC bump: v0.4's guarantees arrived as a **new profile** (`freshness`) behind **new capabilities** (`freshness`, `scope-reassert`) — a v0.3-level gateway still certifies the six original profiles unchanged (its missing capabilities SKIP the new checks, leaving `freshness` honestly incomplete), while a gateway claiming v0.4 certifies the seventh on top. Certifications stay meaningful because they name their profiles and kit version.
