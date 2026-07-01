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

## 5. The one-paragraph version for the meeting

> OPA and Cedar answer "is this allowed?" — statelessly, assuming someone else intercepts the action and enforces the answer. For an AI agent, nothing intercepts by default, "allowed" isn't binary (actions must be *held* for humans and *halted* by operators), and the risk lives in parameters and instances IAM can't see. The ACP gateway is the interception layer plus the four-verdict machinery — typed intents, staged effects, approval holds, a race-free kill, transactional audit — with a deliberately small built-in policy language and a protocol seam where OPA or Cedar can serve as the authorization step if your organisation already standardises on one. Keep IAM as the outer ring; add ACP as the agent's chokepoint; plug in your policy engine if you have one.
