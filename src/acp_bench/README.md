# acp_bench — the benchmark harness (docs/15)

The machinery that turns the project's central claim — *a SIF surface measurably
reduces the rate at which injection/hallucination become unauthorized effects, vs. an
equivalent (even gateway-defended) MCP tool surface* — into a measurement.

> **Build only. No results live here.** Execution, baseline-fairness sign-off, and
> publication are the author's, personally and non-delegably (docs/15 §7). Nothing in
> the repo may quote a number until the harness, gateway configs, registries/policies,
> and raw logs are public (docs/15 §6). The smoke path emits a loud SMOKE banner.

## Run it

```
python -m acp_bench --smoke                      # deterministic fake LLM; no API key
python -m acp_bench --smoke --out bench-out       # + write the raw JSONL log
python -m acp_bench --run --models small,mid --reps 5 --out DIR   # the author's run
python -m acp_bench                               # honesty banner + usage, does nothing
```

The smoke proves the machinery runs end to end; its matrix is meaningless as a
measurement (fake LLM). `--run` executes the real experiment with pinned models — the
author's, with API keys.

## What it builds (all reusing the demo agent, `make demo` battery, and AP demo)

| Piece | Module | docs/15 |
|---|---|---|
| Track S — defense ladder S0→S3 | `conditions` | §1 |
| Track R — tool-count reliability sweep + surfaces + scorer | `tracks` | §1 Track R |
| A1–A7 attack slots (success = *executed*) | `attacks` | §2/§3 |
| Ground-truth oracle (what executed) | `oracle` | §3 |
| Pinned models + token metering | `model` | §4.5/§4.2 |
| ≥5-repetition trial runner | `runner` | §5 |
| Matrix aggregator (ASR exec/attempt, BTS, tokens, variance) | `matrix` | §3 |
| Markdown report | `report` | §3 |
| Raw-log JSONL I/O | `raw_log` | §5 |
| Orchestrator | `harness` | — |

## What is deliberately left to the author (flagged, not faked)

- **S1/S2 rung policies** — the commodity-gateway baselines. Configuring them *in good
  faith* is a non-delegable fairness call (§4.4); the harness ships S0+S3 wired and
  reports S1/S2 **UNCONFIGURED** until `policies/s1-allowlist.acp.yaml` /
  `policies/s2-parameter.acp.yaml` exist (see `policies/README.md`).
- **A1, A3–A7 attack scenarios** — the differentiating classes. Porting AgentDojo-style
  cases (A1/A2) and authoring A3–A7 is the author's (§7); these are declared **UNWIRED**
  slots. A2 (invite-to-wire) is fully wired as the worked example.
- **Real token usage** — `MeteredProvider` estimates tokens (~4 chars/token) so the
  matrix has a column on the fake path; a real run substitutes the SDK's `response.usage`.
- **Track R task set + execution** — the surfaces and scorer are built; a real
  reliability number needs a model choosing among N tools over a benign task set (§7).
