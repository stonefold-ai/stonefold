# ACP RFC — Change Set v0.2 → v0.3

**Purpose.** This is the *delta* for an implementation that already builds RFC v0.2. Apply these items to reach v0.3. The full consolidated spec is `docs/01-RFC-agent-control-policy.md` (v0.3, with a matching changelog); this document is the actionable work order.

**Scope of the change.** v0.3 fixes **internal contradictions found in spec review** — places where the RFC's own examples, the JSON Schema, the fixtures, or the shipped implementation disagreed with the normative text. **No policy-file syntax changed**: `schema/acp.schema.json` is untouched and `apiVersion: acp/v0.1` files remain valid. The one grammar change (CS-013) *widens* what parses. Three new linter rules are the only new gateway behaviour.

**Precedence.** Where this Change Set conflicts with any older wording, **the Change Set wins**.

---

## Summary

| ID | Type | § | One line | Test |
|----|------|---|----------|------|
| CS-010 | FIXED | 7.15, 14.3, 13 | Standing never overrides `deny`; §14.3 corrected to default-deny; lint rule 11 (deny ∩ standing ⇒ error) | A6 |
| CS-011 | FIXED | 7.13 | `emissionControl` syntax is `{ checks: [...] }` (the `{ precondition: [...] }` spelling was schema-invalid) | — (docs) |
| CS-012 | CLARIFIED | 6.1, 13 | Bare-name grant resolution defined; lint rule 12 (ambiguous bare-name `allow` ⇒ warn) | A7 |
| CS-013 | CHANGED | 8 | Grammar: right side of `in`/`not in` may be a function; strings single- or double-quoted | — (parser already conforms) |
| CS-014 | ADDED | 13 | Lint rule 13: `dualAuthorization` with explicit `quorum` < 2 ⇒ error | A8 |
| CS-015 | DOCS | — | Editorial: numbering, cross-refs, worked-example gate coverage, fixture alignment | A5 |
| CS-016 | CLARIFIED | 13 | Rule 1 applies to `deny` too; doc 06's "exception for `deny`" removed | — (linter already conforms) |

*Companion changes outside this RFC (same review):* the Registry spec `docs/06` is now **v1.1** — the attribute-default contradiction is resolved (defaults are the **benign** end, `reversibility: reversible`; danger is declared, the linter guards it), `compensation` is added to `schema/registry.schema.json`, and scope-predicate argument forms plus the `derived` expression boundary are documented. `docs/RFC-changeset-v0.1-to-v0.2.md` was completed with the previously missing CS-008/CS-009 entries.

---

## CS-010 — FIXED — §7.15 / §14.3 / §13 Standing vs deny
- **Was:** §14.3 listed `engage` under `deny` with the comment "default-denied; only 'standing' enables" — but §6.2 says deny always wins, so the standing grant could never fire. (The shipped compiler already implemented deny-wins and carried an `ACP-AMBIGUITY` marker on exactly this conflict.)
- **Now:** Standing grants are conditional *allows*, subject to §6.2 unchanged. A standing-only action is left **out of `allow`** (default-deny covers the off state) and **out of `deny`**. §14.3 and `examples/track-operator.acp.yaml` are corrected. New lint rule 11: an action in both `deny` and a `standing` rule's `enables` ⇒ **error**.
- **Impact:** No authorization-engine change (deny-wins was already built). Implement lint rule 11; remove the compiler's `ACP-AMBIGUITY` marker.
- **Test:** **A6** (a policy with the same action in `deny` and `standing.enables` fails to load).

## CS-011 — FIXED — §7.13 `emissionControl` syntax
- **Was:** §7.13/§14.3 wrote `emissionControl: { precondition: [...] }`, which does not validate against `schema/acp.schema.json` (the gate's value shares the `precondition` definition: `checks`/`from`/`when`). The fixture already used `checks:`.
- **Now:** The RFC text uses `{ checks: [...] }`; also clarified that failed checks resolve `fail`, and `hold` is reserved for a pending authorization decision.
- **Impact:** Documentation only — schema and gate implementation unchanged.
- **Test:** none (covered by A5, examples still load).

## CS-012 — CLARIFIED — §6.1 Bare-name grant resolution
- **Was:** A bare list entry could be a resource or an action name; behaviour with colliding action names (the registry declares `exportData` on two resources) was unspecified.
- **Now:** A bare token matches the **resource** of that name (all of the kind's actions on it, including explicitly declared ones — `observe: [Patient]` grants `readSealed` too) or **any same-kind action with that name on every resource that declares it**. Bare-name `deny` matching all is intentional (broad deny is safe); bare-name `allow` matching more than one resource's action triggers new lint rule 12 (**warn** — use the `{ Entity: [names] }` map form).
- **Impact:** No matcher change (this is what the compiled matcher already does). Implement lint rule 12.
- **Test:** **A7** (an `allow` bare name declared by two resources lints with a warning; the map form does not).

## CS-013 — CHANGED — §8 Grammar amendment
- **Was:** The grammar allowed only a list literal on the right of `in`, yet §7.15's own example used `context.time in window('08:00-18:00')`; quote style for strings was undefined.
- **Now:** `comparison := … | operand ("in" | "not in") (list | function)`; string literals may be single- or double-quoted. No new operator or function.
- **Impact:** None if your parser already accepted the examples (the reference parser did, behind an `ACP-AMBIGUITY` marker — remove the marker). Otherwise extend the `in` production.
- **Test:** covered by the existing condition-parse suite; add `x in window("08:00-18:00")` if missing.

## CS-014 — ADDED — §13 Dual-authorization quorum lint
- **Was:** `dualAuthorization` implies `quorum: 2`, but the schema structurally admits `quorum: 1` and nothing rejected it.
- **Now:** Lint rule 13: `dualAuthorization` with an explicit `quorum` < 2 ⇒ **error**.
- **Impact:** One linter check.
- **Test:** **A8** (a policy with `dualAuthorization: { quorum: 1 }` fails to load).

## CS-015 — DOCS — Editorial repairs
- Section numbering fixed (file structure is §3 with §3.1/§3.2; kinds are §4; the top-level-keys table points `standing` at §7.15 and `defaults` at §9–§11).
- §4.3 lists all five `record` built-ins (`unlink` was missing); §7 names the `Resource.action` gate-key form the examples already used; §7.1 no longer implies rate windows accept the `Ns/Nm/Nh/Nd` duration shorthand.
- §14.1 gains `quota` + `spendLimit` and §14.5 gains `window`, making the "full gate catalog" claim true; §14.3 aligned with `examples/track-operator.acp.yaml` (checks syntax; the undefined `window: {hours: "always"}` removed).
- Fixtures synced: `examples/support-assistant.acp.yaml`, `examples/legal-matter.acp.yaml`, `examples/track-operator.acp.yaml`, and the `INVALID-*` fixture's stale comment.
- **Test:** **A5** (all examples still load and lint clean).

## CS-016 — CLARIFIED — §13 rule 1 covers `deny`
- **Was:** The Registry spec (doc 06 §8 v1.0) exempted `deny` from the "referenced names must exist" check, so a policy could deny undeclared actions. The reference linter never implemented the exemption — an implementation-vs-spec conflict.
- **Now:** Resolved in the linter's (and RFC §13.1's plain-reading) favour: **every** referenced name must exist, `deny` included. Rationale: a deny of an unknown name is a security no-op (default-deny already refuses unknowns) and almost always a typo that silently becomes dead policy. To pre-forbid a capability, declare the action in the registry and deny it — then registry growth surfaces policy reviews instead of silently activating.
- **Impact:** None if your linter already checked `deny` names (the reference one did). Otherwise extend rule 1 to `deny`. Doc 06 §8 and doc 07 §5 rewritten.
- **Test:** extend A4-style coverage: a policy whose `deny` names an undeclared action fails to load.

---

## Applying this Change Set
1. Add/extend the cited acceptance scenarios (**A6–A8**) first; keep A5 green throughout.
2. Implement the three linter rules (11, 12, 13); remove the two `ACP-AMBIGUITY` markers this change set resolves (compiler standing-note, condition-parser `in` note).
3. Re-run the full suite; confirm all `examples/*` still validate (`track-operator`, `support-assistant`, `legal-matter` changed in this set — deliberately).
4. When done, the system conforms to RFC v0.3 (`docs/01`). Note in your commit which CS items it covers.
