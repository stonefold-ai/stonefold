# acp_bench — the benchmark harness (docs/15)

Two experiments, **each runnable in isolation from the console**. This page is the
operator's manual: what each track measures, how to run it, and exactly what files it
writes (so you can graph them).

> **Build only. No results live here.** Execution, baseline-fairness sign-off, and
> publication are the author's, personally and non-delegably (docs/15 §7). Nothing in
> the repo may quote a number until the harness, gateway configs, registries/policies,
> and raw logs are public (docs/15 §6). The smoke path emits a loud SMOKE banner.

## The two purposes

**Track R — tool-selection effectiveness.** *The comparison this harness exists for:*
how effectively does a model select the right capability when the same N capabilities
are presented as

| surface | what the model sees |
|---|---|
| `mcp` | N separate tools (the unmitigated tool surface) |
| `mcp-retrieval` | only the top-k tools a naive retriever surfaces (the mandatory mitigated baseline) |
| `sif` | one `submit_intent` tool whose registry declares the same N capabilities |

Per condition × N it measures: **correct** selection, **wrong-tool**, **hallucinated**
names, **malformed** calls, **no-call**, retrieval misses, and token cost. Capability
parity holds — all three surfaces expose the same N capabilities; only the shape
differs (docs/15 §4.1).

**Track S — security ablation (the second purpose, from docs/15).** Whether SIF+ACP
stops injection/hallucination from becoming **executed** unauthorized effects where
commodity defenses do not: the ladder S0 (naked tools) → S1 (gateway allowlist) → S2
(parameter-level policy) → S3 (SIF+ACP full), scored as ASR-executed / ASR-attempted
per attack class (A1–A7), with benign task success (BTS) reported alongside.

## Run it (console)

```bash
# Windows, this repo's venv:
.\.venv\Scripts\python.exe -m acp_bench --track r --smoke

# Track R — tool-selection effectiveness (fake LLM smoke / real run):
python -m acp_bench --track r --smoke
python -m acp_bench --track r --run --models small --reps 5

# Track S — security ablation:
python -m acp_bench --track s --smoke
python -m acp_bench --track s --run --models small,mid --reps 5

python -m acp_bench            # honesty banner + usage, runs nothing
```

**Isolation flags** — narrow any run down to one slice:

```bash
--track r --surfaces mcp,sif          # only these surfaces
--track r --ns 10,100                 # only these tool counts
--track r --probes pay-invoice        # only this benign task
--track s --rungs S0,S3               # only these defense rungs
--models small                        # only this pinned model
--reps 2                              # fewer rounds
--out mydir                           # output base directory (default: bench_out/)
```

`--smoke` uses the deterministic fake LLM (no API key; reps clamped to 2) and proves
the machinery end to end — its numbers are meaningless as measurements. `--run` is the
real experiment with pinned models and API keys (the author's; in a TLS-intercepting
network where `certifi` lacks the corporate CA, run under `truststore`).

## Output contract (streamed — nothing waits for the end of the run)

All files land in `<out>/track-r/` or `<out>/track-s/`:

| File | Written | Contents |
|---|---|---|
| `trials.jsonl` | **appended + flushed as each trial finishes** | one raw trial per line — the published artifact a reviewer recomputes from |
| `cells.json` | **rewritten after every round** (repetition) | aggregated per-cell rates + run context (`rounds_done` tells you how fresh) |
| `cells.csv` | rewritten after every round | the same cells flat, for spreadsheets/plotting |
| `bts.csv` | (track S only) after every round | benign task success per rung |
| `report.md` | end of run | the human-readable matrix with the SMOKE/honesty banner |
| `meta.json` | start + end of run | parameters, filters, start/finish timestamps, trial count |

A run cut short still leaves every finished trial in `trials.jsonl` and the cells as
of the last completed round — and because repetition is the **outermost** loop, a
partial run is a *complete* matrix at fewer repetitions, never a matrix missing
conditions. Progress is printed to stderr after every round.

### Field schemas (for graphing)

`track-r/trials.jsonl` — one line per trial:
`model, condition (mcp|mcp-retrieval|sif), n, probe, rep, outcome (correct|wrong_tool|hallucinated|malformed|no_call), retrieval_miss (bool), tokens, chose`

`track-r/cells.json → cells[]` / `cells.csv` — one row per condition × N:
`condition, n, count, correct, wrong_tool, hallucinated, malformed, no_call, retrieval_miss, tokens_mean` (rates are 0..1)

Typical Track-R graph: x = `n`, y = `correct`, one line per `condition`.

`track-s/trials.jsonl` — one line per trial:
`model, rung (S0..S3), scenario (A1..A7|benign), rep, attempted (bool), executed (bool), benign_ok (bool), tokens, decisions[]`

`track-s/cells.json → cells[]` / `cells.csv` — one row per attack × rung:
`scenario, rung, n, asr_executed, asr_attempted, tokens_mean, tokens_std`; benign
success lives in `bts.csv` (`rung, bts, n`) and in `cells.json → bts`.

Typical Track-S graph: grouped bars of `asr_executed` per `scenario`, one group per
`rung`, with `bts` as the utility check next to it.

## Module map

| Piece | Module | docs/15 |
|---|---|---|
| Track R — surfaces (mcp / retrieval / sif) + scorer | `tracks` | §1 Track R |
| Track R — probes, trial runner, cells, report | `reliability` | §1 Track R |
| Track S — defense ladder S0→S3 | `conditions` | §1 |
| Track S — A1–A7 attack slots (success = *executed*) | `attacks` | §2/§3 |
| Track S — trial runner (≥5 reps/cell) | `runner` | §5 |
| Track S — orchestrator (rep-outermost, callbacks) | `harness` | — |
| Ground-truth oracle (what executed) | `oracle` | §3 |
| Pinned models + token metering | `model` | §4.5/§4.2 |
| Matrix aggregation (ASR/BTS, variance) | `matrix` | §3 |
| Markdown reports | `report`, `reliability` | §3 |
| Streaming + structured output (JSONL/JSON/CSV) | `raw_log` | §5 |
| Console entry point | `__main__` | §5 |

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
- **Execution + fairness sign-off + publication** — the full multi-model ≥5-rep sweeps
  on both tracks, the good-faith judgment that the surfaces/baselines are comparable,
  and the decision to publish (§4/§6/§7).
