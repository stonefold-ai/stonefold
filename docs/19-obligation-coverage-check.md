# Coverage check: the obligation pattern against the two worked examples

*Supporting document (context, not requirements). Verifies that the pattern in
docs/18 covers the failure cases enumerated for invoicing and clinical.
Written against v0.5 (plain-precondition form) and kept as the record of that
analysis; the v0.6 deltas each row fed into are annotated. Conclusion first:
yes, with two footnotes and one non-claim — all written down so they are
chosen, not discovered.*

---

## 1. Invoicing

| # | Case | Covered by | Enough? |
|---|------|-----------|---------|
| 1 | Reference doesn't exist | Check queries the source, zero matches → fail | ✅ (v0.6: `onNoMatch: deny \| hold`, RFC §7.16) |
| 2 | Wrong-but-real reference | Check compares the record's vendor against the intent's source fields (sender domain, document ref) | ✅ — requires the intent to carry source evidence (footnote 1; v0.6: the `provenance` conjunction) |
| 3 | Already paid | ERP marks the line spent → no open match | ✅ |
| 4 | Wrong moment (dispute, not yet due) | Check reads the flags | ⚠️ only if the flags exist as fields — data hygiene, unchanged |
| 5 | Injected bank change in document | Not the check's job — the connector pays only to the account on the vendor master | ✅ existing rule |
| 6 | Superseded twin (credited) | Linked credit note → no open match; else two candidates → fail with reason | ⚠️ partial — as good as the links in the data (v0.6: two candidates ⇒ `onAmbiguous: hold`) |
| 7 | The order itself is fraudulent | Upstream, where records are created: approvals, separation of duties | ❌ out of scope, stated (RFC §1 non-goals) |

**Double-spend note.** Two intents can both match the same PO line between
decide and dispatch. For a real ERP this is tolerable: posting is
transactional, the second post fails at the connector and settles FAILED,
audited — a failed call, not a double payment. For record systems that do not
enforce spending, the original caveat was "fix the system or add a human
approval"; **v0.6 replaced that caveat with the reservation lifecycle**
(reserve at staging, consume at settle, release on cancel — RFC §12, CS-035),
which closes the window in the gateway when the record system won't.

## 2. Clinical

| # | Case | Covered by | Enough? |
|---|------|-----------|---------|
| 1 | No such prescription | Zero matches → fail | ✅ |
| 2 | Wrong patient | Check requires a fresh wristband-scan result as a typed intent field, compared against the prescription's patient id | ✅ expressible as typed fields (footnote 1) |
| 3 | Double dose | Slot state in the EMR → no open match; EMR charting rejects a second entry | ✅ |
| 4 | Hold / allergy / too soon | Fields on the prescription and chart | ⚠️ only if charted |
| 5 | Injected "dose increased" note | Pattern rules 1–2 verbatim: dose read only from the structured prescription; free text never an input | ✅ this is what the rules are for |
| 6 | Two active orders for the same drug | Two candidates → fail with reason | ⚠️ worked, clumsily; see footnote 2 |
| 7 | The prescription itself is wrong | Upstream: prescriber writes, pharmacist verifies | ❌ out of scope, stated |

## 3. Two footnotes and one non-claim

**Footnote 1 — the intent must carry its evidence.** Case 2 in both tables
depends on the *intent schema*, not the policy: the intent must include the
source evidence (sender domain for an invoice, scan result for a dose) as
typed fields, or the check has nothing to compare the record against. One line
of guidance for whoever designs intent types:

> If a check needs to bind the record to where the intent came from, the
> intent must carry that evidence as typed fields.

(v0.6 made this a first-class gate field: `requireMatch.provenance`, RFC §7.16.)

**Footnote 2 — where `hold` was predicted to fire first, and what actually
happened.** Clinical case 6: two active orders → the check correctly fails —
but operationally that is a nurse at a bedside with a deny, when the right
outcome is "paused, pharmacist paged." When this note was written the
conclusion was: not a reason to add `hold` now; a clinical pilot would surface
the need within weeks, with evidence attached. **What actually happened: v0.6
added `hold` (CS-026) as a strategy decision, ahead of any pilot** — this row
was the argument, and the honest record is that the trigger was fired by
analysis, not by field evidence (docs/18 §7 says the same).

**Non-claim — the pattern is not the safety story.** The clinical example's
scan-every-time and human-administers-the-dose properties come from the
deployment and the domain's own protocol, not from this stack. The pattern
verifies; the domain's existing checkpoints carry the irreversibility. This is
a feature: the stack is enough precisely because it slots into checkpoints the
domain already built — the deterministic clerk inside the protocol, never the
whole protocol.
