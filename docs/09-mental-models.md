# 09 — Mental models: a developer's guide to the ACP safety model

*Non-normative companion.* This doc **teaches the mental model**; it does not define
anything. Every rule lives in the RFC ([`01-RFC-agent-control-policy.md`](01-RFC-agent-control-policy.md))
and the pinned decisions in [`03-architecture-decisions.md`](03-architecture-decisions.md);
this page links to them. If something here ever seems to *state a rule*, trust the
RFC, not this page.

It exists because a handful of concepts — kill, halt, reversibility, compensation,
stakes — **share words or look like one knob when they're actually two**. That's
where everyone trips. Each section below leads with the confusion.

The running example is the Accounts-Payable demo ([`05-demo-spec.md`](05-demo-spec.md),
`demo/`): an AI agent pays invoices; the gateway allows the $800 Acme invoice,
**holds** the $6,000 Globex invoice for a human, and **denies** the $500 Initech
invoice (sanctioned country).

---

## 1. The whole thing in one paragraph

**The agent proposes; the gateway disposes.** The agent never touches a bank, a
database, or an inbox directly — it can only *submit an intent* ("pay this
invoice"). The gateway evaluates that intent against a declarative policy and
returns one of four **verdicts**:

| Verdict | Meaning | Demo |
|---|---|---|
| **allow** | permitted — it runs | Acme $800 |
| **hold** | paused for a human to approve | Globex $6,000 |
| **deny** | refused by policy | Initech $500 (sanctioned) |
| **halt** | stopped by the operator's kill switch | (when an operator hits KILL) |

No language model runs inside that evaluation — it's plain deterministic code.
That's the whole product. Everything else is detail about *how* a verdict is
reached and *what happens after* an `allow`. (RFC §1, §12.)

---

## 2. Three different things people call "stop"

This is the #1 confusion. "Stop" is three things at three layers:

- **deny / halt** — a **verdict** on *one* action. `deny` = the policy refused this
  specific action. `halt` = the operator's kill switch caught it. These are
  *outcomes*, not buttons.
- **kill** — an **operator control** (a button/API). One human action flips a flag;
  from then on matching actions come back `halt` and nothing staged dispatches. It's
  the *cause*; `halt` is the *effect*. (RFC §9.)
- **stopping the gateway** — killing the *enforcer process itself*. **Don't.** That
  removes the guard, not the danger — the demo's "Gateway OFF" toggle shows exactly
  this: payments hit the bank directly, nothing is checked or audited, and the kill
  button is powerless because there's no chokepoint left.

> **One-liner.** Kill = keep the guard, tell it to block everything. Stop the
> gateway = send the guard home. Halt = the stamp the guard puts on a blocked request.

And one more pair people merge:

- **`killable` (a tag in the policy)** is *not* the operator's authority. The
  operator can **always** halt — unconditionally, ignoring `killable` (else a buggy
  agent could mark itself un-killable). `killable: false` means *"a generic
  mid-flight freeze is unsafe for me — stop me via my declared safe-stop instead,"*
  not *"you may not stop me."* (See [`03`](03-architecture-decisions.md) → "Kill is two axes".)

---

## 3. Two axes, not one: reversibility vs stakes

The trap that bit hardest. You will be tempted to use "is it reversible?" to decide
"does a human need to approve it?" **They are different questions.**

- **Reversibility** = *can this be undone?* (`reversible` / `compensable` /
  `irreversible`). It's a fact about the action *type*. It drives **recovery**
  concerns only. (RFC §5.)
- **Stakes** = *does a human need to be in the loop?* It's per-instance and lives in
  the **data** (amount, recipient, sensitivity). You express it with
  `operativeForce`, `resultSensitivity`, and `when:` conditions on `data.*`. (RFC §7.)

> **You might think:** "It's irreversible, so it needs approval."
> **Actually:** a routine internal email and an email leaking secrets to an outsider
> are *equally* irreversible — only one needs a human. Approval keys on **stakes**,
> not reversibility.

Every combination is real — which is the proof they're independent axes:

| | **low stakes** (just run it) | **high stakes** (get a human) |
|---|---|---|
| **reversible** | thermostat setpoint | open a power breaker · grant admin |
| **compensable** | a small refund | a large payment / wire (the demo's `pay`) |
| **irreversible** | a routine email / page | administer a drug · purge a DB · e-file |

Two cautions that follow:

- **reversible ≠ safe.** Undoing the *action* is not undoing its *consequences*.
  Re-closing a breaker doesn't un-blackout the hospital; revoking access doesn't
  un-leak what was copied during the window. So gate on the blast radius, not on
  "well, it's reversible."
- The axes are **separately decided but often coincide** — high-stakes irreversibles
  (a drug dose, a wipe) are common. Don't read "irreversible ⇒ always approve" or
  "reversible ⇒ never approve."

(Design rationale: [`03`](03-architecture-decisions.md) → "Reversibility ≠ stakes".)

---

## 4. Undoing damage: compensation, operators, and the audit log

So an agent did something wrong-but-allowed (a duplicate bookkeeping entry, a
payment to the right vendor but the wrong amount). How do you fix it?

**First, what "compensation" is — and isn't.**

- A `compensation` is a **declared, in-system action the gateway can route to** —
  `refund` for a `pay`, `discontinue` for a `prescribe`. If an action is
  `compensable`, the policy **must** declare its undo action — the linter enforces
  it (RFC §13 rule 10).
- It is **not** "anything that mitigates." A clinical antidote, a restore-from-backup,
  an ops runbook — those are out-of-band, so they do **not** count. If the only
  recovery is out-of-band, the action is simply `irreversible`.
- Compensation is **not time travel.** A refund doesn't un-spend the slippage; a
  reversing journal entry doesn't erase the original (in accounting you *want* both
  rows). It posts a *new* offsetting effect — itself audited, scoped, and gated.

**Who runs the undo? Usually an operator, not the agent.**

If the reason you need to undo is *the agent malfunctioned*, then the agent is the
last thing you should trust to fix it — the recovery would share the fault. So a
fault-remediation undo is performed by an **independent authority** (an operator /
accountant), never the agent. Mechanically this is just authorization: the reverse
action is in the **operator's** allow-set, not the agent's — so it never even
appears in the agent's toolset. (The agent *may* undo things it correctly chose to
do — "cancel my own draft"; it must not be the one to undo what it got *wrong*.)

**The audit log is the find-and-fix tool.** Every attempt — allow / hold / deny /
halt — is recorded, and an executed effect's record carries `resultRefs`: the
downstream id(s) of what it created (the payment id, the ledger entry id). That's
the handle an accountant or an external tool uses to **locate** the bad entry and
reverse it **in the source system**. (RFC §11.)

---

## 5. Where the gateway's job ends

> **You might think:** "The gateway controls everything the action causes."
> **Actually:** it controls the **agent → world** edge, not **world → world**.

A committed payment may trigger a webhook → a journal entry → a report → an alert to
creditors. The agent issued *one* intent; the world produced a *chain*, most of it
inside other systems. The gateway never sees that cascade. So an action's
`reversibility`, `compensation`, `resultRefs`, and even the kill switch describe the
**direct** effect only — never the downstream reactions.

That's deliberate scope, not a missing feature: chasing the cascade would make ACP a
distributed-transaction coordinator (out of scope). The seam back is `resultRefs` +
`correlationId` — the keys a downstream reconciler uses to trace and remediate the
chain. (RFC §9, §11; [`03`](03-architecture-decisions.md) → "Multi-effect & cascade".)

---

## 6. "Which knob do I reach for?"

| You want… | Reach for | Not |
|---|---|---|
| a human to sign off first | a **gate** — `requireApproval` / `dualAuthorization`, keyed on stakes (`operativeForce`, `data.*`) — §7 | reversibility |
| this to *never* happen | `deny`, or just don't `allow` it (default-deny) — §6 | a gate |
| to be able to undo it later | mark it `compensable` + declare its undo action; expose the undo to an operator — §5, §13 | hoping a backup exists |
| to find & fix what the agent did | the **audit log** + `resultRefs`, then reverse in the source system — §11 | the gateway doing it for you |
| an emergency "stop now" | the **kill** switch (operator) — §9 | turning the gateway off |
| to cap amount / frequency | `valueLimit` / `rate` / `quota` gates (counters) — §7 | reversibility |
| to not leak sensitive data | `disclosure` gate + `resultSensitivity` — §5, §7 | scope alone |

---

## 7. FAQ — the misconceptions, head-on

- **"Isn't `halt` just `deny`?"** No. `deny` = the *policy* refused this action
  (normal, rule-based). `halt` = an *operator* pulled the kill (emergency). `hold` =
  waiting for a human. Different reasons, different colors in the trace.
- **"If I mark it irreversible, does it auto-require approval?"** No. Approval is a
  separate gate you write, keyed on stakes. Irreversibility drives *recovery* things
  (the compensation mandate, fail-closed on dependency failure), not approval.
- **"Can a policy turn off the kill switch?"** No. The operator hard-kill is
  unconditional. `killable` is about *how* to stop safely, never *whether* the
  operator may.
- **"Email is compensable — I can send a correction, right?"** No. A correction
  doesn't recall the original; the recipient already read it. Email is `irreversible`.
  `compensable` means a *real declared undo* exists.
- **"`pay` has a `refund`, so it's safe to skip approval?"** No — `reversible ≠ safe`,
  and approval is driven by **stakes** (the amount), not by whether an undo exists.
- **"Does the gateway undo the damage?"** No. It **records** it (with `resultRefs`)
  and makes it findable; the **source system or an operator** does the reversal.
- **"Can the agent run the reversal?"** For a *fault* fix, no — the agent that erred
  can't be trusted to fix it; an independent operator does. (For normal forward
  business — "cancel my own draft" — yes.)

---

## 8. Advanced edges (skip on a first read)

Two timing subtleties, here only so they don't surprise you later. Both were
documented-but-open boundaries through v0.3 and are **specified and built since v0.4**
(CS-017 / CS-018):

- **Decision freshness.** A verdict is computed at *decision time*; a staged effect
  dispatches later. v0.4 bounds that gap two ways: every staged effect carries a
  decision **TTL** (an expired row settles `CANCELLED`/`stale-decision` — a late
  approval cannot resurrect it), and the dispatch claim re-validates the **volatile**
  gates (allow/denylists, windows, preconditions) — a payee sanctioned between
  approval and dispatch settles `stale-guard:denylist` with nothing sent. Counters
  and approval grants are deliberately *not* re-run (that would double-count / re-ask).
  (RFC §12, §4.4.)
- **Scope no-race (TOCTOU).** `scope-on-effect` is decided before staging, and the
  target could change hands before the commit. v0.4 closes it where it can be closed:
  a **transactional** (SQL-class) connector re-asserts the scope predicate *inside the
  effect's own transaction* — the write lands on authorized state or not at all
  (`scope-lost`); a connector that can't carry the predicate (HTTP, email, device)
  re-resolves the target just before dispatch and its declared residual window is
  written into the audit record — the leftover risk is priced, not hidden. (RFC §6.3.)

See [`03`](03-architecture-decisions.md) → "Decision freshness" / "Scope no-race"
(both marked BUILT), and `docs/02` §9.1–9.2 for the wiring.

---

*See also:* [`08-glossary.md`](08-glossary.md) (one-line definitions),
[`01-RFC-…`](01-RFC-agent-control-policy.md) (the normative spec),
[`03-architecture-decisions.md`](03-architecture-decisions.md) (the pinned decisions and their rationale).
