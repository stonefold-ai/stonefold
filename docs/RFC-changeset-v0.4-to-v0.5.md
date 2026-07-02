# RFC Change Set — v0.4 → v0.5 (DRAFT)

**Status: draft — accumulating.** Items here are additions agreed for the next spec
revision; the RFC header remains v0.4 until this set is closed. On any conflict with
older wording, a Change Set wins (same rule as prior sets).

**Scope of the change.** Both items are **additive declarations** — no policy-file
syntax changes, no new kinds/gates/operators (the frozen shape holds). `apiVersion`
strings and all existing `examples/*` remain valid.

---

## CS-014 — Trust boundary stated in the spec (ADDED, §1)

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

## CS-015 — Connector digest pinning (ADDED, registry §5; §10 hook)

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
CS-014's boundary statement) was invisible to the gateway; with a pinned digest it
becomes a fail-closed refusal with an audit record, and changing connector code requires
a registry change — a reviewed, versioned artifact.

**Implementation impact:** gateway verifies digests at load + dispatch when declared
(reference implementation: pending). TCK: a freshness-style profile check is future
work for when the reference implements it — a certification claim MUST NOT imply digest
verification until then.

## CS-016 — Identity-provider seam (ADDED, architecture decision 11)

**What:** the authenticated `actor:`/`agent:` identities the session carries come from
an `IdentityProvider` protocol ahead of the pipeline. Default and built-in: the existing
session/transport authentication (no behavioural change; the gateway remains fully
standalone). The seam permits credential-based verifiers (agent passports, W3C VCs,
SPIFFE) to stand in the same slot. No specific scheme is integrated or endorsed; no
DID/VC machinery becomes a dependency.

**Why:** identity provenance is deliberately outside this spec's scope (the trust
boundary, CS-014) — but *where* identity enters must be a declared seam, like the
authorization step (decision 9), or every deployment invents its own splice point.
Names the seam; changes nothing about how identity is used downstream. Invariant 3
(identity never from the agent payload) is restated as binding on every provider.

**Implementation impact:** protocol definition + the trivial built-in + fakes in tests
(reference implementation: pending).
