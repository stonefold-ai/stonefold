# 14 — EU AI Act mapping: the gateway as oversight & logging machinery

*Non-normative companion. How the gateway's mechanisms map onto the EU AI Act's
high-risk obligations — for EU deployers whose agent touches money, regulated records,
or irreversible effects.*

> **DRAFT — citations unverified.** Every article number, paragraph, and date below is
> marked `[VERIFY]` and MUST be checked against the regulation text (Regulation (EU)
> 2024/1689 `[VERIFY]`) before this page is published or shown to a buyer. This page is
> an engineering mapping, **not legal advice**, and deploying the gateway does **not**
> make an AI system compliant — see the honesty section at the end.

## Why this page exists

The AI Act's high-risk obligations begin applying to Annex III systems around
**2 August 2026** `[VERIFY]`. For an organisation deploying an AI agent in a high-risk
context, two of those obligations are *mechanical* — they demand technical machinery,
not paperwork: the system must **log what it did** and a human must be able to
**oversee and interrupt it**. That machinery is exactly what this gateway is. The
mapping below is line-by-line, mechanism to obligation.

## The mapping

| AI Act obligation | What the regulation asks `[VERIFY each]` | The gateway mechanism | Where specified | The evidence artifact |
|---|---|---|---|---|
| **Art. 12 — Record-keeping** | the system technically allows automatic recording of events (logs) over its lifetime, sufficient for traceability of its functioning | **transactional audit**: every evaluated action — allowed, held, denied, halted — writes one append-only record; for executed effects the audit write shares the database transaction with the effect (no effect without a record, no record without an effect); `resultRefs` link records to downstream reality | RFC §11; CS-006 | the audit log itself: who asked, what was decided, which gate, who approved, what executed |
| **Art. 14 — Human oversight** | the system can be effectively overseen by natural persons; oversight measures include the ability to intervene in its operation **or interrupt it through a "stop" button or similar** `[VERIFY 14(4)]` | **approval holds** (`requireApproval`, `dualAuthorization`): consequential actions pause, staged, until a named human releases them — the agent cannot release its own actions; **kill-switch with the no-race guarantee**: flipping the switch halts every action not yet dispatched, checked inside the same serialized transaction that dispatches (an action cannot slip through the gap) | RFC §7.8–7.9, §9; design §8.4 | the approvals inbox; the halt audit records; the demo's held-$6,000 walkthrough |
| **Art. 14 — oversight capacity** | overseers can correctly interpret output, decide not to use it, override or disregard it `[VERIFY 14(4)]` | **deterministic decisions with recorded reasons**: no model in the enforcement path, so every verdict is reproducible and explainable; `requireExplanation` forces the agent to record its rationale on consequential assessments | RFC §1, §7.14 | per-decision gate results at `audit: full` |
| **Art. 26 — Deployer obligations** | use the system per its instructions; assign competent human oversight; monitor operation; **keep the automatically generated logs** (≥ 6 months `[VERIFY 26(6)]`) | the **policy file is the documented control**: a short, readable, versioned artifact stating exactly what the agent may do, reviewable by the compliance officer who signs Art. 26 responsibility; the audit store is the log retention target | RFC §1 (one-sitting readability goal), §11 | the policy file in version control + the retained audit log |

**Adjacent, same shape `[VERIFY applicability]`:** financial entities carry parallel
obligations under **DORA** (ICT risk management, auditability of automated actions) and
existing SOX/PSD2/EBA outsourcing duties — the same audit record and approval evidence
serve those filings; docs/13 §1 lists them per buyer.

## What this mapping is NOT (read before quoting it)

- **It is not a compliance claim.** The AI Act regulates the whole system — data
  governance (Art. 10), transparency (Art. 13), accuracy/robustness (Art. 15), risk
  management (Art. 9), conformity assessment — `[VERIFY]` almost all of which concerns
  the *model and its development*, which the gateway deliberately does not touch. The
  gateway covers the **acting surface**: what the agent can do, who allowed it, how a
  human stops it, and what the record proves.
- **It does not classify your system.** Whether a given agent deployment is "high-risk"
  under Annex III is a question for your counsel, not for this repo.
- **The honest limits still apply.** The gateway bounds and proves what the agent can
  *do*; it does not make the agent's permitted choices *good* (README caveat), and its
  guarantee ends at the declared/actual trust boundary (docs/13).

## The one-paragraph version for the meeting

> Two of the AI Act's high-risk obligations are mechanical: log everything the system
> does (Art. 12) and keep a human able to intervene and interrupt (Art. 14) `[VERIFY]`.
> The gateway is that machinery, built as infrastructure: every action the agent
> attempts is decided deterministically against a written policy, consequential actions
> pause for a named human, an operator stop-switch halts everything not yet dispatched
> with a no-race guarantee, and every decision — including refusals — lands in a
> transactional audit record. The policy file itself is the documented control your
> compliance office signs. What the gateway does not do: make the model accurate,
> transparent, or well-trained — it makes the agent's *actions* governed and provable.

*See also:* [`13-who-is-this-for.md`](13-who-is-this-for.md) (buyers and per-industry
obligations), [`12-conformance-tck.md`](12-conformance-tck.md) (certifying an
implementation's guarantees), [`10-positioning-policy-engines.md`](10-positioning-policy-engines.md)
(where OPA/Cedar/IAM plug in).
