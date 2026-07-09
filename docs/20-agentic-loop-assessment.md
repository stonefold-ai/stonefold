# Assessment: does the design carry the agentic loop to external systems?

*Supporting document (context, not requirements). Self-assessment, written to
be attacked. Method: name the properties that make the local loop (a coding
agent against compiler + tests) work, score the design against each, honestly.
Written against v0.5 + the obligation pattern; the three gaps it found became
v0.6 change-set items (CS-029/030/031) and are annotated with what closed
them. Kept as the record of why those items exist.*

---

## The reference loop

A coding agent converges because: the verifier is independent of the agent,
the verdict is deterministic, the error is specific enough to act on, failed
attempts cost nothing, feedback is fast, the verifier's blind spots are known,
errors are fixable by the agent, "done" is unambiguous, success lands exactly
once, and what the verifier can't judge goes to a human (PR review).

Ten properties. Scorecard:

| # | Property | Verdict (at assessment) | Notes |
|---|----------|------------------------|-------|
| 1 | Verifier independent of agent | **Holds** | Reads from source; agent has no write access. Rests on the one deployment rule — unenforceable by tooling where the overlap isn't statically visible (v0.6 lints the visible case, §13 rule 15). The single point the whole loop trusts. |
| 2 | Deterministic verdict | **Holds, qualified** | Deterministic given registry state; state moves between attempts (a PO can close mid-loop). Each verdict is still exact and evidence is recorded — "tests on a changing repo", not flakiness. |
| 3 | Specific, actionable error | **GAP (G1, G2)** — closed by CS-029/CS-030 | See below. |
| 4 | Failed attempts are free | **GAP (G3)** — dedupe closed by CS-031; budget deferred to v0.6.1 | Denies are cheap (pre-commit, no side effect). Holds spend human attention. |
| 5 | Fast feedback | **Holds** | Gateway decision is fast. Holds are slow by design — that is the point of a hold. |
| 6 | Blind spots known | **Holds** | The residue is named in writing: content quality, upstream record legitimacy, uncharted state. |
| 7 | Errors fixable by agent | **GAP (G2)** — closed by CS-029 | `no-open-match` may mean "wrong reference — fix and retry" or "no order exists — nothing to fix." |
| 8 | "Done" is unambiguous | **Holds** | allow → settle → receipt in `resultRefs` (v0.6 adds `obligationRefs`/`consumption` — both ends of the relation). |
| 9 | Success lands exactly once | **Holds** | Idempotency per intent id; v0.6 extends it to the obligation side (reserve/consume/release idempotent per ref+intent, CS-035). |
| 10 | Unverifiable residue → human | **Holds** | `hold` is exactly this: the human is the test suite for what has no deterministic verdict. |

## The three gaps (as found — and what closed them)

**G1 — the agent's feedback channel was maximally leaky.**
The reference implementation returned the **full gate trace with prose
reasons** to the agent — the channel was not missing, it was an accident at
the leaky extreme: maximally convergent, and a deny oracle handing over
record-side values and mapping the policy one probe at a time. (An earlier
draft of this note said the channel was "unspecified"; the precise defect was
that the spec was silent and the reference default leaked.) What the agent
sees on deny must be a policy decision, not an accident.
**Closed by CS-030:** a per-gate/per-check `feedback:` key — `code` |
`code+fields` (the new default: which intent fields failed, never record-side
values) | `code+evidence` — applied on the return path only; the audit record
keeps everything.

**G2 — reason codes need a retry class.**
A compiler error is always in-principle fixable by editing. A deny is not:
"amount outside tolerance" is fixable (re-extract the amount); "no order
exists" is not (the fix is a human conversation, or nothing). Without the
distinction, agents loop on unfixable denies until the rate gate throttles
them — a crude, uninformative stop.
**Closed by CS-029:** every code declares `retryable` | `terminal` |
`escalate`, returned with the code; undeclared defaults terminal (stop
retrying); built-in gate reasons carry normative classes (RFC §11).

**G3 — holds spend a budget the design didn't track.**
Denies are free; holds cost human attention — the scarcest resource in the
system, and the one whose depletion (rubber-stamping) silently disables the
safety property. Ten variants of the same unmatched invoice are one question
wearing ten disguises.
**Closed (half) by CS-031:** duplicate holds — same (agent, action, reason
code, candidate refs) within the deployment's dedupe window — collapse into
one queue item with an attempt count, each attempt still audited. The
per-principal ceiling on open holds (`hold-budget-exhausted`) is **deferred to
v0.6.1**, waiting on deployment evidence for where the ceiling should sit.

## One security note carried over

Reason codes are an oracle regardless of the visibility setting: even bare
codes enumerate the policy's walls one probe at a time. Do not blunt the codes
— that kills the loop. Make probing visible instead: deny-rate and
reason-code distribution per agent principal belong on the audit surface (the
reference exposes them at `GET /admin/reason-codes`). A converging loop and a
mapping loop look different in that data; the difference is the detection.

## Verdict

The design is the external-systems version of the proven loop: independent
deterministic verifier, pre-commit iteration so failures precede side effects,
exactly-once settlement, and a human tail for the unverifiable residue — the
last property being the one the local loop also quietly relies on (PR review).
The verifiers themselves are old (three-way match, maker-checker, BCMA —
decades of catching human fallibility); what is new is only the composition.

The three gaps shared one root: the reference loop's feedback channel
(compiler output) was designed for the iterating party, while this design's
feedback channel (audit record) was designed for the reviewing party — and the
agent's channel was left to an accident. G1–G3 were that one omission seen
from three sides: what the agent sees, whether it should retry, and what its
retries cost. All three landed in v0.6 as additive deltas: one visibility key,
one three-value retry class, one dedupe rule. None touched the frozen shape.
