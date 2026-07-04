# Renaming — from "Agent Control Policy" / `agent-control-protocol`

**Date:** July 2026. **Status: DECIDED, NOT YET EXECUTED** — this records the rename
decision ahead of its execution; every artifact in the repo still carries the ACP names
until the mechanical sweep runs. Once executed, this page is kept permanently as the
honesty artifact: anyone who saw an old name can confirm this is the same project, and
the history is visibly not hidden.

> **Supersedes the earlier "Interlock" proposal.** A prior revision of this page recorded
> *Interlock* (product) / *Interlock Policy Language* (spec) as the decided names, gated on
> the author's due diligence. That due diligence **disqualified Interlock: "Interlock" is
> the name of an active ransomware group** — a fatal association for a security product,
> and exactly the kind of collision the gate existed to catch. The name was dropped. The
> domains **stonefold.ai** (primary) and **stonefold.io** (held defensively) are now
> reserved, and a collision check settled the language name. The decision below —
> **Stonefold** (product) / **Stele** (policy language) — replaces it. Interlock is recorded
> here only so the trail is honest; it was **never applied to the repo** (the code was still
> ACP), so the sweep goes ACP → Stonefold/Stele directly.

## What changes

| Layer | Old name (in repo today) | New name |
|---|---|---|
| The product / gateway | ACP Gateway ("Agent Control Gateway") | **Stonefold** (the Stonefold Gateway) |
| The policy language (RFC 01) | Agent Control Policy (ACP) | **Stele** (the Stonefold policy language) |
| The intent format (RFC 00) | SIF (Structured Intent Format) | **SIF** — unchanged |
| Policy file `apiVersion` | `acp/v0.1` | `stele/v0.1` |
| Policy file extension | `*.acp.yaml` | `*.stele.yaml` |
| Policy JSON Schema | `schema/acp.schema.json` | `schema/stele.schema.json` |
| Python packages | `acp_*` | `stonefold_*` |
| Repository | `agent-control-protocol` | `stonefold` (old GitHub URLs will redirect) |

**The naming split is deliberate: Stonefold is the machine; Stele is the tablet the
machine reads.** "Stonefold" names the product and its code packages (the enforcement
engine, the runtime); "Stele" names the policy language and everything *written in it* —
the file extension, the `apiVersion`, the schema. SIF (what the agent emits) keeps its own
name, unchanged.

One sentence for the whole stack: **the agent speaks SIF; Stonefold enforces; the rules
are carved in Stele — and nothing else can act.**

## Why these names

- **Collision (product).** "ACP" collides with the Agent Client Protocol (Zed/JetBrains
  ecosystem) and IBM's Agent Communication Protocol. The old repo name said "protocol"
  while the README said "gateway" — a muddled identity at the moment clarity mattered.
  "Stonefold" is clear in software: a collision check found only an unrelated Lake District
  holiday-rental business, nothing in software or security (the defunct network-security
  firm *Stonesoft*, acquired by McAfee in 2013, is a different word — negligible).
- **Posture.** "Agent Control Protocol" claims a category — "the standard way agents shall
  be controlled" — which a pre-adoption project has not earned. "Stonefold" claims a
  product: a specific, opinionated mechanism; adopt it or don't. The design genuinely is
  opinionated (frozen kinds, frozen gates, a deliberately small condition language), and a
  named product may say "the shape is frozen; that's the point."
- **Why a name, not an acronym (language).** The obvious three- and four-letter names for a
  "Stonefold Policy Language" all collide *inside our own buyer's toolbelt*: **SPL** is
  Splunk's Search Processing Language; **SpEL** is the Spring Expression Language — the
  language Spring Security uses to write `@PreAuthorize` access-control expressions (same
  domain *and* same function); **SPP** is Microsoft's Software Protection Platform, and
  "protocol" is the framing we retired anyway (SIF owns that slot). The category leaders do
  not use acronyms — AWS **Cedar**, OPA **Rego**, HashiCorp **Sentinel** — and a distinct
  evocative name reads as more serious than a taken TLA. **Stele** is clear in the
  authorization/policy category (none of Cedar, Rego, Sentinel, Styra, Oso, Cerbos,
  Axiomatics overlap; the only users are a handful of small, unrelated software firms). Its
  one cost is pronunciation — *STEE-lee* is not obvious from the spelling — a real but
  survivable tax for a developer tool (cf. Rego, nginx, Kubernetes).

## The metaphor is the architecture

- **Stonefold** — a *fold* is an enclosure (as in a sheepfold): the flock roams freely
  inside, but it cannot leave. Build the fold of *stone* and it cannot be broken out of.
  That is the project's whole thesis: the agent inside can be confused, jailbroken, or
  fully hijacked and **it does not matter**, because containment is a property of the wall,
  not the occupant. The wall does not reason (no LLM in the enforcement path — deterministic
  mechanism); there is exactly one gate and it is shut unless a rule opens it (default-deny);
  the gate can take two keys (dual authorization); and the fold can be sealed instantly
  (the kill-switch, including its no-race property).
- **Stele** — an upright stone slab inscribed with law. The oldest surviving legal codes
  (Hammurabi's among them) were carved on a stele and set up in public, where anyone could
  read the rule and no one could quietly alter it. A policy *carved in Stele* is exactly
  that: the rule made permanent, public, and immovable — deterministic, auditable, and
  beyond the reach of the agent it governs. Cut stone, not soft clay.

## What deliberately does NOT change

- **SIF** — the intent format's name, spec, and semantics stay untouched.
- **All normative semantics.** The rename changes identifiers, titles, and file names only.
  No MUST/SHOULD/MAY wording moves. Version numbers are not bumped by the rename;
  `stele/v0.1` accepts exactly the files `acp/v0.1` accepted.
- **The historical change sets** (`docs/RFC-changeset-*.md`) keep their original wording,
  including old names — they describe past versions, and rewriting them would falsify
  history.

## Standardization posture (recorded so it isn't re-litigated)

Opinionated named product now; the conformance kit (docs/12) is the standing answer to
"can others implement this without depending on the author." If standardization becomes
real — a working group, a second independent implementer — donating the spec under a
neutralized name at that moment is a move made from strength. The reverse order (neutral
standard name first, adoption never) is the posture this rename retires.
