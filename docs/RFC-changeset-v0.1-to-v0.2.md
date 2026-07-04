# RFC Change Set — v0.1 → v0.2

**Purpose.** This is the *delta* for an implementation that already builds RFC v0.1. Apply these items to reach v0.2. The full consolidated spec is `docs/01-RFC-agent-control-policy.md` (now v0.3; its "Changelog — v0.1 → v0.2" table matches this document); this is the actionable work order. To continue past v0.2, apply `docs/RFC-changeset-v0.2-to-v0.3.md` next.

**Scope of the change.** All nine items are **semantic / behavioural** clarifications. **No policy-file syntax changed** — `apiVersion: stele/v0.1` files remain valid, `schema/stele.schema.json` and all `examples/*.stele.yaml` are unchanged. You are changing *gateway behaviour and tests*, not the language. (CS-008 touches the **registry** side: a `compensation` declaration and a linter rule; CS-009 adds a field to the **audit record**.)

**Precedence.** Where this Change Set conflicts with any older v0.1 wording, **the Change Set wins**.

**How to read each item:** `ID — type — §section`. *Was* = v0.1 behaviour; *Now* = v0.2 behaviour; *Impact* = what to build/change; *Test* = the acceptance scenario to add or update (`tests/acceptance-scenarios.md`).

---

## Summary

| ID | Type | § | One line | Test |
|----|------|---|----------|------|
| CS-001 | CLARIFIED | 6.3 | Scope = filter for reads, pre-resolution auth check for effects | B2 |
| CS-002 | CLARIFIED | 7.12 | `disclosure` has pre-check and post-check forms | C6 |
| CS-003 | CHANGED | 4.4 | Effects staged (accepted/pending) by default; inline opt-in only | D1 |
| CS-004 | ADDED | 9 | Kill no-race: locked dispatch transaction + 3 check points + guarantee scope | E2, E1, E4 |
| CS-005 | ADDED | 8 | Condition path null/absent at runtime ⇒ gate fails closed | C8 |
| CS-006 | ADDED | 11 | Audit write shares the transaction with the effect settle | F2 |
| CS-007 | ADDED | 9 | Kill propagation prompt + self-healing; store down ⇒ fail closed | E3, E5 |
| CS-008 | ADDED | 13 | Lint: `compensable` action with no resolvable `compensation` ⇒ error | A4 |
| CS-009 | ADDED | 11 | Audit record gains `resultRefs` (list of downstream ids of a settled effect) | F1, F2 |

*Not in this spec:* interception-mode coverage (unmapped tools deny; flag free-form-string tools) is a transport/architecture concern — see `docs/03-architecture-decisions.md`, key decision 1.

---

## CS-001 — CLARIFIED — §6.3 Scope (reads vs effects)
- **Was:** "A scope on a resource applies to every kind touching it." (Left how-to-enforce unspecified, implying a single mechanism.)
- **Now:** Two enforcement forms of the *same* predicate: for `observe`/`record`/`transition` it is a **filter** (e.g. injected `WHERE`) applied by the connector below the gateway; for an `effect` it is a **pre-resolution authorization check** — resolve the target first, and if it is not in the actor's scoped set, **DENY before dispatch**.
- **Impact:** In the pipeline's scope/execute path, branch on kind. For effects, add a resolve-then-authorize step before staging. Reuse the same scope predicate object.
- **Test:** **B2** (scope on an effect denies acting on an out-of-scope target). Keep B1 for the read-filter form.

## CS-002 — CLARIFIED — §7.12 `disclosure`
- **Was:** `disclosure` described once (implicitly checked against the result).
- **Now:** Two forms. **Pre-check:** when result sensitivity is known from the registry, block **before** executing the read. **Post-check:** when sensitivity is row-dependent, execute, then **withhold** a disallowed result on return and record `deny` with "result withheld." Use the pre-check form whenever sensitivity is knowable without executing.
- **Impact:** `disclosure` gate gains a pre-execution path (registry-known sensitivity) and a post-execution path (on the result). The post-check runs on the *return* leg of `observe`.
- **Test:** **C6** (restricted result withheld on return; audit shows executed+withheld). Add a pre-check variant where the read is blocked before execution.

## CS-003 — CHANGED — §4.4 Effect durability (staging is the default)
- **Was:** "the gateway **SHOULD** stage" effects.
- **Now:** Effects are **staged by default** (`SHOULD` → **MUST**): record + commit the intent (atomically with any `record` ops in the batch), return **accepted/pending**, dispatch asynchronously, settle as a `transition`. Inline synchronous execution is an explicit **opt-in for cancellable effects only**. Staging is the substrate for approvals (§7.8) and kill (§9).
- **Impact:** The `pending_actions` outbox is mandatory for effects. The agent receives a pending receipt, not a synchronous result, by default. Build the dispatch worker now (it's depended on by CS-004).
- **Test:** **D1** (effect staged then dispatched exactly once; idempotency under retry).

## CS-004 — ADDED — §9 Kill no-race guarantee
- **Was:** "A `halt` MUST take effect before the connector dispatch of any pending effect." (Intent stated; mechanism unspecified — a real race.)
- **Now:** Kill is evaluated at **three points** (entry short-circuit; per-action step 5; **at dispatch**). The dispatch-time kill re-check and the staged row's `pending → dispatching` transition **MUST occur in one serialised transaction** (e.g. `SELECT … FOR UPDATE`), so no action can both pass the kill check and remain un-dispatched. Each staged action carries an **idempotency key**. **Guarantee scope:** prevents new/not-yet-dispatched actions; cancels cancellable in-flight; compensates declared irreversibles already dispatched; does **not** reverse a committed external effect.
- **Impact:** Implement the locked-transition dispatch (design §8.4), the in-flight cancellation registry (design §8.5), and the `HALT` terminal decision returned to the agent. Depends on CS-003 (staging).
- **Test:** **E2** (the race test — drive concurrent kill vs dispatch; assert never "passed-and-un-dispatched"), **E1** (subsequent actions HALT), **E4** (in-flight cancellable call aborted).

## CS-005 — ADDED — §8 Condition runtime resolution
- **Was:** Grammar defined; runtime behaviour for a missing/null path unspecified.
- **Now:** Unknown paths are rejected at load (§13.9). A path **absent/null at runtime** makes its gate **fail closed** (DENY), distinct from the condition being `false`. A condition error MUST NOT silently pass a gate.
- **Impact:** In the condition engine's `resolve`, a missing path raises a typed "unresolvable" that the gate engine maps to FAIL (closed) for that gate — not to `false`.
- **Test:** **C8** (missing `resource.foo` in a `when:` ⇒ gate fails closed).

## CS-006 — ADDED — §11 Transactional audit
- **Was:** "Every evaluated action produces one append-only record." (Timing/atomicity unspecified.)
- **Now:** For an executed/settled effect, the audit write **MUST share the transaction** with the state change (the outbox settle) — no effect-without-record, no record-without-effect. Refusals/holds are recorded **before** the result returns to the agent. Best-effort side-channel logging is insufficient.
- **Impact:** Write the audit row inside the settle transaction. Add a crash-consistency check (inject a crash between connector success and audit write; assert consistency on restart).
- **Test:** **F2** (audit transactional with settle; crash-consistency).

## CS-007 — ADDED — §9 Kill propagation
- **Was:** Single-instance kill implied; cross-instance propagation unspecified.
- **Now:** A kill MUST take effect across **all** gateway instances **promptly and reliably** — fast notification (e.g. pub/sub) **plus** a self-healing authoritative re-read (e.g. an epoch counter) so a dropped notification self-corrects. Kill store unreachable ⇒ **fail closed** for irreversible effects.
- **Impact:** Add the pub/sub invalidation + epoch poll to the kill-state cache (design §8.2, §8.9). Add the fail-closed-on-store-down path.
- **Test:** **E3** (global kill propagates to a second instance; self-heal after a dropped message), **E5** (kill store down ⇒ fail closed for irreversible).

## CS-008 — ADDED — §13 Compensable-needs-compensation lint
- **Was:** §5 defined `compensable` as "a declared undo exists," but nothing enforced the declaration.
- **Now:** The linter MUST reject a `compensable` action whose registry entry declares no resolvable `compensation`; and any declared `compensation` MUST name a resource+action that exists in the registry. `irreversible` actions MAY declare a `compensation` but are not required to. (RFC §13 rule 10.)
- **Impact:** Registry action model gains a `compensation: { resource, action }` declaration; the load-time linter gains both checks (a failing policy/registry pair must not load).
- **Test:** **A4** (validation rejects a bad policy at load — add the compensable-without-compensation and dangling-compensation variants).

## CS-009 — ADDED — §11 `resultRefs` on the audit record
- **Was:** The audit record identified the attempt, not the downstream artefact(s) the effect produced.
- **Now:** The audit record gains `resultRefs` — a **list** of the connector-returned identifier(s) of the executed/settled effect's result (payment id, ledger-entry id, message id, …). Plural because one action may fan out to several records; it is the lineage/correlation key an external system uses to locate, reconcile, or compensate the effect. Populated for executed/settled effects; empty otherwise. The gateway records the refs; it does **not** perform reversals.
- **Impact:** Connector result type carries the downstream id(s); the settle transaction writes them into the audit record (with CS-006's transactional guarantee).
- **Test:** **F1**/**F2** (extend: a settled effect's audit record carries the connector-returned `resultRefs`; a refused/held action's record has them empty).

---

## Applying this Change Set
1. Read each item; update or add the cited acceptance scenario test **first**.
2. Implement the behaviour; keep the policy syntax, schema, and example files untouched.
3. Re-run the full suite; confirm all `examples/*.stele.yaml` still validate (they should — no syntax changed).
4. When done, the system conforms to RFC v0.2 (`docs/01`). Note in your commit which CS items it covers.
