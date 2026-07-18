# Contributing to Stonefold

Thanks for your interest. This repo is the **Python reference implementation** of the
Stonefold gateway, plus the runnable TCK, demos, and benchmarks. The specifications'
canonical home is [stonefold-ai/spec](https://github.com/stonefold-ai/spec) — spec defects
and ambiguities belong there; implementation issues belong here.

Please read this document before writing code. Stonefold is a security project: its value
rests on a small enforcement core and a precisely worded specification, so contributions
work differently here than in a typical "PRs welcome" repository.

**The one-line summary: we agree on intent before we review code.**

## Why these rules exist

Producing a large, plausible-looking pull request now takes hours. Reviewing one still
takes real human attention, and an unreviewed change to an enforcement layer is attack
surface. Review capacity is the scarcest resource in this project; these rules exist to
spend it where it matters. It is also the same idea Stonefold itself is built on:
constrain the action space up front instead of inspecting arbitrary output afterwards.

## The golden rule: proposal before pull request

**Unsolicited pull requests larger than ~200 changed lines will be closed without
review.** This is not a judgment of you or your code; it applies to everyone.

The flow for anything non-trivial:

1. **Open an issue describing your intent** — what you want to change, why, and how it
   affects (or provably does not affect) the guarantees stated in the spec. A few
   paragraphs is enough.
2. **Wait for an explicit go-ahead** from the maintainer. Proposals may be accepted,
   redirected, or declined; declines come with reasons.
3. **Then write the code.** An implementation of an agreed design is welcome at any size,
   LLM-assisted or not.

Trivial fixes (typos, broken links, obvious one-line bugs) skip the proposal step.

## Contribution zones

Not all parts of the project are equally open. Know which zone you are in before
proposing.

### Zone 1 — the spec and the enforcement core

*The specification text (in the [spec repo](https://github.com/stonefold-ai/spec)) and
the enforcement path in this repo: `src/stonefold_core`, `src/stonefold_gates`,
`src/stonefold_gateway`, `src/stonefold_store`, bound by the invariants in
[`CLAUDE.md`](CLAUDE.md).*

This zone is not closed; it is gated by evidence. There are two lanes.

**Lane A — problems. Open to everyone, no proposal needed, and the most valuable spec
contribution there is.** Contradictions between the RFC text, the schemas, and the
fixtures; ambiguities where two independent implementers could reasonably disagree;
fixtures that expose an under-specified corner; attacks that break a stated guarantee
(those go through [`SECURITY.md`](SECURITY.md), privately). Finding a real hole requires
no alignment with the maintainer's design intent — it only requires the hole to be real.
The spec is at v0.5 precisely because demonstrated holes are expected to reshape it
before 1.0.

**Lane B — design changes. Start from a problem, not a solution.** A proposal to change
the spec or the enforcement core must name the demonstrated hole it fixes, or the real
thing the current design cannot express. If the maintainer agrees the problem is real,
the fix is worked jointly through the spec repo's change-set process (`CS-nnn` items),
and you can author the change set. Proposals that arrive as a finished design with no
agreed problem behind it will be declined regardless of quality.

Two standing commitments in this zone:

- **The language's shape is frozen, but that is an architectural rule, not gatekeeping.**
  No new action kinds, gate types, attribute names, or condition operators (Stele RFC
  §13). The designed extension points are registries: resources, actions, named sets,
  scope predicates, and hooks. Most "extend the language" ideas are registry ideas that
  didn't know where the door was — expect to be redirected there, with a sketch.
- **Declines cite the criterion.** Every declined Zone 1 proposal states which test
  failed ("problem not demonstrated", or "expressible today via X") and stays public in
  the issue, so the project's direction is legible before you invest work.

### Zone 2 — the periphery (open)

This is where code contributions are actively wanted. The proposal step still applies for
anything sizeable, but the bar is normal open-source review. Current areas, with their
honest status:

- **Conformance.** The TCK exists (`src/stonefold_tck/`, language-independent, spec at
  `spec/docs/12-conformance-tck.md`) and has so far been run against exactly one gateway:
  this one. Being the first independent implementation to certify, or contributing TCK
  drivers and wire bindings for other languages, would be a milestone for the project.
- **Registries and connectors.** Worked registries and policies for new domains are the
  designed extension point of the whole system and probably the highest-leverage
  contribution here. New connectors implement the existing `Connector` protocol; MCP
  mappers follow `spec/docs/17-interception-mapping.md`.
- **Testing infrastructure.** Fuzzing setups and property-based tests. CI exists
  (`.github/workflows/ci.yml`: fast suite + mypy on 3.11/3.12, integration suite via
  testcontainers) — hardening and speeding it up is welcome.
- **Deployment.** What exists today is the demo's docker-compose stack, which is a demo,
  not production packaging. Wanted: hardened Compose, Kubernetes manifests/Helm, cloud
  recipes, a single-binary local mode. One hard boundary: the enforcement guarantees must
  hold identically in every deployment target; a deployment that weakens or bypasses
  enforcement in any mode is a Zone 1 concern, not a packaging detail.
- **UI and visibility.** Only a thin approvals/trace/kill UI exists inside the demo.
  Dashboards and inspection tools for declared actions, policy state, and audit trails
  are open ground. UI must remain strictly read-and-approve: any UI path that could
  mutate policy or approve actions outside the enforcement flow crosses into Zone 1.
- **SDKs, examples, docs.** Bindings in additional languages, integration examples,
  documentation, developer experience.

**Not open: the control plane.** Policy distribution, multi-gateway coordination, and
environment management do not exist yet; their design is reserved to the maintainer for
now. Issues and design feedback about them are welcome; pull requests will be declined
regardless of quality. This boundary is deliberate and may be revisited.

### Zone 3 — attack scenarios (most wanted)

The single most valuable thing you can contribute is a **test case describing an attack
that Stonefold should block**. These are small, self-contained, verifiable by running
them, and they directly strengthen the project.

Use the existing format: [`tests/acceptance-scenarios.md`](tests/acceptance-scenarios.md)
— Given/When/Then, citing the governing RFC section. Check what is already covered before
writing: prompt injection, database exfiltration, cross-tenant scope violations, and
salami-slicing sequences all have scenarios. New classes, or sharper variants of existing
ones, are what's missing.

An attack that *breaks* the current gateway is even more valuable — report those through
[`SECURITY.md`](SECURITY.md) first, not as a public PR.

## Requirements for any PR touching enforcement behavior

- **Adversarial tests are mandatory and come first.** A PR that changes what gets allowed
  or denied must include tests demonstrating the boundary — both sides of it. The tests
  are read and judged *before* the implementation; if they don't convincingly pin the
  behavior, the implementation won't be reviewed.
- **No capability changes hidden in refactors.** A PR does one thing. Refactoring and
  behavior change in the same PR will be sent back for splitting.
- **Declare your delta against the spec.** State which RFC sections (or `CS-nnn`
  change-set items) your change implements or touches, or state that it touches none.

## LLM-assisted contributions

Using LLMs to write code, tests, or docs is fine here; the maintainer does too. What is
not fine:

- Submitting generated code you have not personally read, understood, and tested. **You
  are the author of record and are accountable for every line you submit.**
- Generated "improvement" PRs with no prior issue (see the golden rule).
- Generated issue or security reports that no human has verified. Unverified AI-generated
  reports waste review capacity and will get you banned faster than anything else.

## Development setup

```
git clone --recurse-submodules https://github.com/stonefold-ai/stonefold.git
                               # the spec/ submodule carries the schemas + fixtures
pip install -e ".[dev,gateway,demo]"   # Python 3.11+; same extras CI installs —
                               # the fast suite starts the real service, so the
                               # test tooling alone is not enough
docker compose up -d           # Postgres + Redis (integration tests / local runs)
pytest -q -m "not integration" # fast suite
pytest -q                      # full suite (testcontainers needs Docker running)
mypy --strict src
make demo                      # scripted adversarial demo
```

## Definition of done (every PR)

- Tests first, from `tests/acceptance-scenarios.md` and the cited RFC section; full suite
  green including integration.
- All `spec/examples/*` still validate against their schemas.
- `mypy --strict` clean; public types/functions typed and docstring'd.
- The PR notes which RFC sections (or `CS-nnn` change-set items) it implements. Any
  unavoidable ambiguity is marked `# STONEFOLD-AMBIGUITY:` with the RFC reference.
- Specs (documents, schemas, fixtures) live only in the `spec/` submodule — if your change
  needs spec wording, a schema, or a fixture to move, land it in the
  [spec repo](https://github.com/stonefold-ai/spec) first, then bump the submodule pointer.

## Certifying your own gateway

You don't need to touch this repo's internals: implement one TCK driver (or the HTTP
harness for non-Python gateways) and run the kit — see `spec/docs/12-conformance-tck.md`.
The kit's core (`src/stonefold_tck/`) imports nothing from the reference implementation.

## Practical details

- **Small PRs win.** Even with an approved design, prefer a series of small, individually
  reviewable PRs over one large one.
- **Commit messages** should say *why*, not just *what*.
- **Conduct:** be direct, be respectful. Argue about the security properties, not about
  the people.

## License

Apache-2.0. By contributing you agree that:

- your contribution is licensed under Apache-2.0 (§5);
- you certify the [Developer Certificate of Origin](https://developercertificate.org/)
  and sign each commit with `Signed-off-by` (`git commit -s`);
- you grant the maintainer the right to distribute future versions of the project,
  including your contribution, under other license terms.

That last point keeps the project's licensing options open (for example, offering a
commercially licensed edition later) without needing to track down every past
contributor. It does not change the terms of anything already released: every version
published under Apache-2.0 stays Apache-2.0 forever.

## What to expect from the maintainer

This is currently a solo-maintained project. In return for following the rules above, you
can expect a response to proposals within a reasonable time, honest reasons for any
rejection, and credit for your contributions. What you should not expect is fast review
of large or unsolicited work — that trade is what this document is for.
