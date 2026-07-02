# Renaming — from "Agent Control Policy" / `agent-control-protocol`

**Date:** July 2026. **Status: DECIDED, NOT YET EXECUTED** — this records the rename
decision ahead of its execution (gated on the author's due diligence); every artifact
in the repo still carries the ACP names until the rename runs. Once executed, this page
is kept permanently as the honesty artifact: anyone who saw the old name can confirm
this is the same project, and the history is visibly not hidden.

## What changes

| Layer | Old name | New name |
|---|---|---|
| The product / gateway | ACP Gateway ("Agent Control Gateway") | **Interlock** |
| The policy spec (RFC 01) | Agent Control Policy (ACP) | **Interlock Policy Language** |
| The intent format (RFC 00) | SIF (Structured Intent Format) | **SIF** — unchanged |
| Policy file `apiVersion` | `acp/v0.1` | `ipl/v0.1` |
| Policy file extension | `*.acp.yaml` | `*.ipl.yaml` |
| Policy JSON Schema | `schema/acp.schema.json` | `schema/ipl.schema.json` |
| Python packages | `acp_*` | `interlock_*` |
| Repository | `agent-control-protocol` | `interlock` (old GitHub URLs will redirect) |

One sentence for the whole stack: **the agent speaks SIF; Interlock decides per the
policy; nothing else can act.**

## Why

- **Collision.** "ACP" now collides with the Agent Client Protocol (Zed/JetBrains
  ecosystem) and IBM's Agent Communication Protocol. The old repo name said "protocol"
  while the README said "gateway" — a muddled identity at the moment clarity mattered.
- **Posture.** "Agent Control Protocol" claims a category — "the standard way agents
  shall be controlled" — which a pre-adoption project has not earned. "Interlock" claims
  a product: a specific, opinionated mechanism; adopt it or don't. The design genuinely
  is opinionated (frozen kinds, frozen gates, a deliberately small condition language),
  and a named product may say "the shape is frozen; that's the point."
- **The metaphor is the architecture.** A safety interlock is a physical mechanism that
  makes an unsafe action *impossible* rather than forbidden: deterministic mechanism,
  not judgment (no LLM in the enforcement path); two-hand controls (dual authorization);
  lockout-tagout (the kill-switch, including its no-race property); and the machine can
  be arbitrarily powerful and faulty, because the interlock doesn't care (the agent can
  be fully hijacked and it doesn't matter).

## What deliberately does NOT change

- **SIF** — the intent format's name, spec, and semantics stay untouched.
- **All normative semantics.** The rename changes identifiers, titles, and file names
  only. No MUST/SHOULD/MAY wording moves. Version numbers are not bumped by the rename;
  `ipl/v0.1` accepts exactly the files `acp/v0.1` accepted.
- **The historical change sets** (`docs/RFC-changeset-*.md`) keep their original
  wording, including old names — they describe past versions, and rewriting them would
  falsify history.

## Standardization posture (recorded so it isn't re-litigated)

Opinionated named product now; the conformance kit (docs/12) is the standing answer to
"can others implement this without depending on the author." If standardization becomes
real — a working group, a second independent implementer — donating the spec under a
neutralized name at that moment is a move made from strength. The reverse order
(neutral standard name first, adoption never) is the posture this rename retires.
