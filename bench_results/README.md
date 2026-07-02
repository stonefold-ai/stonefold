# bench_results — raw logs of executed benchmark runs

Raw, verbatim output of `python -m acp_bench` runs that were actually executed
(docs/15 §5: the logs are published so anyone can recompute the matrix). The findings
and their interpretation live in **docs/15 → "Pilot run record"** — this directory is
the evidence, not the narrative.

Every run folder carries the full output contract: `trials.jsonl` (one line per model
call), `cells.json` / `cells.csv` (aggregated rates — the graphing input), `report.md`
(rendered matrix), `meta.json` (parameters + timestamps).

| Folder | Model | Surface version | Status |
|---|---|---|---|
| `2026-07-02-trackR-haiku/` | claude-haiku-4-5-20251001 | fixed (action enum-injected) | **headline data** |
| `2026-07-02-trackR-sonnet/` | claude-sonnet-5 | fixed | headline data |
| `2026-07-02-trackR-opus/` | claude-opus-4-8 | fixed | headline data |
| `2026-07-02-trackR-haiku-freestring-action/` | claude-haiku-4-5-20251001 | pre-fix (free-string `action`) | kept as evidence of the formatting finding (docs/15 pilot record, point 3) — do **not** mix with fixed-surface cells |

**These are PILOT runs**: 2 repetitions per cell (the docs/15 §5 bar is ≥5), one probe
set of 10 benign tasks, the retrieval-assisted baseline was not run, and token counts
are the ~4-chars/token estimate (not SDK usage). They are honest pilots, labelled as
such — not the publishable experiment. Findings + full context: docs/15 → "Pilot run
record".

**Reading the sub-100% cells:** haiku's misses on the MCP surface are *not* wrong-tool
picks — in every one (grep `no_call` in the trials.jsonl files) the model declined to
call **any** tool on the one vaguely-worded probe (`update-address`), while under SIF
it committed to the right capability every time. No model ever selected a wrong tool
or emitted an undeclared name on either surface in the fixed-surface runs.

Graph: `trackR-pilot.svg` (regenerate with the cells.csv files; the generator script
is committed alongside as `make_graph.py`).

## Verify the harness before trusting a number

Every mechanism behind these logs is small, committed code — check it, don't take our
word (docs/15 §5: "someone at a gateway vendor can rerun it and be forced to accept
the number"):

| What to check | Where |
|---|---|
| The two surfaces really expose the same N capabilities (parity, §4.1) | `src/acp_bench/tracks.py` — `mcp_surface` / `sif_surface` / `capability_set`; parity asserted in `tests/test_bench_harness.py` |
| The task set (10 benign probes, one per capability) | `src/acp_bench/reliability.py` — `PROBES` |
| The scoring rules (what counts as correct / wrong-tool / hallucinated / malformed / no-call) | `src/acp_bench/reliability.py` — `_score`, unit-tested in `tests/test_bench_harness.py` |
| The single-turn protocol (system prompts, one call per trial) | `src/acp_bench/reliability.py` — `run_trial`, `_SYS_MCP` / `_SYS_SIF` |
| The token numbers are an ESTIMATE (~4 chars/token) | `src/acp_bench/model.py` — `MeteredProvider` docstring |
| The surface-version difference between run folders | `sif_surface`'s docstring records the 2026-07-02 action-enum fix |

Reproduce a run (any Anthropic key; ~40–120 calls):

```
python -m acp_bench --track r --run --models small --reps 2 --ns 10,50,100 --surfaces mcp,sif --out mydir
```

Recompute the matrix from a raw log without re-running: read `trials.jsonl` and count
outcomes per (condition, n) — the aggregation is `reliability.reliability_matrix`,
~20 lines.
