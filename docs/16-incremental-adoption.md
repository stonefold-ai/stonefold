# 16 — Incremental adoption: from an existing tool estate to structural containment

*Non-normative companion. Every piece of this path exists in the spec and the tooling —
this page narrates them as the ramp an organisation actually walks, with the coverage
guarantee at each stage stated honestly. The principle: enterprises adopt ramps, not
cliffs. Nothing here asks anyone to rewrite their agent on day one.*

The two transport bindings this builds on are normative (SIF RFC §7): **interception**
(existing tool/MCP calls mapped to declared actions; an unmapped call MUST be denied)
and **SIF-native** (the agent's only surface is one registry-generated `submit_intent`
tool; coverage is structural). The ramp is simply: enter through the first, migrate
entity by entity to the second, in risk order.

---

## Stage 0 — Draft the domain model (hours; no enforcement, no risk)

Point the registry generator (docs/06 §9) at what already exists — the agent's MCP
`tools/list`, the SQL DDL, the OpenAPI spec — and get a **draft registry**: entities,
actions with *suggested* kinds and governance attributes, every guess marked
`TODO(review)`. Ambiguity defaults to the more-governed reading (an unknown verb drafts
as an `effect`, never as a harmless read).

**What you have:** a reviewable statement of what your agent can actually touch —
frequently the first such document the organisation has ever had. Reviewing it is a
code-review transaction, not an authoring project; the compliance officer can hold the
pen. **Coverage guarantee: none yet — nothing is enforced.** Honest cost note: the
generator drafts the *skeleton*; the judgment calls (attributes, from-states) and the
connector/predicate implementations are the real work and arrive in later stages.

## Stage 1 — Interception: govern the estate as it stands (days–weeks)

Terminate the agent's existing tool/MCP transport at the gateway. Each incoming call is
mapped to a declared action, enforced (authorize → scope → gates), then forwarded or
refused. **Unmapped calls are denied** (SIF RFC §7) — never silently passed — and the
startup **coverage check** fails if the agent holds any tool endpoint that does not go
through the gateway (architecture decision 1). The agent itself is unchanged.

**What you have:** default-deny, per-action policy, stateful limits (`rate`/`quota`/
`spendLimit`), approval holds, the kill-switch, and transactional audit — over the
tool surface you already run. **Coverage guarantee, honestly: configuration-based, not
structural.** Coverage equals what is mapped, which is the same *class* of guarantee as
any tool-wrapping gateway — stronger only in that unmapped ⇒ deny and the startup check
make the gaps loud instead of silent. The agent's expressive surface is still the raw
tool sprawl; what changed is that nothing it says reaches the world unchecked.

## Stage 2 — Migrate entity by entity, highest risk first (weeks, per entity)

Pick one entity — the one whose actions **move money, touch a regulated record, or are
irreversible** (the docs/13 ranking; governance attributes make the ordering mechanical:
`irreversible` and high `operativeForce` first). For that entity:

1. finish its registry review (attributes, from-states, scope predicates),
2. implement its connector (stub generation is the tooling roadmap's top item),
3. add the entity to the `submit_intent` schema the agent is given,
4. **remove the corresponding raw tools from the agent's surface** — the entity's raw
   verbs stop existing for the agent; the intent tool is the only way to reach it.

Both bindings run side by side through one gateway, one policy, one audit stream during
the whole migration — there is no cutover event.

**What you have:** structural containment *for the migrated entities* (for them, the
hallucination/injection surface is closed: undeclared names cannot be emitted, raw
identifiers are never in the agent's hands), interception-grade coverage for the rest.
The guarantee upgrades **per entity**, exactly where the risk is, exactly when you pay
the connector cost — the spend follows the risk ranking, not a big-bang project plan.

## Stage 3 — SIF-native: the surface is the guarantee

The last raw tool is retired. The agent holds exactly one tool; its schema is generated
from the reviewed registry; enum injection bounds what a confused or injected model can
*say*, not merely what passes. **Coverage guarantee: structural.** "Did we wrap
everything?" stops being an audit question because there is nothing to wrap — the
question becomes "what does the registry declare?", which is a document review, plus
"is the executing code what was declared?", which digest pinning answers (docs/06 §5)
and the trust-boundary page bounds honestly (docs/13).

---

## The ramp at a glance

| Stage | Agent changes? | Coverage guarantee | The cost you pay |
|---|---|---|---|
| 0 draft | no | none (nothing enforced) | a review meeting |
| 1 interception | no | mapped + loud gaps (unmapped ⇒ deny; startup coverage check) | mapping table, policy file |
| 2 per-entity | per entity: raw tools removed | structural for migrated entities, mapped for the rest | registry review + connector, per entity |
| 3 SIF-native | one tool total | structural (nothing to wrap) | the last connectors |

Every stage is independently useful and independently reversible (each is gateway
configuration plus registry state — rolling back a stage does not touch the agent's
reasoning or the systems of record). Stage 1 alone is roughly what commodity agent
gateways offer, which is exactly the point: the entry cost matches the market's
afternoon-sized expectation, and the differentiated guarantees are bought entity by
entity where an auditor actually demands them — never as a leap of faith.

*See also:* [`00-RFC-sif-intent-format.md`](00-RFC-sif-intent-format.md) §7 (the two
bindings, normative), [`06-registry-domain-model.md`](06-registry-domain-model.md)
(registry + generator), [`13-who-is-this-for.md`](13-who-is-this-for.md) (risk ranking
per industry), [`10-positioning-policy-engines.md`](10-positioning-policy-engines.md)
(what each stage does/doesn't defend, attack by attack).
