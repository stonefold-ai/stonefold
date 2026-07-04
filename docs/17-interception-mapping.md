# 17 — The interception mapping: how ACP interprets ordinary tool calls

*Non-normative companion. The normative basis is one sentence in the SIF RFC §7
("existing tool/MCP calls are mapped to SIF operations by the gateway; an unmapped call
MUST be denied") and the proxy mechanics in docs/02 §1.2. This page states the idea those
lines compress — because it is the single most common misunderstanding of the design.*

---

## 1. The question this answers

**"Can ACP enforce its policies when the agent uses ordinary MCP tools — no `submit_intent`,
no SIF emitted by the agent?"**

Yes. But the *how* has a crux worth stating precisely, because both naive readings are wrong:

- **Wrong reading 1:** "ACP understands tool calls." It does not. A raw call like
  `create_payment(vendor="X", amount=4200)` carries no governance semantics — nothing in
  it says what resource it touches, whether it is an `effect` or a `read`, whether it is
  irreversible, or which argument is the money. MCP tool schemas describe *syntax*
  (argument names and types), not meaning. There is nothing for a policy to evaluate.
- **Wrong reading 2:** "So ACP needs the agent to speak SIF." It does not. The gateway
  synthesizes the SIF operation *on the agent's behalf*, from the tool call — and can do
  so deterministically, because a human declared the interpretation ahead of time.

The bridge is the **mapping**: a declared, reviewed table that assigns each tool a meaning
in the registry's vocabulary.

## 2. What a mapping entry declares

Per tool, ahead of time, in the registry (see docs/06):

| Tool-call surface | Declared meaning |
|---|---|
| tool name `create_payment` | action `payment.create` on entity `Payment`, kind `effect` |
| argument `amount` | attribute `amount` — the value `spendLimit` gates read |
| argument `vendor` | attribute `payee` — checked against the vendor named set |
| (nothing in the call) | governance attributes: `reversibility: irreversible`, `operativeForce`, … |

At runtime the gateway performs a **lookup, not interpretation**: tool name → declared
action, arguments → typed attributes, then the ordinary pipeline runs unchanged
(authorize → scope → gates → outbox → audit). No inference, no classifier, no LLM —
which is exactly what invariant 1 (deterministic enforcement) requires. The *meaning* of
every tool call is a versioned, human-signed artifact, not a runtime judgment.

And the contrapositive is a feature, not a gap: a tool call with no mapping entry is
**denied, always** — never passed through, never guessed at. If the MCP server grows a
new tool tomorrow, the agent can retrieve and call it, and the gateway refuses it until
someone declares what it means.

## 3. Why mapping wins (the advantages)

1. **Deterministic interpretation.** The one place where "understanding a tool call"
   could have smuggled a model into the enforcement path is instead a table lookup.
   Same call, same meaning, every time — testable, diffable, certifiable (docs/12).
2. **Zero agent changes.** Any agent, any framework, any model that speaks MCP or plain
   tool calls is governed as-is. The adoption entry cost is a mapping table and a policy
   file, not an agent rewrite (docs/16, Stage 1).
3. **Policy is written against semantics, not tool syntax.** `spendLimit` gates the
   `amount` attribute of `payment.create` — regardless of whether the wire call was
   `create_payment`, `pay_vendor`, or an OpenAPI `POST /payments`. Tools can be renamed,
   split, or re-hosted without touching a single policy rule; N heterogeneous MCP servers
   collapse onto one vocabulary, one policy, one audit stream.
4. **The unknown fails loud.** Unmapped ⇒ deny turns tool-estate drift into an immediate,
   attributable refusal instead of a silent hole. The mapping doubles as a drift detector:
   the gap between `tools/list` and the mapping *is* the ungoverned surface, enumerable
   at startup (the coverage check, docs/02 §1.2).
5. **The mapping is itself an audit artifact.** "What can the agent do, and what does each
   capability mean?" becomes a reviewable document — frequently the first such document
   the organisation has ever had (docs/16, Stage 0). Compliance can hold the pen.
6. **Nothing is thrown away on upgrade.** The mapping work *is* the registry review. The
   same reviewed entries later generate the `submit_intent` schema for the SIF-native
   binding — migration is per-entity removal of raw tools (docs/16, Stage 2), not a
   re-authoring project.

## 4. Honest limits (unchanged from docs/02 and docs/16)

- **Coverage is configuration-based, not structural.** The guarantee is "everything mapped
  is enforced and everything unmapped is loudly denied" — not "nothing escapes." Closing
  network paths that bypass the proxy is a deployment obligation.
- **A mapped tool can itself be an escape hatch.** A raw `run_sql(query: string)` maps to
  *one* action however many things the string can do. The mapping layer flags free-form
  string arguments as high-risk pass-throughs requiring explicit acknowledgement
  (docs/02 §1.2); the real fix is migrating that entity to SIF-native.
- **The table must be kept in sync** with a live tool estate. That maintenance burden is
  the recurring cost of this binding — and the thing the SIF-native binding structurally
  removes (one generated tool; nothing to sync).

## 5. "But declared vocabularies lost" — why the old objection is dead

The historical argument against this approach is real: schemas-first ecosystems
(ontologies, WS-*, semantic web) lost to schemaless ones largely on **authoring cost** —
nobody wanted to write and maintain the model, so the model rotted, so it was abandoned.

That economics has inverted. Drafting the schema is now the trivial part: the registry
generator (docs/06 §9) points an LLM-era toolchain at what already exists — the MCP
server's `tools/list`, the SQL DDL, the OpenAPI spec — and emits a draft registry with
suggested kinds, typed attributes, and handler stubs in minutes. What remains human is
exactly the part you *want* human and that was always the true content of the exercise:
the judgment calls (is this irreversible? which argument is the money? which column is
the tenant key?), each marked `TODO(review)`, reviewed like a code change.

So the trade has changed shape. You are no longer paying months of ontology authoring to
buy governance; you are paying a review of a generated draft (the connector work still
arrives per entity — docs/16 keeps that cost honest). Meanwhile the *case for having* the
declared model has strengthened for a structural reason: the schemaless trade-off assumed
the caller was a programmer who knew what the call meant. An autonomous agent breaks that
assumption — under confusion or injection it may emit anything its tool surface permits,
and without a declared vocabulary there is no way to even state a bound on that surface.
Declared vocabulary is what makes "what can this agent do?" answerable at all. Schemaless
won the last era on authoring cost; that cost has collapsed to a review, and the
assumption that justified going without a model no longer holds.

---

*See also:* [`00-RFC-sif-intent-format.md`](00-RFC-sif-intent-format.md) §7 (the two
bindings, normative), [`02-implementation-design.md`](02-implementation-design.md) §1.2
(proxy mechanics, coverage check), [`06-registry-domain-model.md`](06-registry-domain-model.md)
(the registry the mapping resolves into; §9 the generator),
[`16-incremental-adoption.md`](16-incremental-adoption.md) (the adoption ramp this binding
anchors).
