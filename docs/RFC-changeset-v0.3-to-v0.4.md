# ACP RFC — Change Set v0.3 → v0.4 (ACCEPTED)

**Status: ACCEPTED — merged into `docs/01` v0.4 (2026-07-02); the RFC text is now normative and this document is the delta record. Both items are implemented in the reference: CS-017 (scenarios D5/D6, `acp_core/freshness.py`, outbox/dispatch) and CS-018 (scenarios B4/B5, connector `ScopeCapability` + `dispatch_scoped`, dispatch worker).** This document promoted the two deferred timing guarantees (`docs/03` → "Decision freshness", "Scope no-race") from *documented boundary* to *specified behaviour*.

**Why these two.** They are the questions a payments/healthcare buyer opens with, and both close the same shape of gap: a fact that was true at **decision time** stops being true before **dispatch/commit time**. v0.3 handles that window honestly (documented, kill-only); v0.4 closes it where it can be closed and prices it where it can't.

**Scope of the change.** No policy-file syntax changes; `schema/acp.schema.json` untouched. CS-017 adds gateway behaviour + deployment config; CS-018 adds a declared connector capability (registry/connector metadata, additive).

---

## Summary

| ID | Type | § | One line | Test |
|----|------|---|----------|------|
| CS-017 | ADDED | 12, 4.4 | Decision TTL + dispatch-time re-validation of **volatile** gates for staged effects | D5, D6 |
| CS-018 | ADDED | 6.3 | Scope no-race: transactional connectors re-assert the scope predicate **inside the effect's transaction**; others declare their residual window | B4, B5 |

---

## CS-017 — ADDED — §12/§4.4 Decision freshness

- **Was (v0.3):** Evaluation runs at decision time; for a staged effect only the **kill switch** is re-checked at dispatch. A fact that changes in the decide→dispatch window (a payee newly sanctioned, an approval granted days ago) is caught only by a kill. Explicitly documented as out of scope (§12 "Decision-time validity").
- **Now (v0.4):**
  1. **Decision TTL.** Every staged action carries an expiry, set at staging from gateway configuration (deployment config, **not** policy syntax — the language stays frozen). The default MUST be finite; for `irreversible` effects it SHOULD be short (minutes–hours, not days). A row claimed after its TTL settles `CANCELLED` with reason `stale-decision` (audited; the agent's ticket resolves to a recoverable refusal). An approval that arrives after expiry does not resurrect the row — the intent must be re-submitted and re-decided.
  2. **Volatile-gate re-validation at dispatch.** Inside the dispatch claim (after the §9 kill re-check, before the connector call), the gateway re-evaluates the action's **volatile** gates: `allowlist`/`denylist` (set membership changes), `window` (time has passed), `precondition` / `emissionControl` checks (world state changes) — including registry-intrinsic preconditions. It MUST do so for `irreversible` effects and SHOULD for all staged effects. A dispatch-time failure settles `CANCELLED` with reason `stale-guard:<gate>` (audited), never a partial dispatch.
  3. **Non-volatile gates are NOT re-run**, by definition: `valueLimit` (the staged `data` is frozen), `contentCheck` (the payload is frozen), `rate`/`quota`/`quantityCap`/`spendLimit` (consumed at decision time — re-running double-counts), `requireApproval`/`dualAuthorization` (the grant *is* the release; its freshness is bounded by the TTL, rule 1).
- **Impact:** staging writes `expires_at`; the dispatch worker's claim transaction gains the TTL check and the volatile-gate re-run (gate engine invoked with a dispatch-time `RequestEnv`); two new settle reasons; audit carries the re-validation outcome. No policy syntax, no schema change.
- **How to enable (reference implementation):** opt-in via `FreshnessConfig` — pass it to `enforce(freshness=…)` (with an injected clock in `RequestEnv.now`) and give the `DispatchWorker` a `clock` plus `make_dispatch_revalidator(engine, policy)`. Full wiring and semantics: `docs/02` §9.1.
- **Test:** **D5** (a staged effect whose TTL lapsed is cancelled at claim, never dispatched; late approval does not resurrect it), **D6** (denylist updated between decision and dispatch ⇒ the staged `pay` settles `stale-guard:denylist`, nothing sent; a fresh submission is denied at decision time).

## CS-018 — ADDED — §6.3 Scope no-race

- **Was (v0.3):** Scope-on-effect is a decision-time pre-check; the predicate is not re-asserted at the effect's commit. The check→commit window (widened by staging) is a documented TOCTOU race: authorizing state can change (an account reassigned to another tenant) and the effect lands on un-authorized state.
- **Now (v0.4):**
  1. **Connectors declare a scope-reassertion capability**: `transactional` (the connector can carry the predicate into the effect's own transaction) or `window` (it cannot; a residual race window remains). Declared once per connector (registry/connector metadata — additive).
  2. For a **`transactional`** connector (SQL-class), the gateway MUST re-assert the scope predicate **inside the effect's transaction** — mechanically, the predicate's constraint is ANDed into the effect's write (`UPDATE … WHERE id = :target AND tenant_id = :actor_tenant`). Zero rows affected ⇒ the effect settles `FAILED` with reason `scope-lost` (audited); the write commits against authorized state **or not at all**. This is the same shape as the kill no-race (§9): the check and the commit share one transaction.
  3. For a **`window`** connector (HTTP, email, device), the decision-time pre-check remains the guarantee; the gateway SHOULD re-resolve the target under scope immediately before dispatch (shrinking the window to connector latency), and the connector's declared window is surfaced in the audit record so the residual risk is priced, not hidden.
- **Impact:** `ScopePredicate` already produces the SQL constraint (`sql_where`) — the SQL connector's `dispatch` applies it inside its transaction; connector metadata gains the capability flag; a new `FAILED` reason; audit notes which form ran. Non-transactional connectors change only by declaring their window.
- **How to enable (reference implementation):** opt-in via the dispatch worker — pass it `scopes=make_scope_resolver(policy)`; connectors declare a `ScopeCapability` and transactional ones implement `dispatch_scoped` (the shipped `SqlConnector` takes `effect_sql` statement templates with a `{scope}` slot). Full wiring and semantics: `docs/02` §9.2.
- **Test:** **B4** (target reassigned between decision and dispatch; transactional connector ⇒ effect does not land, settles `scope-lost`), **B5** (window connector: pre-dispatch re-resolve catches the reassignment; the declared residual window appears in the audit record).

---

## Interactions and non-goals

- **Kill remains the authoritative dispatch check** (§9); CS-017's re-validation runs *inside the same claimed transaction*, after the kill re-check. Order: kill → TTL → volatile gates → connector.
- **This is not dispatch-time re-authorization.** `allow`/`deny` and scope *decisions* are not re-derived; approvals are not re-requested. The TTL bounds how stale any decision may get; re-validation covers only the gate classes whose facts move independently of the agent.
- **Pure read staleness stays out of scope** (§6.3): reads are correct at read time; a stale read still cannot cause an unauthorized effect, because every effect is independently decided.

## Applying this Change Set — all three steps completed
1. ✔ Acceptance scenarios **D5, D6, B4, B5** added first (`tests/acceptance-scenarios.md`; tests `test_v04_freshness.py`, `test_v04_scope_norace.py`, `test_m4_pg_integration.py`).
2. ✔ CS-017 implemented in the outbox/dispatch worker; CS-018 in the SQL connector + connector metadata; policy syntax and schemas untouched.
3. ✔ Both items merged into `docs/01` (§12, §4.4, §6.3, changelog), bumped to **v0.4**; the two `docs/03` "deferred" bullets moved to "built".
