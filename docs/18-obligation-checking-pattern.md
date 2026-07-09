# Checking intents against records — the obligation pattern

*Supporting document (context, not requirements). The pattern described here is
normative in Stele v0.6: the adoption path and the `requireMatch` gate are
specified in RFC §7.16, the reservation lifecycle in §12 (CS-032–CS-036). This
page is the plain-language account of the problem and the pattern; the RFC is
the contract.*

---

## 1. The problem

Gates compare an intent against constants in the policy file: limits, lists,
rates. That bounds damage. It cannot tell you the action is *right*.

Example: the agent submits `pay` for $800 — known payee, under the limit,
inside the rate. Every gate passes. But nothing was ever ordered from this
vendor, or the invoice is already paid. In bounds, and wrong.

What catches this is not a constant. It is a check against a **record** that
already exists in another system: an open purchase order, an active
prescription, an open support case.

## 2. What makes a record usable

A record can back a validity check only if:

1. **It came first.** Created before the intent — so the agent can't have
   written its own justification.
2. **Someone else made it.** A different person or process, through a
   different channel. Checking against it means checking against a decision
   already made.
3. **It gets spent.** One order line pays one invoice. One dose slot is given
   once. Without this, one record validates unlimited actions.

## 3. Where to start: one precondition

Use a named precondition. Nothing new:

```yaml
gates:
  pay:
    precondition: [matchesOpenPurchaseOrder]
```

The check is code you write and register with the gateway. It queries the ERP
for open orders matching the intent's vendor and amount and passes on exactly
one open match, fails otherwise. Preconditions are already re-checked at
dispatch, so the pipeline needs no change. This is the documented, first-class
adoption path (RFC §7.16, "when a plain precondition is enough") — a
deployment that starts here and never upgrades is using the feature correctly.

## 4. Rules for the check

1. **Read from the source, never from the intent.** Every field you compare
   against comes from the record system, fetched by the check. The intent may
   carry an id to narrow the query — never the data itself. An agent that
   supplies the data it is checked against checks itself.
2. **Compare typed fields only.** Ids, amounts, dates, enums. Never free text.
3. **Fail closed, with a reason code.** No match, several matches, system down
   — all fail, each with its own code in the audit record. Never pick among
   several matches. (A check declared hold-capable may instead resolve `hold`
   for the judgment-shaped cases — RFC §7.6.)
4. **Be deterministic.** Same intent, same records, same answer. No model
   output inside the check.
5. **Write the tolerance down.** "Amount within 10% of the order line" is fine
   — as a visible constant, not an accident.

## 5. Spending the record

The check proves the record exists. Something must also mark it spent — or one
order line validates five invoices.

Prefer the record system's own spending: posting an invoice against an order
consumes the order line in the ERP; charting a dose fills the slot in the EMR.
The check proves an open record exists; the record system marks it spent, in
its own transaction; the gateway keeps doing what it does.

**Where the record system does NOT enforce spending** (a spreadsheet, a
homegrown list, a slow-posting upstream), v0.6's `requireMatch` closes the
window instead: the gateway reserves the matched record with the staging
commit, checks the reservation is still live at dispatch, consumes it with the
settle, and releases it on any terminal non-success (RFC §12, CS-035). Don't
hand-build that inside a check — stateful hooks bring back all the complexity
the pattern avoids; the lifecycle is exactly what the gate exists for.

## 6. One deployment rule

**The agent must not have write access to any system its checks read.** An
agent that can create orders and then pay against them approves itself. The
linter errors where the overlap is statically visible and points at the
deployment check otherwise (RFC §13 rule 15) — but the rule is ultimately a
deployment checklist item. Say it once, loudly.

The flip side: whether the *record itself* is legitimate (a fake order, a
wrong prescription) is guarded where records get created — approvals,
separation of duties. That was true before agents and stays true.

## 7. When to upgrade from the check to the gate

An earlier version of this page listed `hold` and a declarative match gate as
"what would need an RFC (not now)", with a deliberately high bar: field
evidence first. **v0.6 added both without that field evidence — a strategy
decision, not a trigger firing** — because the change set was the moment the
feedback channel, hold composition, and consumption semantics could be
specified coherently, and because the coverage analysis (docs/19) showed
exactly where fail-with-reason is clumsiest. Honest accounting: the design was
argued from analysis, and the pilot evidence the original bar asked for is
still worth collecting.

Reach for `requireMatch` when you need what a named check cannot give: the
match rule **in the policy file** (reviewers, the linter, and the TCK see
"payment requires an open order", not a function name); `ambiguous ⇒ hold`
routed to a named resolver instead of a bare deny; **gateway-managed
reservation and consumption** where the record system doesn't enforce
spending; and the standardized `obligationRefs`/`consumption` audit lineage.
The normative text is RFC §7.16.

---

*Short version: gates check the intent against the policy. This pattern
compares it against a record the agent didn't create and can't write. In
bounds is not the same as owed.*
