# 10 — "We already have OPA / Cedar / IAM — why this?"

*Non-normative companion.* The first question every security team asks. The short answer: **those are decision engines; ACP is a decision engine *plus* the enforcement machinery an agent actually needs** — and the hard parts of governing an agent are the machinery, not the decision. The longer answer, with the comparison and the honest trade-offs, is this page.

If you remember one thing: **you can plug OPA or Cedar into the ACP gateway as its authorization step** (that seam is a pinned decision — [`03`](03-architecture-decisions.md) key decision 9). They are not competitors; they answer a *subset* of the question ACP answers.

---

## 1. The category error

OPA, Cedar, and XACML-style engines are **policy decision points (PDPs)**: given (principal, action, resource, context), return *allow* or *deny* — statelessly, and assuming someone else built the **enforcement point (PEP)** that intercepts the action, gathers the facts, and honours the verdict.

For human-facing APIs that split works: the API server is the PEP, the request is already typed, and the verdict space really is allow/deny.

An AI agent breaks all three assumptions:

1. **There is no trustworthy PEP by default.** The agent holds the tools; nothing intercepts them. ACP's first job is *building* the chokepoint — the gateway that is the only path to any effect ([`02`](02-implementation-design.md) §0) — and the typed intent surface (SIF, enum-injected from the registry) that makes calls decidable at all.
2. **Two verdicts aren't enough.** Agent governance needs **four**: `allow` / `hold` / `deny` / `halt` (RFC [`01`](01-RFC-agent-control-policy.md) §2). `hold` — pause this action until a human approves — is not expressible in a stateless PDP: it requires the action to be **staged** somewhere a human event can release ([`02`](02-implementation-design.md) §7). `halt` requires kill machinery with a no-race guarantee (RFC §9). These two verdicts are most of the product.
3. **The decision is the cheap part.** What makes an agent deployable is what surrounds it: staged effects with idempotent dispatch, stateful gates (rate/quota/spend counters), scope injected *below* the model, transactional audit with `resultRefs`, fail-closed dependency handling. None of that lives in a PDP.

---

## 2. Side by side

| Dimension | OPA / Cedar (generic PDP) | IAM (cloud/enterprise) | ACP gateway |
|---|---|---|---|
| **Verdicts** | allow / deny | allow / deny | allow / **hold** / deny / **halt** |
| **Enforcement point** | bring your own PEP | the API endpoint | the gateway **is** the PEP — sole path to effects, interception coverage checked |
| **Intent surface** | whatever the PEP passes | API call + IAM context | **SIF**: typed, registry-validated, enum-injected — invalid names unrepresentable |
| **Vocabulary** | generic principal/action/resource | services, roles, ARNs | registry-typed **kinds** + **governance attributes** (reversibility, emission, operativeForce, …) |
| **Granularity** | whatever facts the PEP gathers | per-API, per-role | per-action, **per-parameter, per-instance** (`data.amount > 10000`, `resource.patientId`) |
| **Stateful limits** | no (external data) | quotas at best | `rate` / `quota` / `spendLimit` / `quantityCap` as first-class gates |
| **Human-in-the-loop** | out of scope (external workflow) | out of scope | first-class: `requireApproval` / `dualAuthorization` = a staged row a human releases |
| **Effects handling** | out of scope | out of scope | staged outbox, idempotency keys, declared compensation |
| **Emergency stop** | none | credential revocation (coarse) | kill switch with a **no-race guarantee** at dispatch (RFC §9) |
| **Audit** | decision logs | access logs | **transactional evidence**: audit shares the settle transaction; `resultRefs` for reconciliation (RFC §11) |
| **Policy analysis / tooling** | **mature** (Cedar: formal verification; OPA: large ecosystem) | mature | a 13-rule linter — deliberately minimal |

The last row is the honest one — see §4.

---

## 3. Where each belongs (they compose, not compete)

- **IAM stays as the outer ring.** The *gateway's own* credentials to downstream systems are IAM-scoped, so even a gateway bug can't exceed them — and killing an agent can rotate them ([`02`](02-implementation-design.md) §8.7). IAM answers "may this service call this API at all"; it cannot see that a $50,000 payment to a new payee inside an allowed API call is the thing needing two humans.
- **OPA/Cedar slot into the authorize step.** RFC §12 step 2 (default deny → deny wins → allow match) is exactly the shape of a PDP query. An organisation standardised on policy-as-code can implement that step with its engine of choice behind the `authorize` protocol — the seam ships as a protocol, not a build ([`03`](03-architecture-decisions.md) key decision 9). Everything a PDP cannot express — gates with `hold`, staging, kill, transactional audit, scope realisation in connectors — remains the gateway's.
- **Guardrails / moderation models are hooks, not the enforcement layer.** A probabilistic classifier inside the model's loop is the component you're defending *against*, not with. ACP's place for them is the `contentCheck` gate: an external service may be as clever as it likes, but it returns a **deterministic verdict at a deterministic point**, and its failure is governed by `failureMode` (RFC §7.7, §10).

```
IAM (outer ring: what the GATEWAY may touch)
  └─ ACP gateway (chokepoint: what the AGENT may do, per action/parameter/instance)
       ├─ authorize step  ←  built-in matcher  OR  your OPA/Cedar (the seam)
       ├─ gates            (counters, holds, content hooks, windows…)
       ├─ staging / kill / transactional audit
       └─ connectors       (scope applied below the model)
```

---

### 3.1 The closest real system: AWS Bedrock AgentCore

AgentCore is the strongest evidence the category is real: it already runs **Cedar inside an agent runtime** — intercepting tool calls through an MCP gateway, evaluating each before access (including argument values), with human approval available through a separate orchestration layer. Two differences remain, and they are the two things this project is about:

- **How the action surface is modelled.** AgentCore constrains the agent to registered tools with typed argument schemas — a per-tool-name, per-argument view. ACP generates the agent's *entire intent vocabulary from one domain model*, and its five action **kinds** plus **governance attributes** (reversibility, emission, operative force, result sensitivity) let one policy line reason about the *nature* of an action uniformly — `when action.reversibility == irreversible` covers every current and future action, with no per-tool enumeration. A difference of abstraction, not raw capability.
- **Its shape.** In AgentCore, approval, orchestration, and audit are assembled from separate AWS services and coupled to AWS. In ACP, staging, approval holds, kill (with the no-race guarantee), and transactional audit are **first-class parts of one model** — portable across any stack, and specified tightly enough to be independently implemented and audited.

## 4. What OPA and Cedar do better — honestly

- **Policy analysis.** Cedar ships formal, automated reasoning about policy sets ("can any principal ever do X?"); OPA has a large testing/CI ecosystem. ACP's linter is thirteen rules. If you need to *prove properties across thousands of policies*, use their engines for the authorization step — via the seam.
- **Organisation-wide policy-as-code.** OPA governs Kubernetes admission, CI, microservices — one language across the estate. ACP deliberately governs one thing: agent actions at a gateway. It should not become your org's general policy plane.
- **Maturity.** Years of production hardening, tooling, and hiring pools. ACP is a concept-stage reference implementation.

ACP's bet is not "a better policy language." It is that agent governance is **80% machinery, 20% decision** — and the machinery (SIF's typed surface, staging, holds, kill, transactional evidence) is precisely what no PDP provides. The policy language is kept small *on purpose*, so a security officer can read and certify a policy in one sitting (RFC §1); anything needing industrial-strength decision logic can delegate to an industrial-strength decision engine through the seam.

---

## 5. The gap, attack by attack

§2's table says what differs; this section says what the difference *stops*. Every entry lives in the same structural gap: the space between "the decision was correct" and "reality obeyed the decision." A PDP is not there when that space is attacked; a gateway that owns dispatch is.

1. **Bypass / coverage gaps.** PDP enforcement holds only if every call site remembers to ask — an unwrapped tool or a new code path is a silent full bypass, the perennial PDP failure mode. SIF-native has no unwrapped path to forget: coverage is structural, not a per-integration promise.
2. **TOCTOU / stale decisions.** A verdict at time T, execution at T+n, nothing re-checks — a payee sanctioned after approval, a record that changed tenants between check and write. Dispatch-time re-validation inside a serialized transaction (RFC §12, §6.3) is only buildable because the decider owns dispatch; a PDP architecturally isn't present when the window closes.
3. **Salami-slicing / aggregates.** Fifty refunds each under the limit. A stateless PDP needs external counter plumbing fed to it — which is where these controls quietly rot. `rate`/`quota`/`spendLimit`/`quantityCap` are first-class and stateful because the gateway owns the store.
4. **Result-side exfiltration.** A PDP authorizes the request and never sees the response — but data leaks through results. The `disclosure` post-check withholds rows on the return path (RFC §7.12); nothing else in the stack sits on the return path at all.
5. **Hold-and-approve.** allow/deny is not a big enough verdict space. Approval, dual authorization, and timeouts are stateful workflows needing staged execution underneath — bolting them onto a PDP means building this gateway around it.
6. **Kill with no race.** Halting means reaching actions *already authorized but not yet dispatched* — the locked `PENDING → DISPATCHING` transition. A PDP can flip a policy for future queries; it cannot reach actions past the decision point, because it never held them.
7. **Repudiation.** PDP decision logs are a best-effort side channel; effect-without-record is possible under crash or partition. Transactional audit (CS-006) is only expressible because the auditor and the executor share a transaction.
8. **Pre-decision containment.** A PDP evaluates whatever arrives — a hallucinated field or an attacker-supplied internal ID has already reached the enforcement point. Enum injection means undeclared names can't be *emitted*; `resolve` means the agent never handles raw identifiers it could be tricked into swapping.
9. **Lifecycle out-of-order.** A PDP can express a from-state rule but must trust the caller to supply the state — the classic trust-the-input hole. The gateway reads the resource's current state itself.

None of this makes OPA/Cedar deficient — they are a different component (see §4, and the seam in §3). The one-liner: **a policy engine answers questions; the gateway makes reality match the answers.**

---

## 6. Pre-action authorization credentials (OAP-style passports)

A newer neighbour deserves its own comparison: **agent passport schemes** (Open Agent Passport and similar) — a portable, cryptographically verifiable credential stating who the agent is and what capabilities/limits it carries, evaluated by a `before_tool_call` hook or hosted decision API that returns a signed allow/deny. The framing overlaps this project's; the layer does not. A passport scheme deliberately does **not own execution** — the caller receives the verdict and is trusted to obey it. That reproduces the §5 gap list, and in places sharpens it:

- **In-process enforcement.** The hook runs inside the agent's own process, per framework — the enforcement code shares a process with the very component assumed hijackable. An unwrapped tool, an unintegrated framework, or a direct API call never fires it. (§5.1, worse.)
- **Cached decisions.** Passport specs explicitly allow relying parties to cache allow-verdicts until expiry (hour-scale in their own examples) — a revocation, sanction, or exhausted limit inside that window still executes. (§5.2, codified.)
- **Advisory verdicts.** A signed decision object proves a decision was *issued*, not that reality matched it: no staged effects, no hold state, no kill reaching in-flight actions, no transactional audit. Notably, the escalate-to-human path in such schemes tends to be specified but unimplemented — because human-in-the-loop *requires* a staging substrate underneath the decision service. (§5.5–5.7.)
- **Trust-the-caller context.** Passport limits are checked against caller-supplied parameters and context; no target resolution, no scope injection below the model, no lifecycle state read from the system of record. (§5.8–5.9.) Partial credit: daily caps live in the passport schema, so aggregates are at least addressed — in tension with the decision-caching model.

And the honesty column, which matters more here than with OPA: **passport schemes have what this project lacks entirely** — cryptographically verifiable, portable identity (DIDs, W3C Verifiable Credentials, assurance levels) and cross-organizational attestation. This gateway takes `agent:` and `actor:` as authenticated-session facts and verifies nothing cryptographic about them; in any scenario where you must verify *someone else's* agent, the passport addresses a problem this design doesn't touch.

Which is why the two compose rather than compete: **a passport proves who the agent is and issues a verdict; this gateway makes reality obey verdicts.** Concretely: the passport slots in as the identity input at the gateway's `actor:`/`agent:` seam, and the passport's escalation path is this gateway's shipped `hold` + staged execution. Passport control, versus the vault door and the ledger.

---

## 7. The one-paragraph version for the meeting

> OPA and Cedar answer "is this allowed?" — statelessly, assuming someone else intercepts the action and enforces the answer. For an AI agent, nothing intercepts by default, "allowed" isn't binary (actions must be *held* for humans and *halted* by operators), and the risk lives in parameters and instances IAM can't see. The ACP gateway is the interception layer plus the four-verdict machinery — typed intents, staged effects, approval holds, a race-free kill, transactional audit — with a deliberately small built-in policy language and a protocol seam where OPA or Cedar can serve as the authorization step if your organisation already standardises on one. Keep IAM as the outer ring; add ACP as the agent's chokepoint; plug in your policy engine if you have one.
