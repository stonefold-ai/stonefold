# 21 — Why not just tools?

*The case for putting a gateway between an agent and the systems it acts on, made to a
reader whose tools already work — including a straight account of when not to bother,
and of the part of this design most people question first.*

**Supporting doc — context, not normative.** If you have five minutes and one decision
to make ("is this worth my time?"), this page is the argument. The technical follow-ups
are linked at the end.

---

If you build agents, you already have a way to let them act: you register tools, the
model calls them, the calls execute. Function calling is mature, MCP gives you an
ecosystem of ready-made servers, and nothing about it feels broken. So when a product
proposes a gateway in front of those tools, a policy file, a declared registry of what
each action means, and eventually a typed intent format instead of tool calls, the
reasonable reaction is: that's indirection I didn't ask for.

This page tries to earn that indirection. It will also tell you plainly when you
shouldn't adopt it, because the honest answer is that some people shouldn't, yet.

## 1. Where plain tools are the right answer

If a human confirms every consequential action, tools are fine. The human is your
enforcement layer, and a good one. A chat assistant that drafts an email and waits for
you to press send doesn't need a policy engine.

If the agent only reads, and the data isn't sensitive, tools are fine.

If you're building a coding agent, tools are fine for a deeper reason: the software
loop comes with its own safety equipment. Tests verify the work, git makes it
reversible, review provides oversight, and a sandbox contains the stakes. That's why
coding became the first place agents genuinely work unattended.

And if you're prototyping, don't pay any governance tax at all. Get the thing working
first.

## 2. The line is unattended operation

The situations above share one property: either someone reviews each action, or the
environment itself catches and undoes mistakes. The economic promise of agents is to
remove exactly that. An agent that handles invoices, customer messages, records, or
devices is only worth having if nobody reviews every step. That delegation is what
everyone is building toward, and it's where the plain-tools picture changes.

A tool-calling agent is fine on the median input. What decides everything is its
behavior on the worst input it will ever see, because unattended means the worst input
gets no reviewer.

There's a structural fact underneath. A language model has no separation between
instructions and data: the system prompt, the user's request, the email it just read,
the web page it just fetched, all of it is one stream of tokens, and any of it can
steer the next action. This isn't a bug that better prompting fixes; the model vendors
themselves tell you not to rely on the model to enforce security boundaries. If the
agent reads untrusted content and can act, then whoever writes that content has a say
in what the agent does — and with access to anything private, you have all three
ingredients of what Simon Willison calls the lethal trifecta.

So the practical question for an unattended agent isn't "will the model behave?" It's
"when the model misbehaves, what bounds the damage?" With plain tools the answer is
the tool surface itself: everything registered is reachable, on any input, for any
reason the context talked the model into.

Whatever bounds the damage has to sit **below the model** and be **deterministic** —
ordinary software that the prompt cannot argue with. That's the product: a gateway in
the action path, a policy file (Stele) that says what's allowed under which conditions,
and a registry that gives each action a reviewed meaning. Default deny; explicit deny
wins; effects are staged so a human can approve, an operator can kill in flight, and
every decision lands in an audit record that shares a transaction with the change it
records.

## 3. "I already validate inside my tool handlers"

This is a fair objection and deserves a straight answer: a narrow tool with server-side
validation is the same idea. `send_invoice_email(invoice_id)` with checks in the
handler is a small, hand-rolled version of what the gateway does. If you have five such
tools and one agent, you may genuinely not need more. You've already built a tiny
gateway; it just isn't called one.

The question is what happens as that layer grows. Some things it will be asked, sooner
or later:

- What is the complete list of things this agent can do? Could you hand the answer to
  a reviewer as a document, rather than a tour of the codebase?
- When a teammate adds a tool next month, does anything force it through review before
  the agent can call it, or is the default that it just works?
- If three different tools can move money, where does the shared daily limit live?
- When an action needs human sign-off, where does it wait? What happens if nobody
  answers? Can an operator stop everything currently in flight?
- After an incident, can you reconstruct what the agent did and why it was permitted,
  down to the ids of the downstream records it touched?
- If you swap models or rename tools, which of your guards survive?

None of these is exotic, and each has an ad-hoc answer. The trouble is that ad-hoc
answers live scattered across handler code, written under deadline, and they rot
quietly: the new tool ships without a guard, the limit is enforced in two of the three
payment paths, the audit trail is whatever logging survived refactoring. Silence is the
failure mode, because nothing tells you the coverage has a hole.

The gateway is the same guards with the scatter removed. One registry declares what
exists and what each action means (is it reversible? which argument is the money? which
column is the tenant key?). One policy file says what's allowed, so "what can this
agent do?" has an answer security or compliance can read and sign. A call nobody has
mapped is refused rather than passed through, which turns tool-estate drift into a loud
error instead of a silent hole. Approvals, spend counters, staging with a kill switch,
and audit with result lineage come with the layer instead of being rebuilt per project.

You do not have to change your agent to get this. In **interception mode** the gateway
terminates the agent's existing MCP/tool transport, a mapping table assigns each tool
its declared meaning, and the ordinary pipeline runs on every call. Entry cost is the
mapping and a policy file; the agent, the framework, and the model stay as they are
(docs/16, Stage 1).

## 4. The part you'll question most: SIF

Up to here, most engineers nod along; a policy proxy over tools is a familiar shape.
The harder sell is the second binding: the agent stops calling tools altogether and
emits **SIF**, a typed intent ("this declared action, on this entity, with these
values") against a vocabulary generated from the registry. Why go that far?

Because interception, honest as its guarantee is, has a ceiling — and the ceiling is
exactly the original problem in a smaller room:

- Its coverage is configuration-based. Everything mapped is enforced and everything
  unmapped is loudly denied, but "did we map the whole estate, and is the mapping still
  in sync?" remains a standing maintenance question.
- A mapped tool can itself be an escape hatch. `run_sql(query: string)` maps to one
  declared action no matter how many different things the string can do; the free-form
  argument is a hole the policy can flag but not close.
- The agent's expressive surface is still the raw tool sprawl. Policy checks what
  passes; nothing bounds what the model can say.

SIF closes those three, and the shape of the fix is one we've used before. String-
concatenated SQL also worked, until attacker-controlled input reached an interpreter
that couldn't tell data from instructions; what stuck was prepared statements, which
restrict the untrusted party to filling typed slots in a structure it cannot change.
SIF is that move applied to agency. The agent's only surface is one generated tool
whose legal entity, action, field, and value names are injected as enums from the
reviewed registry. There is no verb for "run this", no free-form command field, no way
to name what nobody declared. An injection can still make the agent *want* something
harmful; it can only want it in your vocabulary, where a deterministic gate decides.
Coverage stops being an audit question ("did we wrap everything?") and becomes a
property of the surface — there is nothing to wrap, and no mapping to drift.

This isn't one project's private theory. Security work on agents keeps arriving at the
same place from different directions: the 2025 design-patterns paper by Beurer-Kellner
and colleagues, and DeepMind's CaMeL, both conclude that once an agent has ingested
untrusted input, its ability to express actions must be structurally constrained,
because nothing at the model level reliably filters the input.

There's also a payoff unrelated to safety: constraining what the agent can express
makes it more reliable. Enum-injected names mean the model can't hallucinate a
plausible-but-wrong tool or field, and structured errors ("Patient has no field
'email'; did you mean 'contactEmail'?") let it self-correct on the next turn instead of
failing opaquely.

And the honest part: SIF is the destination, not the entry fee. It costs a connector
per entity, and it's the piece that feels most like indirection. That's why the
adoption path (docs/16) enters through interception and migrates entity by entity,
highest risk first — the payment entity long before the calendar — with both bindings
running side by side through one gateway, one policy, one audit stream. You buy the
structural guarantee exactly where the stakes justify it.

## 5. What it costs, honestly

The registry has to exist. A generator drafts it from what you already have (an MCP
server's tool list, SQL DDL, an OpenAPI spec), but a human must review the judgment
calls, because those are the actual content: which actions are irreversible, which
argument is the money. Budget a real review, not a rubber stamp.

Interception mode costs a proxy deployment, the mapping, and a policy file, with zero
agent changes. Each entity you migrate to SIF-native costs its connector. You take on
a runtime component in the action path, and one more layer to look through when
debugging; the enforcement itself is lookups and deterministic checks, so the
indirection is the cost to count, not the latency.

## 6. What it does not do

The gateway bounds reach; it does not supply judgment. An action that is wrong but in
bounds will execute. Policy can shrink that space — v0.6 can require that a payment
match an open purchase-order line within tolerance before it dispatches — and holds
route genuinely ambiguous cases to a human, but "permitted" never means "correct".
Connectors and the gateway itself are trusted code; the guarantee is that intents
conform to policy, not that the code below the policy is bug-free. Anyone who tells you
a layer like this makes agents safe is overselling. What it makes them is bounded,
auditable, and stoppable, which is what lets a responsible person sign off on removing
the human from the loop at all.

Status, plainly: SIF is a v1.0 format spec and Stele a v0.6 policy spec, both evolving
in the open under Apache-2.0, with a Python reference implementation, a runnable
real-LLM demo, and a conformance test kit so other implementations can certify
independently. It's a serious effort and an early one, currently driven by one person.
Judge it as that.

## 7. The five-minute verdict

Three questions decide whether this is worth more of your time:

1. Does your agent act on things that matter — money, customer communication, records,
   devices — without a person reviewing each action? Or do you want it to within the
   next year?
2. Can you answer "what is the worst thing it can do?" from a document today, rather
   than from a code audit?
3. When someone finally has to sign off on running it unattended, what will you show
   them?

If the answer to the first question is no and will stay no, close the tab; plain tools
are the right call and this layer would be overengineering for you. If it's yes and the
other two make you uncomfortable, that discomfort is the gap this fills. You'll build
this layer either way; the choice is whether it's scattered through your handlers or
declared where someone can read it.

---

*Where to go next:* [`docs/16-incremental-adoption.md`](16-incremental-adoption.md)
(the ramp: draft → intercept → migrate → SIF-native, with the honest coverage guarantee
at each stage) · [`docs/13-who-is-this-for.md`](13-who-is-this-for.md) (industries,
blocking risks, who signs) · [`docs/10-positioning-policy-engines.md`](10-positioning-policy-engines.md)
(why OPA/Cedar/IAM alone don't cover this, and how they compose with it) ·
[`spec/docs/17-interception-mapping.md`](https://github.com/stonefold-ai/spec/blob/main/docs/17-interception-mapping.md)
(how mapping ordinary tool calls works, and its limits) ·
[`spec/docs/00-RFC-sif-intent-format.md`](https://github.com/stonefold-ai/spec/blob/main/docs/00-RFC-sif-intent-format.md)
(the SIF format itself) · [`docs/15-benchmark-design.md`](15-benchmark-design.md)
(pilot measurements of the reliability claim in §4: selection accuracy and token cost,
SIF surface vs raw tool surface, raw logs in the repo).
