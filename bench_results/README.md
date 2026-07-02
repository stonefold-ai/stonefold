# bench_results — test data + harness for verifying the method

**Everything needed to check these results — the raw data of every model call, the
harness that produced them, and the scripts that aggregate and plot them — is in this
repository.** Nothing here asks to be trusted: recompute the matrix from the logs,
read the scoring code, or re-run the whole battery with your own key (docs/15 §5:
"someone at a gateway vendor can rerun it and be forced to accept the number").

The findings and their interpretation live in **docs/15 → "Pilot run record"** — this
directory is the evidence, not the narrative.

![Track R realism battery — selection accuracy per configuration (bars) and tokens per call (right)](trackR-pilot.svg)

## Headline data — the realism battery (2026-07-02, Claude Haiku 4.5)

`2026-07-02-trackR-haiku-realism/` — six configurations at N ∈ {10, 50}, `mcp` vs
`sif`, 2 repetitions × 10 probes per cell, all with **confusable** (near-duplicate)
capabilities in the selection space unless noted:

| Folder | What it varies | One-line result |
|---|---|---|
| `confusable/` | look-alike capabilities, typical prompts | MCP 75/55 %, SIF 95/80 % correct (N=10/50) |
| `explicit/` | detailed prompt wording | hurts both at N=50 (wording collides with look-alike names); SIF still ahead |
| `vague/` | underspecified prompts | both mostly ask a clarifying question (~45–65 %) — scored as its own outcome, not failure |
| `distractor/` | prompts needing NO tool | **perfect on both surfaces** — zero over-calls; reported as the tie it is |
| `realistic/` | production-length tool cards | **MCP recovers to 90/90 % — an honest SIF loss on selection (80/70 %)** — but at 5.4× the tokens (8 251 vs 1 519 at N=50) |
| `context2k/` | ~2 000 tokens of prior conversation | MCP degrades (75/60 %), SIF barely moves (95/90 %) |

Every run folder carries the full output contract: `trials.jsonl` (one line per real
model call, including exactly what the model chose), `cells.json` / `cells.csv` (the
aggregated rates — the graphing input), `report.md` (rendered matrix), `meta.json`
(parameters + timestamps).

**These are PILOT runs**: one model, 2 repetitions per cell (the docs/15 §5 bar is
≥5), token counts are the ~4-chars/token estimate, and the retrieval-assisted
baseline was not run. Honest pilots, labelled as such — not the publishable
experiment.

## Superseded: the count-only pilot

`superseded-count-pilot/` — the first pilot (same day, earlier): tool COUNT alone at
N ∈ {10, 50, 100} with semantically distant fillers, three models (Haiku/Sonnet 5/
Opus 4.8). Its result — count alone does not break selection on current models;
tokens grow linearly for MCP (~3.1× gap at N=100) — motivated the realism battery
above, which is why it is superseded as the headline but kept in full (published
logs stay published; the `…-freestring-action/` subfolder is the evidence for the
formatting finding, docs/15 Amendment A1).

## Verify the harness before trusting a number

| What to check | Where |
|---|---|
| The two surfaces expose the same N capabilities, with the SAME descriptions (parity) | `src/acp_bench/tracks.py` (`mcp_surface`/`sif_surface`), `src/acp_bench/realism.py` (`realistic_mcp`/`realistic_sif`); asserted in `tests/test_bench_harness.py` |
| The confusable catalog (synonym verbs/resources, overlapping descriptions) | `src/acp_bench/realism.py` — `confusable_fillers` |
| The task set, phrasings, gold argument values, no-tool distractors | `reliability.PROBES`, `realism._PROMPTS` / `GOLD_VALUES` / `DISTRACTOR_PROMPTS` |
| The scoring rules (correct / wrong_tool / wrong_args / hallucinated / malformed / no_call / clarify / overcall) | `src/acp_bench/reliability.py` — `_score`, unit-tested |
| SIF is scored on picking the right `resource.action` PAIR — calling the single tool earns nothing | `_score`'s SIF branch; the `chose` column in every trials.jsonl |
| The deterministic 2k-token context prefix | `realism.build_context` + `_CONTEXT_TURNS` |
| Token numbers are an ESTIMATE (~4 chars/token) | `src/acp_bench/model.py` — `MeteredProvider` docstring |

Reproduce any cell (your own Anthropic key):

```
python -m acp_bench --track r --run --models small --reps 2 --ns 10,50 \
    --surfaces mcp,sif --fillers confusable [--phrasing vague] [--cards realistic] \
    [--context-tokens 2000] [--probe-set distractor] --out mydir
```

Recompute a matrix from a raw log without re-running: count outcomes per
(condition, n) over `trials.jsonl` — the aggregation is
`reliability.reliability_matrix`, ~25 lines. The graph regenerates from the CSVs via
`make_graph.py` (committed alongside).
