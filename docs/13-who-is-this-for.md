# 13 — Who is this for: industries, buyers, adoption paths

*Which industries benefit most from an ACP gateway, what each one's risk actually is, and which parts of the spec answer it.*

**Supporting doc — context, not normative.** `docs/04` answers "which technical substrates does the recipe apply to" (SQL, email, devices, …); this document answers "who should deploy it, and why them first". The ranking principle is simple: ACP's guarantees are priced for settings where an agent's action can **move money, touch a regulated record, or cause an irreversible physical or legal effect** — and where someone must later *prove* what the agent did and who allowed it. The further a deployment sits from those three, the less of the gateway it needs.

Each section names the agent work being deployed, the risk that blocks deployment today, the ACP mechanisms that answer it, the worked example already in the spec, and who signs the purchase.

---

## 1. Financial services & payments — the strongest fit

**The agent work:** accounts-payable and accounts-receivable automation, payment operations, treasury actions, claims payouts, fintech back-office (KYC follow-ups, refunds, chargebacks).

**The blocking risk:** an agent that can *pay* can be defrauded — a fraudulent invoice, a payee swapped after approval, a sanctions hit landing between decision and dispatch. One wrong payment is direct, quantifiable loss; a pattern of them is a regulatory event.

**What answers it:** tiered `valueLimit`/`spendLimit`, `denylist` over sanctions sets, `requireApproval`/`dualAuthorization` above thresholds, rate caps per payee, staged effects with a kill-switch — and the v0.4 timing guarantees exist *because of this buyer*: decision freshness (a payee sanctioned after approval is caught at dispatch, RFC §12) and scope no-race (a payment can't land on an account that changed tenants, RFC §6.3). The audit record maps onto obligations these firms already carry (SOX, DORA, PSD2, EBA outsourcing guidelines) — the gateway produces the evidence they must file anyway.

**In the spec:** worked example §14.4 (payments-ops); the runnable real-LLM demo (`demo/`, docs/05) is exactly this — a forwarder agent, a fraudulent-invoice attack, a held $6,000, a denied sanctioned payment.

**Who buys:** the CFO's controls owner and the CISO jointly; risk & compliance signs off. A single prevented incident typically clears the cost bar. Insurance (claims payout automation) is the same shape with a different rulebook.

## 2. Healthcare

**The agent work:** ward assistants (chart lookups, vitals recording, triage suggestions), medication-administration support, prior-authorization and coding agents, patient-communication agents.

**The blocking risk:** irreversible physical effects (a dose administered) and regulated data (HIPAA/GDPR-special-category). The failure mode isn't only harm — it's an *unattributable* action in a clinical record.

**What answers it:** `quantityCap` per patient, `precondition` checks (five-rights, not-discontinued), forbidden-by-default prescribing, scope-to-ward with break-glass `disclosure` for sealed records, `requireApproval` keyed on operative force, `requireExplanation` on consequential assessments. Approval holds map one-to-one onto existing clinical sign-off culture — the gateway doesn't ask clinicians to work differently, it makes the existing sign-off machine-enforced.

**In the spec:** worked example §14.2 (ward nurse assistant); the hospital walkthrough in the README.

**Who buys:** the CMIO/CIO with clinical safety and the privacy officer. Slower cycle than fintech; higher stickiness once deployed.

## 3. Customer-facing enterprise operations (CRM, support, e-commerce)

**The agent work:** support agents that read customer records and send email, issue refunds and credits, change accounts, run marketing sends. This is where agents are being deployed *fastest* today, usually with the least control.

**The blocking risk:** prompt injection through customer-supplied content (the README's attack example), data exfiltration across customer boundaries, and money leaking one small refund at a time.

**What answers it:** scope injection below the model (only *this* customer's rows exist, RFC §6.3), recipient `allowlist`s, `contentCheck` on outbound text, `rate`/`quota`/`spendLimit` on refund-class effects, export forbidden by default. Lower stakes per action than the two above — but the largest deployment volume, and the controls are the cheap, deterministic ones.

**In the spec:** worked examples §14.1 (support assistant) and §14.5 (legal matter assistant — same shape with privilege boundaries); the scripted adversarial demo (`make demo`) runs the attack battery against exactly this policy.

**Who buys:** VP of support/operations for the value; security review is the gate. Shortest sales cycle of the list.

## 4. Cloud, DevOps & managed service providers

**The agent work:** agents holding infrastructure credentials — deploy, scale, restart, delete, rotate; incident-response runbooks; cost-optimization actions.

**The blocking risk:** blast radius. A fast agent with cluster-admin can destroy in seconds what took years to build; "delete" is usually irreversible in practice even when an undo nominally exists.

**What answers it:** environment `allowlist`s (prod vs staging), `window` gates (change freezes), `requireApproval` on destructive verbs, declared compensations, the kill-switch, and the startup **interception-coverage check** (no tool reachable except through the gateway) — which is precisely the property platform teams cannot get from per-tool wrappers.

**In the spec:** docs/04 §5 (cloud/DevOps vignette); §14.6 (industrial vehicle controller) shows the same bounded-continuous-effect pattern.

**Who buys:** platform engineering directly — fastest technical adoption, usually smaller contracts, and a natural channel: an MSP deploys one gateway pattern across many clients.

## 5. Legal

**The agent work:** matter-management assistants, document drafting and filing, deadline docketing, client communication.

**The blocking risk:** privilege boundaries (matter A's material surfacing in matter B), irreversible court filings, and actions taken "on behalf of" without evidence of authority.

**What answers it:** scope per client/matter (`forMatterOfClient`), `transition` from-states on filing workflows, `requireApproval` before anything leaves the firm, and the audit record as the evidence-of-authority the profession already reasons in.

**In the spec:** worked example §14.5; docs/04 §3 (files/documents).

**Who buys:** managing partner / general counsel with the firm's IT. A smaller market than the ones above, but one that pays for evidence.

## 6. Defence & critical infrastructure — the ceiling, not the beachhead

**The agent work:** decision-support for track/threat operators, sensor tasking, logistics; in civilian critical infrastructure, grid/water/plant operations assistants.

**The blocking risk:** the highest of all — emissions that reveal position, kinetic effects, safety-critical actuation. Here "no LLM in the enforcement path" (invariant 1) is a hard requirement, not a preference, and human authority over force is a legal obligation.

**What answers it:** `emissionControl`, `standing` rules for declared ROE states, `dualAuthorization` with positive-identification preconditions, `requireExplanation` on hostile-classification assessments, clearance-compartment scoping. The point of the design in this domain is keeping humans in command — the AI supplies information; it cannot satisfy the authorization chain by itself.

**In the spec:** worked example §14.3 (track operator); the README defence walkthrough.

**Who buys:** primes and government programs — certification and procurement cycles put this **last on the timeline** even though it fits the design most exactly. Treat it as the proof of the model's ceiling, not the first sale.

---

## The other customer: platforms and vendors who embed a gateway

Orthogonal to every industry above sits a second class of customer: **agent-platform vendors and vertical-SaaS companies** that need a governance layer inside their own product rather than a deployment of ours. For them the deliverables that matter are the **spec** (docs/00, docs/01 — a policy language their customers' auditors can read), the **registry generator** (docs/06 §9 — a registry drafted from the schemas they already have), and above all the **conformance TCK** (docs/12): they implement the gateway in their own stack and language, run the kit, and publish which profiles they certify. That is why the TCK's driver is deliberately tiny and why the wire binding exists — the spec is designed to be *adopted*, not just *installed*.

## Who actually signs — a cross-cutting note

In every regulated vertical the economic buyer is usually **not the AI team**. The AI team wants to ship the agent; the **CISO / compliance office / controls owner** is the one blocked on "can you control it and prove what it did". Lead with the audit record and the kill-switch (the evidence and the emergency brake), not with the policy grammar. The Gartner/MIT numbers in the README are the macro version of the same fact: agentic projects stall on *inadequate controls*, not on model capability.

## What the gateway does not judge — and where other systems plug in

Everything above is what the gateway *does*. Just as important for an evaluator is what it deliberately does **not** do. The enforcement core is deterministic by invariant — no model, no heuristics, no judgment calls inside `enforce()`. That is what makes its decisions predictable and auditable, and it draws a hard line: **the gateway never judges meaning itself.** It cannot read an email body and know it contains PII, look at an invoice and sense fraud, or decide whether a clinical judgment is sound.

The design answer is not to pretend otherwise but to give each of those judgments a **declared hook** — a registered function the gateway calls at its chokepoint, whose verdict enters the pipeline as an ordinary deterministic gate result, and whose *failure* triggers the fail-closed discipline like any other dependency (RFC §10):

| The gateway cannot itself… | The hook where a specialist system plugs in | Who plugs in |
|---|---|---|
| judge **content** — PII in an outbound email, fraudulent wording, toxic or off-policy text | the `contentCheck` gate is a registered content hook (RFC §7.7); `disclosure` adds pre/post result-flow checks (§7.12) | your DLP, moderation service, or classifier |
| verify **world truth** — is the drug discontinued? has the payee's cooling-off elapsed? | `precondition` / `emissionControl` named checks (§7.6, §7.13) — re-validated at dispatch since v0.4 | the system of record |
| hold the **organization's authorization model** — roles, entitlement graphs, org hierarchies | registered scope predicates (§6.3) and the authorization seam (docs/10 — OPA/Cedar/IAM compose behind it) | your IAM / policy engine |
| make the agent's choice **good** — a permitted-but-wrong action stays possible | `requireApproval` / `dualAuthorization` / `requireExplanation`; the audit record as reviewable evidence | humans; downstream review and SIEM |

Two things keep this from being a hand-wave. First, the hooks are held to a contract: registered functions are **policy-grade code** — reviewed and versioned like the policy itself, and the reference ships a conformance harness (determinism, totality, fail-closed; docs/06 §6) to hold them to it. Second, the placement matters: the external check runs *at the gateway's chokepoint, under the gateway's failure mode, onto the gateway's audit record* — so "the gateway can't check content" resolves to "your content checker runs where it can't be bypassed and its verdict is on the record".

What remains genuinely open even with every hook plugged: an action whose content passes every check can still be a bad idea (that is what the approval gates and the audit trail are for), and the gateway governs the agent's *direct* effects, never the downstream cascade a committed effect triggers (RFC §11 scope boundary).

## Where ACP is the wrong tool (honesty section)

- **Read-only, low-stakes agents** (internal search, summarization, analytics over non-sensitive data): default-deny scoping is cheap insurance but the full gateway is overkill; a thin allowlist proxy may do.
- **Creative/content workflows** with a human already reviewing every output: the human *is* the gateway.
- **Agent orchestration, planning, memory, multi-step workflow engines:** explicitly out of scope (RFC non-goals) — ACP governs the actions, not the agent's reasoning loop.

## Recommended beachhead

If forced to pick one entry point: **mid-market fintech and AP-automation vendors** — the pain is monetary and immediate, the sales cycle is the shortest of the regulated verticals, the shipped demo (docs/05) already speaks their language, and the v0.4 guarantees were built from their opening questions. **Healthcare second** — slower to enter, hardest to displace once in.

*See also:* [`04-domains-and-use-cases.md`](04-domains-and-use-cases.md) (the same recipe across technical substrates), [`10-positioning-policy-engines.md`](10-positioning-policy-engines.md) (why a policy engine alone doesn't cover this), [`12-conformance-tck.md`](12-conformance-tck.md) (how an embedder certifies their own implementation).
