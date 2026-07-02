# RFC Change Set — v0.4 → v0.5 (DRAFT)

**Status: draft — accumulating.** Items here are additions agreed for the next spec
revision; the RFC header remains v0.4 until this set is closed. On any conflict with
older wording, a Change Set wins (same rule as prior sets).

**Scope of the change.** The items in this set are **additive declarations, semantic
completions, or text fixes** — no policy-file syntax changes, no new kinds/gates/operators
(the frozen shape holds). `apiVersion` strings and all existing `examples/*` remain valid.

---

## CS-019 — Trust boundary stated in the spec (ADDED, §1)

**What:** RFC §1 now states the guarantee boundary explicitly: the gateway proves that
*intents conform to policy*; it does not prove that the code executing them does what
it declares. Connectors, registered hooks, and the gateway itself are the trusted
computing base; their integrity is a supply-chain property, not a property the policy
language establishes. Non-normative discussion (attack class, TCB-size argument,
detection-vs-prevention, what is deliberately out of scope) added to docs/13.

**Why:** every serious security review opens with "where does your guarantee end?" —
a reference-monitor design that does not draw the line itself gets it drawn for it,
uncharitably. No behavioural change; documentation of an existing boundary.

**Implementation impact:** none (text only).

## CS-020 — Connector digest pinning (ADDED, registry §5; §10 hook)

**What:** a connector declaration MAY carry `digest: "sha256:<64 hex>"` pinning the
implementing artifact by content digest. When declared, the gateway MUST verify the
loaded implementation against the digest **at policy load and at dispatch**; a mismatch
is a **dependency failure** under `failureMode` (§10) — fail closed by default, audited.
Production deployments handling irreversible effects SHOULD pin their effect connectors.
Digest computation / artifact signing is deployment tooling, out of registry semantics.
Schema: optional `digest` property on connector objects (`registry.schema.json`);
additive, existing registries unaffected.

**Why:** the registry declared *what* a connector does but not *which code* is trusted
to do it. Silent replacement of a connector's implementation (the supply-chain attack in
CS-019's boundary statement) was invisible to the gateway; with a pinned digest it
becomes a fail-closed refusal with an audit record, and changing connector code requires
a registry change — a reviewed, versioned artifact.

**Implementation impact:** gateway verifies digests at load + dispatch when declared
(reference implementation: done — `acp_core.digest`, verified at policy load and at
dispatch; the reference pins each connector's module source bytes, docs/06 §5). TCK: a
digest profile check is future work — a certification claim MUST NOT imply digest
verification until it exists.

## CS-021 — Identity-provider seam (ADDED, architecture decision 11)

**What:** the authenticated `actor:`/`agent:` identities the session carries come from
an `IdentityProvider` protocol ahead of the pipeline. Default and built-in: the existing
session/transport authentication (no behavioural change; the gateway remains fully
standalone). The seam permits credential-based verifiers (agent passports, W3C VCs,
SPIFFE) to stand in the same slot. No specific scheme is integrated or endorsed; no
DID/VC machinery becomes a dependency.

**Why:** identity provenance is deliberately outside this spec's scope (the trust
boundary, CS-019) — but *where* identity enters must be a declared seam, like the
authorization step (decision 9), or every deployment invents its own splice point.
Names the seam; changes nothing about how identity is used downstream. Invariant 3
(identity never from the agent payload) is restated as binding on every provider.

**Implementation impact:** protocol definition + the trivial built-in + fakes in tests
(reference implementation: done — `acp_gateway.identity`, wired at the `submit_intent`
route ahead of the pipeline; the built-in reproduces the prior transport-header
behaviour, so the default is unchanged).

## CS-022 — Kill wording reconciled with the two axes (FIXED, §9)

**What:** RFC §9's opening read as if `killable` gates the operator ("`killable: true` …
lets an operator issue a `halt`"). It is rewritten per the pinned decision (docs/03,
key decision 10): the **operator hard-kill is unconditional** — a policy cannot opt out,
and `killable` never gates it; **`killable`** is a separate, action-level declaration of
the *manner of stopping under normal/automated operation* (generic live-freeze vs a
declared safe-stop/compensation) that guards automated halts and informs, but never
blocks, the operator. The §9 UNDER-REVIEW note is retired; its content is now the
section opening.

**Why:** an operator-safety section must not contradict itself. The two-axes model was
already decided and documented in docs/03; the RFC text lagged it.

**Implementation impact:** none (text only). The graceful-halt wiring stays deferred:
`killable` is parsed but not consulted by `enforce()` — everything is killable today,
which is safe by default. Open design questions remain listed in docs/03.

## CS-023 — Batch decision semantics (ADDED, §12; SIF §5)

**What:** what happens when one operation of a SIF batch is refused was unspecified
(SIF §5 promises transactional record/transition batches; ACP §11 decomposes a batch
into independently authorized actions — neither said what a mid-batch DENY or HOLD does).
Now specified in §12: the gateway decides **every** operation first (steps 1–5, each with
its own audit record); any **DENY or HALT refuses the whole batch** before anything
commits or stages (a batch is a request for atomicity; independent outcomes ⇒ independent
intents), with the structured error identifying the failing operation. A **HOLD does not
refuse the batch**: the held effect stages `PENDING_APPROVAL` and any `record` ops commit
atomically with the staging (the behaviour §4.4 already mandated); a later rejection or
TTL expiry does not roll the committed ops back — they were independently authorized and
remain reconstructable via `correlationId`.

**Why:** the batch/decision interaction is the first question a TCK profile for batches
would have to answer, and both RFCs pointed at each other without answering it.

**Implementation impact:** reference implementation pending — the reference gateway
accepts single-operation intents today; these semantics are specified ahead of the
multi-op implementation (tests/scenarios to be added with it).

## CS-024 — Classification ordering for `disclosure` (CLARIFIED, §7.12; registry §4)

**What:** `disclosure.maxClassification` compares classifications, which requires an
order the spec never declared. Now: the built-in `resultSensitivity` values are ordered
`public < internal < confidential < restricted`; a domain substituting its own labels
(§5) MUST declare them as an **ordered** value set in the registry (docs/06 §4 — order
is list position, lowest first); a classification value missing from the declared order
makes the gate **fail closed** (the §8 runtime-resolution rule).

**Why:** without a declared order the gate is unimplementable for domain labels, and two
gateways could disagree — fatal for a gate whose selling point is certifiability.

**Implementation impact:** registry loaders treat the classification value set as
ordered; no schema change (`valueSets` already carries ordered lists).

## CS-025 — Editorial & clarification batch (DOCS, §6.2, §6.3, §7, §13)

One batch of text fixes, no semantic inventions:

- **§6.2 rule 4 reworded.** The old wording ("the most specific allow governs which
  gates apply") contradicted §7's rule that *all* matching gates AND-combine — and the
  worked examples rely on §7. Now: allow-matches admit, gates bind by their own §7 keys,
  all matching gates apply.
- **CS-018 capability home named** (§6.3): the scope-reassertion capability is connector
  metadata **declared in gateway code** alongside the connector implementation (like the
  scope-predicate bindings) — not a registry-YAML field. docs/06 §5 now says so too.
- **`spendLimit` unit defined** (§7.4): the unit and cost assignment are gateway
  configuration; the policy number is not portable across deployments.
- **Approver `role:` namespace** (§7.8, §13 rule 1): resolves at the identity seam
  (decision 11), not the registry; explicitly exempt from lint rule 1.
- **`window` absolute form documented** (§7.10): `from`/`to` — already in
  `acp.schema.json` and the catalog row ("date range"), previously unexemplified.
- **Gate table row 13** corrected to pass/fail/hold (matches §7.13 / CS-011).
- **Catalog approval examples re-keyed on stakes** (`operativeForce == high`) — §7's own
  examples used the reversibility-keyed anti-pattern the §5 note warns against.
- **CS-020/CS-021 changelog status** corrected from "pending" to shipped
  (`acp_core.digest`, `acp_gateway.identity`).
