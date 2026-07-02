# 11 — Delegation & multi-agent: authority attenuation (exploration)

*Non-normative exploration — v0.5 scope at the earliest. Nothing here is specified, implemented, or promised; it exists so the multi-agent question has a considered answer before it becomes a feature request. The current model (one policy per agent, one actor per session) is deliberately unchanged.*

---

## 1. The pressure

Agent systems are becoming trees: an orchestrator spawns researchers, a coding agent spawns reviewers, an agent calls another team's agent. ACP v0.x governs **one agent at one gateway**; the moment agents spawn agents, three questions appear:

1. **Authority** — what may the spawned agent do? (Surely never *more* than its spawner.)
2. **Accounting** — whose limits does it consume? (A swarm must not multiply rate limits by spawning.)
3. **Accountability** — who did what, on whose behalf, when the run is a tree?

## 2. The one principle: attenuation, never amplification

A delegate's authority is **at most** its delegator's. Formally: the effective policy of a spawned agent is the **meet** (greatest lower bound) of its parent's effective policy and its own declared policy —

```
effective(child) = meet( effective(parent), declared(child) )
```

- an action is allowed only if **both** allow it; a `deny` anywhere in the chain denies it (deny wins, transitively);
- gates take the **more restrictive** value at each level (lower limit, narrower allowlist, stricter window);
- scope predicates **intersect**: the child sees only rows visible to *both* predicates.

The key observation: **ACP already has this operation.** `extends` composition (RFC §3.2) merges fragments with exactly these rules — union, deny wins, more-restrictive-gate wins, "composition MUST NOT widen a permission a fragment denied." Delegation is the same merge run in the other direction: `extends` composes *sideways* at authoring time; delegation composes *downward* at spawn time. One semantics, one implementation, two uses.

## 3. Sketch of the mechanics (unspecified)

- **Spawning is itself a governed effect.** No new SIF kind (the five are frozen): `spawnAgent` is an ordinary declared `effect` in the registry, so a policy gates *who may delegate at all* — `requireApproval` on spawn, `quota: 5/session` on subagents, `deny` for agents that must stay leaf nodes. The gateway is already the chokepoint for it.
- **The actor never changes.** The human principal flows through the whole chain; a child agent acts *for the same actor* as its root (scope stays the actor's, invariant 3). Delegation changes the **agent** identity, never the **actor**. Cross-principal delegation (acting for two humans at once) is explicitly out.
- **Identity is a chain.** The session carries the delegation path (`root-agent/sub-A/sub-A.2`); a spawn issues a child session bound to the parent's, not a fresh top-level one.
- **Counters bound the tree, not the node.** `rate`, `quota`, `spendLimit` for a delegated session key on the **root** session, so twenty subagents share one budget — closing the spawn-to-multiply hole. (A policy MAY additionally cap per-node.)
- **Kill covers the subtree.** A kill scoped to a session matches any chain with that prefix: killing the orchestrator halts every descendant, at the same three check points, with the same no-race guarantee. Killing a leaf leaves the rest running.
- **Audit is a tree replay.** `correlationId` becomes the chain; every record carries the delegation path, so "what did this run do" is one ordered query over the subtree — same audit table, richer key.
- **Approvals show the path.** A held action surfaces *who asked, spawned by whom, for which human* — the approver judges the chain, not just the leaf.

## 4. What stays out

- **Orchestration/workflow** — who spawns what, in which order, with what retries — remains out of scope (RFC non-goals). ACP governs each edge of the tree; it does not schedule the tree.
- **Foreign agents** (another organisation's agent, A2A protocols): not delegates. A remote agent is an **external system** — reach it through a connector, gate it like any effect. Attenuation applies only inside one trust domain.
- **Trust negotiation / capability marketplaces** — far out.

## 5. Open questions (why this is v0.5, not v0.4)

1. Is the meet computed **at spawn time** (compile once, fast, but stale if the parent's policy changes mid-run) or **per request** (always fresh, needs the chain walk on the hot path)?
2. Do child-declared **standing** rules survive the meet, or is context-conditional authority a root-only construct?
3. May a child policy *add* gates and denies freely (surely yes — restriction is always safe) while adding `allow` lines is meaningless under the meet — and should the linter say so?
4. What does `spawnAgent`'s **reversibility** mean — is "kill the subtree" its declared compensation?
5. How does a **decision TTL** (CS-017, v0.4) interact with long-lived delegated sessions?

*See also:* RFC §3.2 (`extends` — the merge this reuses), §9 (kill scopes), §11 (`correlationId`); [`10-positioning-policy-engines.md`](10-positioning-policy-engines.md) (why the machinery, not the decision language, is the product).
